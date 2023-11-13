#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
#  with the License. A copy of the License is located at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
#  and limitations under the License.
from ideasdk.client.evdi_client import EvdiClient
from ideasdk.context import SocaContext
from ideadatamodel.auth import (
    User,
    Group,
    InitiateAuthRequest,
    InitiateAuthResult,
    RespondToAuthChallengeRequest,
    RespondToAuthChallengeResult,
    ListUsersRequest,
    ListUsersResult,
    ListGroupsRequest,
    ListGroupsResult,
    ListUsersInGroupRequest,
    ListUsersInGroupResult,
    ConfigureSSORequest
)
from ideadatamodel import exceptions, errorcodes, constants
from ideasdk.utils import Utils, GroupNameHelper
from ideasdk.auth import TokenService

from ideaclustermanager.app.accounts.ldapclient.abstract_ldap_client import AbstractLDAPClient
from ideaclustermanager.app.accounts.cognito_user_pool import CognitoUserPool
from ideaclustermanager.app.accounts import auth_constants
from ideaclustermanager.app.accounts.auth_utils import AuthUtils
from ideaclustermanager.app.accounts.db.group_dao import GroupDAO
from ideaclustermanager.app.accounts.db.user_dao import UserDAO
from ideaclustermanager.app.accounts.db.group_members_dao import GroupMembersDAO
from ideaclustermanager.app.accounts.db.single_sign_on_state_dao import SingleSignOnStateDAO
from ideaclustermanager.app.accounts.helpers.single_sign_on_helper import SingleSignOnHelper
from ideaclustermanager.app.tasks.task_manager import TaskManager

from typing import Optional, List
import os
import re
import tempfile
import time


def nonce() -> str:
    return Utils.short_uuid()


class AccountsService:
    """
    Account Management Service

    Integrates with OpenLDAP/AD, Cognito User Pools and exposes functionality around:
    1. User Management
    2. Groups
    3. User Onboarding
    4. Single Sign-On

    The service is primarily invoked via AuthAPI and AccountsAPI
    """

    def __init__(self, context: SocaContext,
                 ldap_client: Optional[AbstractLDAPClient],
                 user_pool: Optional[CognitoUserPool],
                 evdi_client: Optional[EvdiClient],
                 task_manager: Optional[TaskManager],
                 token_service: Optional[TokenService]):

        self.context = context
        self.logger = context.logger('accounts-service')
        self.user_pool = user_pool
        self.ldap_client = ldap_client
        self.evdi_client = evdi_client
        self.task_manager = task_manager
        self.token_service = token_service

        self.group_name_helper = GroupNameHelper(context)
        self.user_dao = UserDAO(context, user_pool=user_pool)
        self.group_dao = GroupDAO(context)
        self.group_members_dao = GroupMembersDAO(context, self.user_dao)
        self.sso_state_dao = SingleSignOnStateDAO(context)
        self.single_sign_on_helper = SingleSignOnHelper(context)

        self.user_dao.initialize()
        self.group_dao.initialize()
        self.group_members_dao.initialize()
        self.sso_state_dao.initialize()

        self.ds_automation_dir = self.context.config().get_string('directoryservice.automation_dir', required=True)

    def is_cluster_administrator(self, username: str) -> bool:
        cluster_administrator = self.context.config().get_string('cluster.administrator_username', required=True)
        return username == cluster_administrator

    def is_sso_enabled(self) -> bool:
        return self.context.config().get_bool('identity-provider.cognito.sso_enabled', False)

    # user group management methods

    def get_group(self, group_name: str) -> Optional[Group]:
        if Utils.is_empty(group_name):
            raise exceptions.invalid_params('group_name is required')

        group = self.group_dao.get_group(group_name)

        if group is None:
            raise exceptions.SocaException(
                error_code=errorcodes.AUTH_GROUP_NOT_FOUND,
                message=f'Group not found: {group_name}'
            )

        return self.group_dao.convert_from_db(group)

    def create_group(self, group: Group) -> Group:
        """
        create a new group
        :param group: Group
        """
        ds_readonly = self.ldap_client.is_readonly()

        group_name = group.name
        if not group_name:
            raise exceptions.invalid_params('group.name is required')
        if not group.group_type:
            raise exceptions.invalid_params('group.group_type is required')
        if group.group_type not in constants.ALL_GROUP_TYPES:
            raise exceptions.invalid_params(
                f'invalid group type: {group.group_type}. must be one of: {constants.ALL_GROUP_TYPES}')
        if not group.type:
            group.type = constants.GROUP_TYPE_EXTERNAL
        if group.type not in [constants.GROUP_TYPE_EXTERNAL, constants.GROUP_TYPE_INTERNAL]:
            raise exceptions.invalid_params(
                f'invalid group type: {group.type}. must be one of: {[constants.GROUP_TYPE_EXTERNAL, constants.GROUP_TYPE_INTERNAL]}')

        db_existing_group = self.group_dao.get_group(group_name)
        if db_existing_group is not None:
            raise exceptions.invalid_params(f'group: {group_name} already exists')

        # Perform AD validation only for groups identified as external groups. Internal grpups are RES specific groups and are not expected to be present in the AD.
        if group.type == constants.GROUP_TYPE_EXTERNAL and ds_readonly:
            if not group.ds_name:
                raise exceptions.invalid_params(f'group.ds_name is required when using Read-Only Directory Service: {constants.DIRECTORYSERVICE_ACTIVE_DIRECTORY}')
            group_in_ad = self.ldap_client.get_group(group.ds_name)
            if group_in_ad is None:
                raise exceptions.invalid_params(f'group with name {group.ds_name} is not found in Read-Only Directory Service: {constants.DIRECTORYSERVICE_ACTIVE_DIRECTORY}')
            group_id_in_ad = group_in_ad['gid']
            if group_id_in_ad is None:
                raise exceptions.invalid_params(f'Group id is not found in Directory Service: {constants.DIRECTORYSERVICE_ACTIVE_DIRECTORY}')
            group.gid = group_id_in_ad
            group_name_in_ad = group_in_ad['name']
            if group_name_in_ad is None:
                raise exceptions.invalid_params(f'Group name matching the provided name {group.ds_name} is not found in Directory Service: {constants.DIRECTORYSERVICE_ACTIVE_DIRECTORY}')
            if group_name_in_ad != group.ds_name:
                raise exceptions.invalid_params(f'group.ds_name {group.ds_name} does not match the value {group_name_in_ad} read from Directory Service: {constants.DIRECTORYSERVICE_ACTIVE_DIRECTORY}')

        group.enabled = True

        db_group = self.group_dao.convert_to_db(group)

        self.logger.info(
            f"Creating Group {group.name} (AD READ-ONLY: {ds_readonly}) (IS RES-GROUP: {group.type == constants.GROUP_TYPE_INTERNAL})")

        self.group_dao.create_group(db_group)

        return self.group_dao.convert_from_db(db_group)

    def modify_group(self, group: Group) -> Group:
        """
        modify an existing group
        :param group: Group
        """

        group_name = group.name
        if Utils.is_empty(group_name):
            raise exceptions.invalid_params('group_name is required')

        db_group = self.group_dao.get_group(group_name)
        if db_group is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.AUTH_GROUP_NOT_FOUND,
                message=f'group not found for group name: {group_name}'
            )

        if not db_group['enabled']:
            raise exceptions.soca_exception(
                error_code=errorcodes.AUTH_GROUP_IS_DISABLED,
                message='cannot modify a disabled group'
            )

        # do not support modification of group name or GID
        # only title updates are supported
        update_group = {
            'group_name': db_group['group_name'],
            'title': db_group['title']
        }

        # only update db, sync with DS not required.
        updated_group = self.group_dao.update_group(update_group)

        return self.group_dao.convert_from_db(updated_group)

    def delete_group(self, group_name: str, force: bool = False):
        """
        delete a group
        :param group_name:
        :param force: force delete a group even if group has existing members
        :return:
        """

        if Utils.is_empty(group_name):
            raise exceptions.invalid_params('group_name is required')

        group = self.group_dao.get_group(group_name)

        if group is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.AUTH_GROUP_NOT_FOUND,
                message=f'group: {group_name} not found.'
            )

        # Delete User Group relation
        if self.group_members_dao.has_users_in_group(group_name):
            if force:
                usernames = self.group_members_dao.get_usernames_in_group(
                    group_name)
                try:
                    self.remove_users_from_group(
                        usernames, group_name, force)
                except exceptions.SocaException as e:
                    self.logger.info(f"Error: {e}")
            else:
                raise exceptions.soca_exception(
                    error_code=errorcodes.AUTH_INVALID_OPERATION,
                    message='Users associated to group. Group must be empty before it can be deleted.'
                )

        # Delete Project Group relation
        if self.context.projects.user_projects_dao.has_projects_in_group(group_name):
            if force:
                project_ids = self.context.projects.user_projects_dao.get_projects_by_group_name(
                    group_name)
                try:
                    self.context.projects.remove_projects_from_group(
                        project_ids, group_name, force)
                except exceptions.SocaException as e:
                    self.logger.info(f"Error: {e}")
            else:
                raise exceptions.soca_exception(
                    error_code=errorcodes.AUTH_INVALID_OPERATION,
                    message='Projects associated to group. Group must not be mapped to any project before it can be deleted.'
                )

        self.group_dao.delete_group(group_name=group_name)

    def enable_group(self, group_name: str):
        if Utils.is_empty(group_name):
            raise exceptions.invalid_params('group_name is required')

        group = self.group_dao.get_group(group_name)

        if group is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.AUTH_GROUP_NOT_FOUND,
                message=f'group: {group_name} not found.'
            )

        self.group_dao.update_group({
            'group_name': group_name,
            'enabled': True
        })

    def disable_group(self, group_name: str):
        if Utils.is_empty(group_name):
            raise exceptions.invalid_params('group_name is required')

        group = self.group_dao.get_group(group_name)

        if group is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.AUTH_GROUP_NOT_FOUND,
                message=f'group: {group_name} not found.'
            )

        self.group_dao.update_group({
            'group_name': group_name,
            'enabled': False
        })

    def list_groups(self, request: ListGroupsRequest) -> ListGroupsResult:
        return self.group_dao.list_groups(request)

    def list_users_in_group(self, request: ListUsersInGroupRequest) -> ListUsersInGroupResult:
        return self.group_members_dao.list_users_in_group(request)

    def add_users_to_group(self, usernames: List[str], group_name: str, bypass_active_user_check: bool = False):
        """
        add users to group
        :param usernames: List of usernames to be added to the group
        :param group_name:
        :param bypass_active_user_check: For an inactive user, the user relation with groups and projects are not updated. The group_name is only added to the user's additional_groups attribute.
        The bypass_active_user_check flag can be used to bypass this check. This is helpful to add users to RES specific admin and user groups, were the user's relations to these groups must be updated
        even if the user is inactive
        :return:
        """
        if Utils.is_empty(usernames):
            raise exceptions.invalid_params('usernames is required')

        if Utils.is_empty(group_name):
            raise exceptions.invalid_params('group_name is required')

        group = self.group_dao.get_group(group_name)
        if group is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.AUTH_GROUP_NOT_FOUND,
                message=f'group: {group_name} not found.'
            )
        if not group['enabled']:
            raise exceptions.soca_exception(
                error_code=errorcodes.AUTH_GROUP_IS_DISABLED,
                message='cannot add users to a disabled user group'
            )

        for username in usernames:
            user = self.user_dao.get_user(username)
            if user is None:
                raise exceptions.soca_exception(error_code=errorcodes.AUTH_USER_NOT_FOUND, message=f'user not found: {username}')
            if not user['enabled']:
                raise exceptions.soca_exception(error_code=errorcodes.AUTH_USER_IS_DISABLED, message=f'user is disabled: {username}')

            additional_groups = user.get('additional_groups', [])

            if group_name not in additional_groups:
                self.user_dao.update_user({
                    'username': username,
                    'additional_groups': list(set(user.get('additional_groups', []) + [group_name]))
                })

            if bypass_active_user_check or user['is_active']:
                self.group_members_dao.create_membership(group_name, username)

                # For group_types MODULE or CLUSTER, the user must be added to those groups in cognito too. This gives users admin/user access to various sections on the UI.
                if group['group_type'] not in (constants.GROUP_TYPE_USER, constants.GROUP_TYPE_PROJECT):
                    self.user_pool.admin_add_user_to_group(username=username, group_name=group_name)

                self.logger.info(f'add user projects for user: {username} in group: {group_name} - DAO ...')
                self.context.projects.user_projects_dao.group_member_added(group_name=group_name, username=username)

    def remove_user_from_groups(self, username: str, group_names: List[str]):
        """
        remove a user from multiple groups.
        useful for operations such as delete user.
        :param username:
        :param group_names:
        :return:
        """
        if Utils.is_empty(username):
            raise exceptions.invalid_params('usernames is required')
        if Utils.is_empty(group_names):
            raise exceptions.invalid_params('group_names is required')

        self.logger.info(f'removing user: {username} from groups: {group_names}')

        # dedupe and sanitize
        groups_to_remove = []
        for group_name in group_names:
            if group_name in groups_to_remove:
                continue
            groups_to_remove.append(group_name)

        user = self.user_dao.get_user(username)
        if user is None:
            raise exceptions.soca_exception(error_code=errorcodes.AUTH_USER_NOT_FOUND, message=f'user not found: {username}')
        if not user['enabled']:
            raise exceptions.soca_exception(error_code=errorcodes.AUTH_USER_IS_DISABLED, message=f'user is disabled: {username}')

        additional_groups = user.get('additional_groups', [])

        for group_name in groups_to_remove:
            group = self.group_dao.get_group(group_name)
            if group is None:
                if not user['is_active']:
                    if group_name in additional_groups:
                        additional_groups.remove(group_name)
                else:
                    self.logger.error(f'user: {username} cannot be removed from group: {group_name}. group not found')
                continue
            if not group["enabled"]:
                self.logger.warning(f'user: {username} cannot be removed from group: {group_name}. group is disabled')
                continue

            if group_name in additional_groups:
                additional_groups.remove(group_name)

            self.group_members_dao.delete_membership(group_name, username)

            # For group_types MODULE or CLUSTER, the user must be removed from those groups in cognito too. This removes user's admin/user access to various sections on the UI.
            if group["group_type"] not in (constants.GROUP_TYPE_USER, constants.GROUP_TYPE_PROJECT):
                self.user_pool.admin_remove_user_from_group(username=username, group_name=group_name)

        self.user_dao.update_user({'username': username, 'additional_groups': additional_groups})

        # removing user projects after updating the user object, to ensure updated user membership is accessible during user-project deletion.
        for group_name in groups_to_remove:
            self.logger.info(f"Removing user projects for user {username} in group {group_name}")
            self.context.projects.user_projects_dao.group_member_removed(group_name=group_name, username=username)

    def remove_users_from_group(self, usernames: List[str], group_name: str, force: bool = False):
        """
        remove multiple users from a group
        useful for bulk operations from front end
        :param usernames:
        :param group_name:
        :param force: force delete even if group is disabled. if user is not found, skip user.
        :return:
        """
        if Utils.is_empty(usernames):
            raise exceptions.invalid_params('usernames is required')

        if Utils.is_empty(group_name):
            raise exceptions.invalid_params('group_name is required')

        try:
            group = self.group_dao.get_group(group_name)
            if group is None:
                raise exceptions.soca_exception(
                    error_code=errorcodes.AUTH_GROUP_NOT_FOUND,
                    message=f'group: {group_name} not found.'
                )
            if not group['enabled'] and not force:
                raise exceptions.soca_exception(
                    error_code=errorcodes.AUTH_GROUP_IS_DISABLED,
                    message='cannot remove users from a disabled user group'
                )

            for username in usernames:
                user = self.user_dao.get_user(username)
                if user is None:
                    if force:
                        continue
                    raise exceptions.soca_exception(error_code=errorcodes.AUTH_USER_NOT_FOUND, message=f'user not found: {username}')
                if not user['enabled'] and not force:
                    raise exceptions.soca_exception(error_code=errorcodes.AUTH_USER_IS_DISABLED, message=f'user is disabled: {username}')

                additional_groups = user.get('additional_groups', [])
                if group_name in additional_groups:
                    self.user_dao.update_user({
                        'username': username,
                        'additional_groups': list(set(user.get('additional_groups', [])) - {group_name})
                    })

                self.group_members_dao.delete_membership(group_name, username)

                # For group_types MODULE or CLUSTER, the user must be removed from those groups in cognito too. This removes user's admin/user access to various sections on the UI.
                if group['group_type'] not in (constants.GROUP_TYPE_USER, constants.GROUP_TYPE_PROJECT):
                    self.user_pool.admin_remove_user_from_group(username=username, group_name=group_name)

                self.context.projects.user_projects_dao.group_member_removed(
                    group_name=group_name,
                    username=username
                )
        except exceptions.SocaException as e:
            if e.error_code == errorcodes.AUTH_GROUP_NOT_FOUND:
                for username in usernames:
                    user = self.user_dao.get_user(username)
                    if not user['is_active']:
                        self.logger.info(f"Detected removal of inactive user {username} from deleted group {group_name}.")
                        self.context.accounts.user_dao.update_user({
                            'username': username,
                            'additional_groups': list(set(user.get('additional_groups', []) - {group_name}))
                        })
                        self.logger.info(f"Removed inactive user {username} from group {group_name} successfully")
                    else:
                        self.logger.error(f"Failed: Removal of active user {username} from deleted group {group_name}")
            else:
                raise e

    # sudo user management methods

    def add_admin_user(self, username: str):

        if Utils.is_empty(username):
            raise exceptions.invalid_params('username is required')

        user = self.user_dao.get_user(username)
        if user is None:
            raise exceptions.SocaException(
                error_code=errorcodes.AUTH_USER_NOT_FOUND,
                message=f'User not found: {username}'
            )

        if user.get('role') == 'admin':
            return

        self.user_dao.update_user({
            'username': username,
            'role': 'admin',
            'sudo': True
        })

        self.user_pool.admin_add_user_as_admin(username)

    def remove_admin_user(self, username: str):

        if Utils.is_empty(username):
            raise exceptions.invalid_params('username is required')

        if self.is_cluster_administrator(username):
            raise AuthUtils.invalid_operation(f'Admin rights cannot be revoked from RES Environment Administrator: {username}.')

        user = self.user_dao.get_user(username)
        if user is None:
            raise exceptions.SocaException(
                error_code=errorcodes.AUTH_USER_NOT_FOUND,
                message=f'User not found: {username}'
            )

        if user.get('role') == 'user':
            return

        self.user_dao.update_user({
            'username': username,
            'role': 'user',
            'sudo': False
        })

        self.user_pool.admin_remove_user_as_admin(username)

    # user management methods

    def get_user(self, username: str) -> User:

        username = AuthUtils.sanitize_username(username)
        if Utils.is_empty(username):
            raise exceptions.invalid_params('username is required.')

        user = self.user_dao.get_user(username)
        if user is None:
            raise exceptions.SocaException(
                error_code=errorcodes.AUTH_USER_NOT_FOUND,
                message=f'User not found: {username}'
            )

        return self.user_dao.convert_from_db(user)

    def create_user(self, user: User, email_verified: bool = False) -> User:
        """
        create a new user
        """

        # username
        username = user.username
        username = AuthUtils.sanitize_username(username)
        if username == None or len(username.strip()) == 0:
            raise exceptions.invalid_params('user.username is required')
        if not re.match(auth_constants.USERNAME_REGEX, username):
            raise exceptions.invalid_params(f'user.username must match regex: {auth_constants.USERNAME_REGEX}')
        AuthUtils.check_allowed_username(username)

        bootstrap_user = self.is_cluster_administrator(user.username)

        if not bootstrap_user and not self.ldap_client.is_existing_user(username):
            raise exceptions.invalid_params(f'User {username} does not exist in AD, please add user in AD and retry')

        existing_user = self.user_dao.get_user(username)
        if existing_user is not None:
            raise exceptions.soca_exception(
                error_code=errorcodes.AUTH_USER_ALREADY_EXISTS,
                message=f'username: {username} already exists.'
            )

        # email
        email = user.email
        email = AuthUtils.sanitize_email(email)

        # password
        password = user.password
        if email_verified:
            if password == None or len(password.strip()) == 0:
                raise exceptions.invalid_params('Password is required')

            user_pool_password_policy = self.user_pool.describe_password_policy()
            # Validate password compliance versus Cognito user pool password policy
            # Cognito: https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-settings-policies.html
            if len(password) < user_pool_password_policy.minimum_length:
                raise exceptions.invalid_params(f'Password should be at least {user_pool_password_policy.minimum_length} characters long')
            elif len(password) > 256:
                raise exceptions.invalid_params(f'Password can be up to 256 characters')
            elif user_pool_password_policy.require_numbers and re.search('[0-9]', password) is None:
                raise exceptions.invalid_params('Password should include at least 1 number')
            elif user_pool_password_policy.require_uppercase and re.search('[A-Z]', password) is None:
                raise exceptions.invalid_params('Password should include at least 1 uppercase letter')
            elif user_pool_password_policy.require_lowercase and re.search('[a-z]', password) is None:
                raise exceptions.invalid_params('Password should include at least 1 lowercase letter')
            elif user_pool_password_policy.require_symbols and re.search('[\^\$\*\.\[\]{}\(\)\?"!@#%&\/\\,><\':;\|_~`=\+\-]', password) is None:
                raise exceptions.invalid_params('Password should include at least 1 of these special characters: ^ $ * . [ ] { } ( ) ? " ! @ # % & / \ , > < \' : ; | _ ~ ` = + -')
        else:
            self.logger.debug('create_user() - setting password to random value')
            password = Utils.generate_password(8, 2, 2, 2, 2)

        # login_shell
        login_shell = user.login_shell
        if login_shell == None or len(login_shell.strip()) == 0:
            login_shell = auth_constants.DEFAULT_LOGIN_SHELL

        # home_dir
        home_dir = user.home_dir
        if home_dir == None or len(home_dir.strip()) == 0:
            home_dir = os.path.join(auth_constants.USER_HOME_DIR_BASE, username)

        # note: no validations on uid / gid if existing uid/gid is provided.
        # ensuring uid/gid uniqueness is administrator's responsibility.

        # uid
        uid = user.uid
        if uid is None and not bootstrap_user:
            raise exceptions.invalid_params('user.uid missing in AD user data')

        gid = user.gid
        if gid is None and not bootstrap_user:
            raise exceptions.invalid_params('user.gid missing in AD user data')

        # sudo
        sudo = bool(user.sudo)

        # res admin
        admin = user.role == 'admin'

        # is_active
        is_active = bool(user.is_active)

        self.logger.info(f'creating Cognito user pool entry: {username}, Email: {email} , email_verified: {email_verified}')

        self.user_pool.admin_create_user(
            username=username,
            email=email,
            password=password,
            email_verified=email_verified
        )

        # additional groups
        additional_groups = Utils.get_as_list(user.additional_groups, [])
        self.logger.debug(f'Additional groups for {username}: {additional_groups}')

        self.logger.info(f"Creating user {username} in RES DDB table")
        created_user = self.user_dao.create_user({
                'username': username,
                'email': email,
                'uid': uid,
                'gid': gid,
                'additional_groups': additional_groups,
                'login_shell': login_shell,
                'home_dir': home_dir,
                'sudo': sudo,
                'enabled': True,
                'role': constants.ADMIN_ROLE if admin else constants.USER_ROLE,
                'is_active': is_active,
            })

        if self.is_sso_enabled():
            self.logger.debug(f'Performing IDP Link for {username} / {email}')
            self.user_pool.admin_link_idp_for_user(username, email)
        if admin:
            self.logger.debug(f'Performing ADMIN for {username}')
            self.user_pool.admin_add_user_as_admin(username)

        # todo: Remove once use of RES specific groups is removed
        for user_group in constants.RES_USER_GROUPS:
            try:
                self.logger.info(f'Adding username {username} to RES_USER_GROUP: {user_group}')
                self.add_users_to_group([username], user_group, bypass_active_user_check=True)
            except Exception as e:
                self.logger.debug(f"Could not add user {username} to RES_USER_GROUP: {user_group}")

        for additional_group in additional_groups:
            self.logger.info(f'Adding username {username} to additional group: {additional_group}')
            self.add_users_to_group([username], additional_group)

        # bootstrap user does not exist in AD, therefore skipping home directory create
        if not bootstrap_user:
            self.task_manager.send(
                task_name='accounts.create-home-directory',
                payload={
                    'username': username
                },
                message_group_id=username
            )

        return self.user_dao.convert_from_db(created_user)

    def modify_user(self, user: User, email_verified: bool = False) -> User:
        """
        Modify User

        Only ``email`` updates are supported at the moment.

        :param user:
        :param email_verified:
        :return:
        """

        username = user.username
        username = AuthUtils.sanitize_username(username)

        existing_user = self.user_dao.get_user(username)
        if existing_user is None:
            raise exceptions.soca_exception(error_code=errorcodes.AUTH_USER_NOT_FOUND,
                                            message=f'User not found: {username}')
        if not existing_user['enabled']:
            raise exceptions.soca_exception(error_code=errorcodes.AUTH_USER_IS_DISABLED,
                                            message='User is disabled and cannot be modified.')

        user_updates = {
            'username': username
        }

        if Utils.is_not_empty(user.email):
            new_email = AuthUtils.sanitize_email(user.email)
            existing_email = Utils.get_value_as_string('email', existing_user)
            if existing_email != new_email:
                user_updates['email'] = new_email
                self.user_pool.admin_update_email(username, new_email, email_verified=email_verified)
                if self.is_sso_enabled():
                    self.user_pool.admin_link_idp_for_user(username, new_email)

        if Utils.is_not_empty(user.login_shell):
            user_updates['login_shell'] = user.login_shell

        if user.sudo is not None:
            user_updates['sudo'] = Utils.get_as_bool(user.sudo, False)

        if user.uid is not None:
            user_updates['uid'] = user.uid

        if user.gid is not None:
            user_updates['gid'] = user.gid

        updated_user = self.user_dao.update_user(user_updates)

        return self.user_dao.convert_from_db(updated_user)

    def activate_user(self, existing_user: User):
        if not existing_user.is_active:
            username = existing_user.username
            for additional_group in existing_user.additional_groups:
                try:
                    self.logger.info(f'Adding username {username} to additional group: {additional_group}')
                    self.context.accounts.add_users_to_group([username], additional_group, bypass_active_user_check=True)
                except Exception as e:
                    self.logger.warning(f'Could not add user {username} to group {additional_group}')
            self.user_dao.update_user({'username': username, 'is_active': True})

    def enable_user(self, username: str):
        if Utils.is_empty(username):
            raise exceptions.invalid_params('username is required')

        existing_user = self.user_dao.get_user(username)
        if existing_user is None:
            raise exceptions.soca_exception(error_code=errorcodes.AUTH_USER_NOT_FOUND,
                                            message=f'User not found: {username}')

        is_enabled = Utils.get_value_as_bool('enabled', existing_user, False)
        if is_enabled:
            return

        self.user_pool.admin_enable_user(username)
        self.user_dao.update_user({'username': username, 'enabled': True})

    def disable_user(self, username: str):
        if Utils.is_empty(username):
            raise exceptions.invalid_params('username is required')

        if self.is_cluster_administrator(username):
            raise AuthUtils.invalid_operation('Cluster Administrator cannot be disabled.')

        existing_user = self.user_dao.get_user(username)
        if existing_user is None:
            raise exceptions.soca_exception(error_code=errorcodes.AUTH_USER_NOT_FOUND,
                                            message=f'User not found: {username}')

        is_enabled = Utils.get_value_as_bool('enabled', existing_user, False)
        if not is_enabled:
            return

        self.user_pool.admin_disable_user(username)
        self.user_dao.update_user({'username': username, 'enabled': False})

        self.evdi_client.publish_user_disabled_event(username=username)

    def delete_user(self, username: str):
        log_tag = f'(DeleteUser: {username})'

        if self.is_cluster_administrator(username):
            raise AuthUtils.invalid_operation('Cluster Administrator cannot be deleted.')

        user = self.user_dao.get_user(username=username)
        if user is None:
            self.logger.info(f'{log_tag} user not found. skip.')
            return

        groups = Utils.get_value_as_list('additional_groups', user, [])

        if Utils.is_not_empty(groups):
            self.logger.info(f'{log_tag} clean-up group memberships')
            try:
                self.remove_user_from_groups(username=username, group_names=groups)
            except exceptions.SocaException as e:
                if e.error_code == errorcodes.AUTH_USER_IS_DISABLED:
                    pass
                else:
                    raise e

        # disable user from db, user pool and delete from directory service
        self.logger.info(f'{log_tag} disabling user')
        self.disable_user(username=username)

        # delete user in user pool
        self.logger.info(f'{log_tag} delete user from user pool')
        self.user_pool.admin_delete_user(username=username)

        # delete user from db
        self.logger.info(f'{log_tag} delete user in ddb')
        self.user_dao.delete_user(username=username)

    def reset_password(self, username: str):
        username = AuthUtils.sanitize_username(username)
        if Utils.is_empty(username):
            raise exceptions.invalid_params('username is required')

        # trigger reset password email
        self.user_pool.admin_reset_password(username)

    def list_users(self, request: ListUsersRequest) -> ListUsersResult:
        return self.user_dao.list_users(request)

    def change_password(self, access_token: str, username: str, old_password: str, new_password: str):
        """
        change password for given username in user pool and ldap
        this method expects an access token from an already logged-in user, who is trying to change their password.
        :return:
        """

        # change password in user pool before changing in ldap
        self.user_pool.change_password(
            username=username,
            access_token=access_token,
            old_password=old_password,
            new_password=new_password
        )

    # public API methods for user onboarding, login, forgot password flows.

    def initiate_auth(self, request: InitiateAuthRequest) -> InitiateAuthResult:

        auth_flow = request.auth_flow
        if Utils.is_empty(auth_flow):
            raise exceptions.invalid_params('auth_flow is required.')

        if auth_flow == 'USER_PASSWORD_AUTH':

            username = request.username
            if Utils.is_empty(username):
                raise exceptions.invalid_params('username is required.')

            password = request.password
            if Utils.is_empty(password):
                raise exceptions.invalid_params('password is required.')

            return self.user_pool.initiate_username_password_auth(request)

        elif auth_flow == 'REFRESH_TOKEN_AUTH':

            username = request.username
            if Utils.is_empty(username):
                raise exceptions.invalid_params('username is required.')

            refresh_token = request.refresh_token
            if Utils.is_empty(refresh_token):
                raise exceptions.invalid_params('refresh_token is required.')

            return self.user_pool.initiate_refresh_token_auth(username, refresh_token)

        elif auth_flow == 'SSO_AUTH':

            if not self.is_sso_enabled():
                raise exceptions.unauthorized_access()

            authorization_code = request.authorization_code
            if Utils.is_empty(authorization_code):
                raise exceptions.invalid_params('authorization_code is required.')

            db_sso_state = self.sso_state_dao.get_sso_state(authorization_code)
            if db_sso_state is None:
                raise exceptions.unauthorized_access()

            auth_result = self.sso_state_dao.convert_from_db(db_sso_state)

            self.sso_state_dao.delete_sso_state(authorization_code)

            return InitiateAuthResult(
                auth=auth_result
            )

        elif auth_flow == 'SSO_REFRESH_TOKEN_AUTH':

            if not self.is_sso_enabled():
                raise exceptions.unauthorized_access()

            username = request.username
            if Utils.is_empty(username):
                raise exceptions.invalid_params('username is required.')

            refresh_token = request.refresh_token
            if Utils.is_empty(refresh_token):
                raise exceptions.invalid_params('refresh_token is required.')

            return self.user_pool.initiate_refresh_token_auth(username, refresh_token, sso=True)

    def respond_to_auth_challenge(self, request: RespondToAuthChallengeRequest) -> RespondToAuthChallengeResult:

        if Utils.is_empty(request.username):
            raise exceptions.invalid_params('username is required.')

        challenge_name = request.challenge_name
        if Utils.is_empty(challenge_name):
            raise exceptions.invalid_params('challenge_name is required.')
        if challenge_name != 'NEW_PASSWORD_REQUIRED':
            raise exceptions.invalid_params(f'challenge_name: {challenge_name} is not supported.')

        if Utils.is_empty(request.session):
            raise exceptions.invalid_params('session is required.')

        if Utils.is_empty(request.new_password):
            raise exceptions.invalid_params('new_password is required.')

        self.logger.debug(f'respond_to_auth_challenge() - Request: {request}')

        result = self.user_pool.respond_to_auth_challenge(request)

        return result

    def forgot_password(self, username: str):
        """
        invoke user pool's forgot password API
        introduce mandatory timing delays to ensure valid / invalid user invocations are processed in approximately the same time
        """
        if Utils.is_empty(username):
            raise exceptions.invalid_params('username is required')

        wait_time_seconds = 5
        start = Utils.current_time_ms()
        self.user_pool.forgot_password(username)
        end = Utils.current_time_ms()
        total_secs = (end - start) / 1000

        if total_secs <= wait_time_seconds:
            time.sleep(wait_time_seconds - total_secs)

    def confirm_forgot_password(self, username: str, password: str, confirmation_code: str):
        if Utils.is_empty(username):
            raise exceptions.invalid_params('username is required')
        if Utils.is_empty(password):
            raise exceptions.invalid_params('password is required')
        if Utils.is_empty(confirmation_code):
            raise exceptions.invalid_params('confirmation_code is required')

        # update user-pool first to verify confirmation code.
        self.user_pool.confirm_forgot_password(username, password, confirmation_code)

    def sign_out(self, refresh_token: str, sso_auth: bool):
        """
        revokes the refresh token issued by InitiateAuth API.
        """
        self.token_service.revoke_authorization(
            refresh_token=refresh_token,
            sso_auth=sso_auth
        )

    def global_sign_out(self, username: str):
        """
        Signs out a user from all devices.
        It also invalidates all refresh tokens that Amazon Cognito has issued to a user.
        The user's current access and ID tokens remain valid until they expire.
        """

        if Utils.is_empty(username):
            raise exceptions.invalid_params('username is required')

        self.user_pool.admin_global_sign_out(username=username)

    def _get_gid_from_existing_ldap_group(self, groupname: str):
        existing_gid = None
        existing_group_from_ds = self.ldap_client.get_group(group_name=groupname)
        self.logger.debug(f'READ-ONLY DS lookup results for group  {groupname}: {existing_group_from_ds}')

        if existing_group_from_ds is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.GENERAL_ERROR,
                message=f'Unable to Resolve a required RES group from directory services: RES group {groupname}'
            )

        existing_gid = Utils.get_value_as_int('gid', existing_group_from_ds, default=None)

        if existing_gid is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.GENERAL_ERROR,
                message=f'Found group without POSIX gidNumber attribute - UNABLE to use this group. Update group with gidNumber attribute within Directory Service: {groupname}'
            )

        return existing_gid

    def create_defaults(self):
        ds_readonly = self.ldap_client.is_readonly()

        self.logger.info(f'Creating defaults. Read-Only status: {ds_readonly}')

        try:
            # cluster admin user
            admin_username = self.context.config().get_string('cluster.administrator_username', required=True)
            admin_email = self.context.config().get_string('cluster.administrator_email', required=True)
            admin_user = self.user_dao.get_user(username=admin_username)
            if admin_user is None:
                self.logger.info(f'creating cluster admin user: {admin_username}')
                self.create_user(
                    user=User(
                        username=admin_username,
                        email=admin_email,
                        sudo=False,
                        role='admin',
                        is_active=True,  # cluster admin always considered active user
                    ),
                    email_verified=False,
                )
        except Exception as e:
            self.logger.error(f"Error during clusteradmin user creation: {e}")

        # create managers group
        cluster_managers_group_name = self.group_name_helper.get_cluster_managers_group()
        self.create_res_group(ds_readonly, group_name=cluster_managers_group_name,
                              title="Managers (Administrators without sudo access)", group_type=constants.GROUP_TYPE_CLUSTER, ref=None)

        # for all "app" modules in the cluster, create the module users and module administrators group to enable fine-grained access
        # if an application module is added at a later point in time, a cluster-manager restart should fix the issue.
        # ideally, an 'resctl initialize-defaults' command is warranted to address this scenario and will be taken up in a future release.
        modules = self.context.get_cluster_modules()
        for module in modules:
            if module['type'] != constants.MODULE_TYPE_APP:
                continue
            module_id = module['module_id']
            module_name = module['name']

            module_administrators_group_name = self.group_name_helper.get_module_administrators_group(module_id=module_id)
            self.create_res_group(ds_readonly, group_name=module_administrators_group_name,
                                  title=f"Administrators for Module: {module_name}, ModuleId: {module_id}, DS: {module_administrators_group_name}", group_type=constants.GROUP_TYPE_MODULE, ref=module_id)

            module_users_group_name = self.group_name_helper.get_module_users_group(module_id=module_id)
            self.create_res_group(ds_readonly, group_name=module_users_group_name,
                                  title=f"Users for Module: {module_name}, ModuleId: {module_id}", group_type=constants.GROUP_TYPE_MODULE, ref=module_id)

    def create_res_group(self, ds_readonly: bool, group_name: Optional[str], title: Optional[str], group_type: Optional[str], ref: str = None):
        if ds_readonly:
            self.logger.info(f'Skipping {group_name} sync with AD')

        group = self.group_dao.get_group(group_name)

        if group is None:
            self.logger.info(f'Group {group_name} not found in RES DynamoDB')

        try:
            self.create_group(
                group=Group(
                    title=title,
                    name=group_name,
                    ds_name=group_name,
                    gid=None,
                    group_type=group_type,
                    ref=ref,
                    type=constants.GROUP_TYPE_INTERNAL
                ),
            )
        except Exception as e:
            self.logger.error(f'Error {e}')

        self.logger.info(f'creating group: {group_name}')

    def configure_sso(self, request: ConfigureSSORequest):
        self.single_sign_on_helper.configure_sso(request)
        self.context.ad_sync.sync_from_ad()  # submit ad_sync task after configuring SSO
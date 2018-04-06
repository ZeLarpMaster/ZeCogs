import discord
import os.path
import os

from .utils.dataIO import dataIO
from discord.ext import commands
from cogs.utils import checks


class AssignRoles:
    """Authorize one role to give another role."""

    DATA_FOLDER = "data/assign_roles"
    CONFIG_FILE_PATH = DATA_FOLDER + "/config.json"

    CONFIG_DEFAULT = {}  # Structure: {server.id: {giveable_role: [authorized_roles]}}

    ASSIGN_ADDED = ":white_check_mark: Successfully assigned the `{}` role."
    ASSIGN_REMOVED = ":put_litter_in_its_place: Successfully removed the `{}` role."
    ASSIGN_NO_EVERYONE = ":x: Error: you cannot give someone the Everyone role!"
    AUTHORIZE_EXISTS = ":x: Error: the role you want to authorized is already authorized to give this role."
    AUTHORIZE_EMPTY = ":x: Error: `{}` is not authorized to be assigned by any other roles."
    AUTHORIZE_MISMATCH = ":x: Error: {} is not currently authorized to give the `{}` role."
    AUTHORIZE_NO_EVERYONE = ":x: Error: you cannot authorize everyone to give a role!"
    AUTHORIZE_NO_HIGHER = ":x: Error: you cannot authorize a role that is not below your highest role!"
    AUTHORIZE_SUCCESS = ":white_check_mark: Successfully authorized `{}` to assign the `{}` role."
    CLEAN_SUCCESS = ":white_check_mark: Successfully cleaned the role authorizations."
    DEAUTHORIZE_SUCCESS = ":put_litter_in_its_place: Successfully de-authorized `{}` to assign the `{}` role."
    LIST_DESC_NORMAL = "The roles below can be given by the mentioned roles."
    LIST_DESC_EMPTY = "No roles are authorized to give other roles."

    def __init__(self, bot: discord.Client):
        self.bot = bot
        self.check_configs()
        self.load_data()

    # Events

    # Commands
    @commands.group(name="assign", pass_context=True, invoke_without_command=True, no_pm=True)
    async def _assign(self, ctx, role: discord.Role, user: discord.User=None):
        """Assign a role to a user"""
        msg = ctx.message
        author = msg.author
        if user is None:
            user = author
        server_dict = self.config.setdefault(msg.server.id, {})
        role_id = role.id

        if role.is_everyone:
            notice = self.ASSIGN_NO_EVERYONE
        elif role_id not in server_dict:  # No role authorized to give this role.
            notice = self.AUTHORIZE_EMPTY.format(role.name)
        # Check if any of the author's roles is authorized to grant the role.
        elif not any(r.id in server_dict[role_id] for r in author.roles):
            notice = self.AUTHORIZE_MISMATCH.format(author.mention, role.name)
        else:  # Role "transaction" is valid.
            if role in user.roles:
                await self.bot.remove_roles(user, role)
                notice = self.ASSIGN_REMOVED.format(role.name)
            else:
                await self.bot.add_roles(user, role)
                notice = self.ASSIGN_ADDED.format(role.name)
        await self.bot.send_message(msg.channel, notice)

    @_assign.command(pass_context=True, no_pm=True)
    @checks.admin_or_permissions(manage_server=True)
    async def authorize(self, ctx, authorized_role: discord.Role, giveable_role: discord.Role):
        """Authorize one role to give another role

        Allows all members with the role `authorized_role` to give the role `giveable_role` to everyone.
        In order to authorize, your highest role must be strictly higher than `authorized_role`."""
        msg = ctx.message
        server_dict = self.config.setdefault(msg.server.id, {})

        author_max_role = max(r for r in msg.author.roles)
        authorized_id = authorized_role.id
        giveable_id = giveable_role.id

        if authorized_role.is_everyone:  # Role to be authorized should not be @everyone.
            notice = self.AUTHORIZE_NO_EVERYONE
        elif authorized_role >= author_max_role:  # Hierarchical role order check.
            notice = self.AUTHORIZE_NO_HIGHER
        # Check if "pair" already exists.
        elif giveable_id in server_dict and authorized_id in server_dict[giveable_id]:
            notice = self.AUTHORIZE_EXISTS
        else:  # Role authorization is valid.
            server_dict.setdefault(giveable_id, []).append(authorized_id)
            self.save_data()
            notice = self.AUTHORIZE_SUCCESS.format(authorized_role.name, giveable_role.name)
        await self.bot.send_message(msg.channel, notice)

    @_assign.command(pass_context=True, no_pm=True)
    @checks.admin_or_permissions(manage_server=True)
    async def deauthorize(self, ctx, authorized_role: discord.Role, giveable_role: discord.Role):
        """De-authorize one role to give another role

        In order to de-authorize, your highest role must be strictly higher than `authorized_role`."""
        msg = ctx.message
        server_dict = self.config.setdefault(msg.server.id, {})

        author_max_role = max(r for r in msg.author.roles)
        authorized_id = authorized_role.id
        giveable_id = giveable_role.id

        if authorized_role.is_everyone:  # Role to be de-authorized should not be @everyone.
            notice = self.AUTHORIZE_NO_EVERYONE
        elif authorized_role >= author_max_role:  # Hierarchical role order check.
            notice = self.AUTHORIZE_NO_HIGHER
        elif giveable_id not in server_dict:
            notice = self.AUTHORIZE_EMPTY.format(giveable_role.name)
        elif authorized_id not in server_dict[giveable_id]:
            notice = self.AUTHORIZE_MISMATCH.format(authorized_role.name, giveable_role.name)
        else:  # Role de-authorization is valid.
            server_dict[giveable_id].remove(authorized_id)
            self.save_data()
            notice = self.DEAUTHORIZE_SUCCESS.format(authorized_role.name, giveable_role.name)
        await self.bot.send_message(msg.channel, notice)

    @_assign.command(pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_server=True)
    async def list(self, ctx):
        """Send an embed showing which roles can be given by other roles"""
        msg = ctx.message
        srv = msg.server
        server_dict = self.config.setdefault(srv.id, {})
        embed = discord.Embed(colour=0x00D8FF, title="Assign authorizations")

        for role_id, auth_list in server_dict.items():
            role = discord.utils.get(srv.roles, id=role_id)
            if role is not None:
                auth_roles = (discord.utils.get(srv.roles, id=i) for i in auth_list)
                mentions_str = ", ".join(r.mention for r in auth_roles if r is not None)
                if len(mentions_str) > 0:  # Prevent empty fields from being sent.
                    embed.add_field(name=role.name, value=mentions_str)

        embed.description = self.LIST_DESC_EMPTY if len(embed.fields) == 0 else self.LIST_DESC_NORMAL
        await self.bot.send_message(msg.channel, embed=embed)

    # Config
    def check_configs(self):
        self.check_folders()
        self.check_files()

    def check_folders(self):
        if not os.path.exists(self.DATA_FOLDER):
            os.makedirs(self.DATA_FOLDER, exist_ok=True)

    def check_files(self):
        self.check_file(self.CONFIG_FILE_PATH, self.CONFIG_DEFAULT)

    def check_file(self, file, default):
        if not dataIO.is_valid_json(file):
            dataIO.save_json(file, default)

    def load_data(self):
        self.config = dataIO.load_json(self.CONFIG_FILE_PATH)

    def save_data(self):
        dataIO.save_json(self.CONFIG_FILE_PATH, self.config)


def setup(bot):
    bot.add_cog(AssignRoles(bot))

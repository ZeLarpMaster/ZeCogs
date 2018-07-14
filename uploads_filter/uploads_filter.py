import contextlib
import copy
import discord
import os.path
import os
import logging

from discord.ext import commands
from discord.ext.commands import Context

from .utils import checks
from .utils.dataIO import dataIO


class UploadsFilter:
    """Filters files uploaded to some channels"""

    DATA_FOLDER = "data/uploads_filter"
    CONFIG_FILE_PATH = DATA_FOLDER + "/config.json"

    CONFIG_DEFAULT = {}
    CHANNEL_DEFAULT = {"types": [], "allow": None}

    YES_STRINGS = ("yes", "y", "1", "true", "t")
    NO_STRINGS = ("no", "n", "0", "false", "f")

    CANT_BE_WHITELIST_AND_BLACKLIST = ":x: Error: You cannot put both a whitelist and a blacklist on a channel"
    FILETYPE_ALLOWED = ":white_check_mark: The filetype `.{}` has been allowed in <#{}>."
    FILETYPE_DENIED = ":white_check_mark: The filetype `.{}` has been prehibited in <#{}>."
    ALREADY_WHITELISTED = ":x: The filetype `.{}` is already whitelisted in <#{}>."
    ALREADY_BLACKLISTED = ":x: The filetype `.{}` is already blacklisted in <#{}>."
    NOT_IN_LIST = ":x: The filetype `.{}` isn't in the list of <#{}>."
    LIST_CLEARED = ":put_litter_in_its_place: The list for <#{}> has been cleared."
    FILETYPE_WARNING = "You are not allowed to send a file of type `.{}` in <#{}>."
    CANT_SET_LIST_ON_PMS = ":x: Cannot put upload rules on private messages."

    def __init__(self, bot: discord.Client):
        self.bot = bot
        self.logger = logging.getLogger("red.ZeCogs.uploads_filter")
        self.check_configs()
        self.load_data()

    # Events
    async def on_message(self, message: discord.Message):
        with contextlib.suppress():
            await self._on_message(message)

    # Commands
    @commands.group(name="uploads_filter", pass_context=True, invoke_without_command=True)
    @checks.mod_or_permissions(manage_roles=True)
    async def _uploads_filter(self, ctx: Context):
        await self.bot.send_cmd_help(ctx)

    @_uploads_filter.command(name="allow", pass_context=True)
    @checks.mod_or_permissions(manage_roles=True)
    async def _uploads_allow(self, ctx: Context, channel: discord.Channel, filetype):
        """Allows a filetype to be uploaded to a channel"""
        config = self.get_config(channel)
        filetype = filetype.lower().lstrip(".")
        reply_channel = ctx.message.channel
        if channel.server is None:
            reply = self.CANT_SET_LIST_ON_PMS
        elif config["allow"] is False:
            reply = self.CANT_BE_WHITELIST_AND_BLACKLIST
        elif filetype in config["types"]:
            reply = self.ALREADY_WHITELISTED.format(filetype, reply_channel.id)
        else:
            config["allow"] = True
            config["types"].append(filetype)
            self.save_data()
            reply = self.FILETYPE_ALLOWED.format(filetype, reply_channel.id)
        await self.bot.send_message(reply_channel, reply)

    @_uploads_filter.command(name="deny", pass_context=True)
    @checks.mod_or_permissions(manage_roles=True)
    async def _uploads_deny(self, ctx: Context, channel: discord.Channel, filetype):
        """Prevents a filetype from being uploaded to a channel"""
        config = self.get_config(channel)
        filetype = filetype.lower().lstrip(".")
        reply_channel = ctx.message.channel
        if channel.server is None:
            reply = self.CANT_SET_LIST_ON_PMS
        elif config["allow"] is True:
            reply = self.CANT_BE_WHITELIST_AND_BLACKLIST
        elif filetype in config["types"]:
            reply = self.ALREADY_BLACKLISTED.format(filetype, reply_channel.id)
        else:
            config["allow"] = False
            config["types"].append(filetype)
            self.save_data()
            reply = self.FILETYPE_DENIED.format(filetype, reply_channel.id)
        await self.bot.send_message(reply_channel, reply)

    @_uploads_filter.command(name="remove", aliases=["del", "rm"], pass_context=True)
    @checks.mod_or_permissions(manage_roles=True)
    async def _uploads_remove(self, ctx: Context, channel: discord.Channel, filetype):
        """Removes a filetype's rule of being allowed or denied from a channel"""
        config = self.get_config(channel)
        filetype = filetype.lower().lstrip(".")
        reply_channel = ctx.message.channel
        if filetype not in config["types"]:
            reply = self.NOT_IN_LIST.format(filetype, reply_channel.id)
        else:
            config["types"].remove(filetype)
            if len(config["types"]) == 0:
                config["allow"] = None
            self.save_data()
            reply = self.FILETYPE_ALLOWED.format(filetype, reply_channel.id)
        await self.bot.send_message(reply_channel, reply)

    @_uploads_filter.command(name="clear", pass_context=True)
    @checks.mod_or_permissions(manage_roles=True)
    async def _uploads_clear(self, ctx: Context, channel: discord.Channel):
        """Removes all rules from a channel"""
        config = self.get_config(channel)
        reply_channel = ctx.message.channel
        config["types"].clear()
        config["allow"] = None
        self.save_data()
        reply = self.LIST_CLEARED.format(reply_channel.id)
        await self.bot.send_message(reply_channel, reply)

    @_uploads_filter.command(name="list", pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_roles=True)
    async def _uploads_list(self, ctx: Context):
        """Lists all rules on the current server"""
        server = ctx.message.server
        embed = discord.Embed(title="Rules for {}".format(server.name))
        for channel_id, config in self.config.items():
            channel = server.get_channel(channel_id)
            if channel is not None and len(config["types"]) > 0:
                embed.add_field(name=channel.name, value=", ".join("`.{}`".format(t) for t in config["types"]))
        if len(embed.fields) == 0:
            embed.description = "No rules here"
        await self.bot.send_message(ctx.message.channel, embed=embed)

    # Utilities
    async def _on_message(self, message: discord.Message):
        for attachment in message.attachments:
            config = self.config.get(message.channel.id)
            if config is not None \
                    and isinstance(message.author, discord.Member) \
                    and not self.is_member_staff(message.author):
                extension = os.path.splitext(attachment["filename"])[-1].lower().lstrip(".")
                list_type = config["allow"]
                allowed = True
                if list_type is True and extension not in config["types"]:
                    allowed = False
                elif list_type is False and extension in config["types"]:
                    allowed = False

                if allowed is False:
                    await self.bot.delete_message(message)
                    warning = self.FILETYPE_WARNING.format(extension, message.channel.id)
                    with contextlib.suppress(discord.Forbidden, discord.NotFound, discord.HTTPException):
                        await self.bot.send_message(message.author, warning)

    def is_member_staff(self, member: discord.Member):
        staff_role_names = (self.bot.settings.get_server_admin(member.server).lower(),
                            self.bot.settings.get_server_mod(member.server).lower())
        return any(r.name.lower() in staff_role_names for r in member.roles)

    # Config
    def get_config(self, channel: discord.Channel):
        return self.config.setdefault(channel.id, copy.deepcopy(self.CHANNEL_DEFAULT))

    def check_configs(self):
        self.check_folders()
        self.check_files()

    def check_folders(self):
        self.check_folder(self.DATA_FOLDER)

    def check_folder(self, name):
        if not os.path.exists(name):
            self.logger.debug("Creating " + name + " folder...")
            os.makedirs(name, exist_ok=True)

    def check_files(self):
        self.check_file(self.CONFIG_FILE_PATH, self.CONFIG_DEFAULT)

    def check_file(self, file, default):
        if not dataIO.is_valid_json(file):
            self.logger.debug("Creating empty " + file + "...")
            dataIO.save_json(file, default)

    def load_data(self):
        self.config = dataIO.load_json(self.CONFIG_FILE_PATH)

    def save_data(self):
        dataIO.save_json(self.CONFIG_FILE_PATH, self.config)


def setup(bot):
    bot.add_cog(UploadsFilter(bot))

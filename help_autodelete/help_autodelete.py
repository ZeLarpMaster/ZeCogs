import asyncio
import copy
import discord
import os
import os.path
import re

from discord.ext import commands
from .utils.dataIO import dataIO
from .utils import checks


class HelpAutoDelete:
    """Cog which allows you to set a timeout after which help messages delete themselves"""
    
    DATA_FOLDER = "data/client_modification"
    CONFIG_FILE_PATH = DATA_FOLDER + "/config.json"
    
    SERVER_DEFAULT = {"help_timeout": 0}
    CONFIG_DEFAULT = {}

    _MENTIONS_REPLACE = {
        '@everyone': '@\u200beveryone',
        '@here': '@\u200bhere'
    }
    _MENTION_PATTERN = re.compile('|'.join(_MENTIONS_REPLACE.keys()))
    
    def __init__(self, bot):
        self.bot = bot
        self.check_configs()
        self.load_data()
        asyncio.ensure_future(self._init_modifications())
    
    # Events
    async def _init_modifications(self):
        await self.bot.wait_until_ready()
        self._init_help_modif()
    
    def __unload(self):
        # This method is ran whenever the bot unloads this cog.
        self.revert_modifications()

    # Commands
    @commands.command(name="help_timeout", pass_context=True, no_pm=True)
    @checks.admin_or_permissions(manage_server=True)
    async def _set_help_timeout(self, ctx, timeout: float):
        """Sets the timeout for the help message in the current server"""
        if timeout >= 0:
            conf = self.get_config(ctx.message.server.id)
            conf["help_timeout"] = timeout
            self.save_data()
            if ctx.message.channel.permissions_for(ctx.message.channel.server.me).manage_messages:
                await self.bot.delete_message(ctx.message)
    
    # Utilities
    def _init_help_modif(self):
        self.__og_default_help_cmd = self.bot.commands["help"].callback
        self.__og_send_cmd_help = self.bot.send_cmd_help
        self.bot.commands["help"].callback = self._default_help_command
        self.bot.send_cmd_help = self.send_cmd_help

    async def send_cmd_help(self, ctx):  # Used users FailFish a command
        invoked_command = ctx.invoked_subcommand if ctx.invoked_subcommand else ctx.command
        pages = self.bot.formatter.format_help_for(ctx, invoked_command)
        await self.temp_send(ctx.message.channel, pages, [ctx.message])

    async def _default_help_command(self, ctx, *cmds: str):  # [p]help
        """Shows this message"""
        bot = ctx.bot
        destination = ctx.message.author if bot.pm_help else ctx.message.channel

        def repl(obj):
            return self._MENTIONS_REPLACE.get(obj.group(0), "")

        pages = None
        command = bot
        for key in cmds:
            name = self._MENTION_PATTERN.sub(repl, key)
            if name in bot.cogs:
                command = bot.cogs.get(name)
            elif isinstance(command, discord.ext.commands.GroupMixin):
                command = command.commands.get(name)
                if command is None:
                    pages = [bot.command_not_found.format(name)]
                    break
            else:
                pages = [bot.command_has_no_subcommands.format(command, name)]
                break
        if pages is None:
            pages = bot.formatter.format_help_for(ctx, command)

        if bot.pm_help is None:
            characters = sum(map(lambda l: len(l), pages))
            if characters > 1000:
                destination = ctx.message.author

        await self.temp_send(destination, pages, [ctx.message])  # All of that copy paste for this one line change
    
    def revert_modifications(self):
        self.bot.send_cmd_help = self.__og_send_cmd_help
        self.bot.commands["help"].callback = self.__og_default_help_cmd

    async def temp_send(self, channel, pages, msgs):
        for page in pages:
            msgs.append(await self.bot.send_message(channel, page))
        if self and hasattr(channel, "server"):
            config = self.get_config(channel.server.id)
            if config is not None:
                seconds = config.get("help_timeout", 0)
                if seconds > 0:
                    await asyncio.sleep(seconds)
                    await self.delete_messages(msgs)

    async def delete_messages(self, messages):
        while len(messages) > 0:
            if len(messages) == 1:
                await self.bot.delete_message(messages[0])
                messages = messages[:-1]
            else:
                await self.bot.delete_messages(messages[-100:])
                messages = messages[:-100]
    
    # Config
    def get_config(self, server_id):
        config = self.config.get(server_id)
        if config is None:
            config = copy.deepcopy(self.SERVER_DEFAULT)
            self.config[server_id] = config
        return self.config.get(server_id)
    
    def check_configs(self):
        self.check_folders()
        self.check_files()
    
    def check_folders(self):
        if not os.path.exists(self.DATA_FOLDER):
            print("Creating data folder...")
            os.makedirs(self.DATA_FOLDER, exist_ok=True)
    
    def check_files(self):
        self.check_file(self.CONFIG_FILE_PATH, self.CONFIG_DEFAULT)
    
    def check_file(self, file, default):
        if not dataIO.is_valid_json(file):
            print("Creating empty " + file + "...")
            dataIO.save_json(file, default)
    
    def load_data(self):
        # Here, you load the data from the config file.
        self.config = dataIO.load_json(self.CONFIG_FILE_PATH)
    
    def save_data(self):
        # Save all the data (if needed)
        dataIO.save_json(self.CONFIG_FILE_PATH, self.config)


def setup(bot):
    bot.add_cog(HelpAutoDelete(bot))

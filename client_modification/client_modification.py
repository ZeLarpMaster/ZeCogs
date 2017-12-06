import asyncio
import copy
import discord
import os
import os.path
import re

from discord.ext import commands
from .utils.dataIO import dataIO
from .utils import checks


class ClientModification:
    """Cog which provides endpoints to use modified Client features
    Usage example: bot.get_cog("ClientModification").add_cached_message(msg)
    
    Currently supports:
        - Adding messages to the client message cache
        - Setting a timeout for help messages

    This cog mostly exists because I was denied a change on discord.connection to allow adding messages into the cache.
    This could've been done cleanly by adding a side dictionary of messages cached by the client which can be
        manipulated by cogs through discord.Client, but I was told this change didn't have it's place in discord.py.

    We need to be able to add messages into the cache to listen to events on messages which weren't received while the
        bot was online. This is because discord.py doesn't throw events when they happen on an Object which isn't in
        cache. This is because Discord only sends the message id for events which happen on it so the library isn't
        able to send an event with a Message object. The developers decided they just wouldn't send the event if that
        happened. The only alternative they give us is on_socket_raw_receive, but that would me rewriting all the
        parsing internally in every cog which needs it and that's very redundant.

        As an example, if someone wanted to listen to reactions on a specific message which could've been posted months
        ago, you wouldn't be able to go grab the actual message object through endpoints and add it to the cache to then
        receive the events because Danny decided so. So here's a monkey patch to support it.

    If you want to fight for it to be added natively, go ahead. I failed at expressing the need for it when I tried.

    Changes like these are in this centralized cog to prevent conflicts when monkey patching.
        Basically to ensure `revert_modifications` doesn't remove another monkey patch which might've been done later"""
    
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
        self.cached_messages = {}
    
    # Events
    async def _init_modifications(self):
        await self.bot.wait_until_ready()
        self._init_message_modifs()
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

    # Endpoints
    def add_cached_messages(self, messages):
        self.cached_messages.update((m.id, m) for m in messages if isinstance(m, discord.Message))
    
    def add_cached_message(self, message):
        if isinstance(message, discord.Message):
            self.cached_messages[message.id] = message
    
    def remove_cached_message(self, message):
        if isinstance(message, discord.Message):
            if message.id in self.cached_messages:
                del self.cached_messages[message.id]
        elif isinstance(message, str):
            if message in self.cached_messages:
                del self.cached_messages[message]
    
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

    def _init_message_modifs(self):
        def _get_modified_message(message_id):
            message = None
            cm = self.bot.get_cog("ClientModification")
            # Checking if ClientModification is still loaded in case it was unloaded without reverting this
            if cm is not None:
                message = cm.cached_messages.get(message_id)
            return message or self.__og_get_message(message_id)
        self.__og_get_message = self.bot.connection._get_message
        self.bot.connection._get_message = _get_modified_message
    
    def revert_modifications(self):
        self.bot.connection._get_message = self.__og_get_message
        self.bot.send_cmd_help = self.__og_send_cmd_help
        self.bot.commands["help"].callback = self.__og_default_help_cmd

    async def temp_send(self, channel, pages, msgs):
        for page in pages:
            msgs.append(await self.bot.send_message(channel, page))
        if isinstance(channel, discord.Channel) and self:
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
    # Creating the cog
    c = ClientModification(bot)
    # Finally, add the cog to the bot.
    bot.add_cog(c)

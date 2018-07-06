import asyncio
import os.path
import os
import discord
import copy
import re
import io
import logging
import typing
import math

import aiohttp  # Comes with discord

from discord.ext import commands
from .utils import checks
from .utils.dataIO import dataIO

MessageList = typing.List[discord.Message]


class Welcome:
    """An utility cog which sends a welcome message in PMs to new users"""

    # File constants
    DATA_FOLDER = "data/welcome"
    DATA_FILE_PATH = DATA_FOLDER + "/config.json"

    # Config defaults
    CONFIG_DEFAULT = {}
    SERVER_DEFAULT = {"enabled": False, "image": None, "text": None}

    # Behavior constants
    TEMP_MESSAGE_TIMEOUT = 5  # seconds
    DOWNLOAD_TIMEOUT = 30  # seconds
    IMAGE_MAX_SIZE = 8388608  # bytes
    IMG_REGEX = re.compile("(https?://(\S+/)([\w\d/]+)).(jpg|jpeg|gif|png|bmp)\??(\S+)*", re.IGNORECASE)

    # Message constants
    REQUESTING_IMAGE = "Requesting the image..."
    INVALID_IMAGE_FORMAT = ":x: Invalid image url format."
    IMAGE_TOO_BIG = ":x: The image is too big. Limit: {}MB".format(math.floor(IMAGE_MAX_SIZE / (1024 * 1024)))
    IMAGE_SAVED = ":white_check_mark: Your welcome message's image has been saved."
    MESSAGE_OR_IMAGE_NEEDED = ":x: Your welcome message or your image must be set."
    IMAGE_DELETED = ":put_litter_in_its_place: The welcome message's image has been deleted."
    TEXT_SET = ":white_check_mark: The welcome message's text has been set."
    TEXT_DELETED = ":put_litter_in_its_place: The welcome message's text has been deleted."
    WELCOME_DISABLED = ":white_check_mark: The welcome message has been disabled on this server."
    NEED_SOMETHING_TO_ENABLE = ":x: Error. You must set the welcome message's text or image before enabling it."
    WELCOME_ENABLED = ":white_check_mark: The welcome message has been enabled on this server."
    
    def __init__(self, bot):
        self.bot = bot
        self.logger = logging.getLogger("red.ZeCogs.welcome")
        self.check_configs()
        self.load_data()
    
    # Events
    async def on_member_join(self, member):
        await self._welcome_member(member)
    
    # Commands
    @commands.group(pass_context=True, no_pm=True, invoke_without_command=True)
    @checks.mod_or_permissions(manage_roles=True)
    async def welcome(self, ctx):
        """Manage the welcoming message"""
        await self.bot.send_cmd_help(ctx)
    
    @welcome.command(name="test", pass_context=True, no_pm=True)
    async def welcome_test(self, ctx):
        """Tests the welcome message on yourself

        Will send the reason why it doesn't send the welcome message if it can't."""
        await self._welcome_member(ctx.message.author, debug=ctx.message.channel)
    
    @welcome.command(name="enable", pass_context=True, no_pm=True)
    async def welcome_enable(self, ctx):
        """Enables the welcome message on the current server

        Requires having at least text or an image set."""
        message = ctx.message
        msgs = [message]
        channel = message.channel
        conf = self.get_config(message.server.id)
        if conf.get("text") is None and conf.get("image") is None:
            await self.temp_send(channel, msgs, self.NEED_SOMETHING_TO_ENABLE)
        else:
            conf["enabled"] = True
            self.save_data()
            await self.temp_send(channel, msgs, self.WELCOME_ENABLED)
    
    @welcome.command(name="disable", pass_context=True, no_pm=True)
    async def welcome_disable(self, ctx):
        """Disables the welcome message on the current server"""
        message = ctx.message
        msgs = [message]
        conf = self.get_config(message.server.id)
        conf["enabled"] = False
        self.save_data()
        await self.temp_send(message.channel, msgs, self.WELCOME_DISABLED)
    
    @welcome.command(name="text", pass_context=True, no_pm=True)
    async def welcome_text(self, ctx, *, content=""):
        """Sets the welcome message's text to `content`"""
        message = ctx.message
        msgs = [message]
        channel = message.channel
        conf = self.get_config(message.server.id)
        if len(content) > 0:
            conf["text"] = content
            self.save_data()
            await self.temp_send(channel, msgs, self.TEXT_SET)
        elif conf.get("image") is None:
            await self.temp_send(channel, msgs, self.MESSAGE_OR_IMAGE_NEEDED)
        else:
            conf["text"] = None
            self.save_data()
            await self.temp_send(channel, msgs, self.TEXT_DELETED)
    
    @welcome.command(name="image", pass_context=True, no_pm=True)
    async def welcome_image(self, ctx, *, img_url=""):
        """Sets the welcome message's image to `img_url`"""
        message = ctx.message
        msgs = [message]
        server = message.server
        channel = message.channel
        conf = self.get_config(server.id)
        if len(img_url) > 0:
            msgs.append(await self.bot.send_message(channel, self.REQUESTING_IMAGE))
            img_result = await self.download_server_welcome(server.id, img_url)
            if img_result is None:
                await self.temp_send(channel, msgs, self.INVALID_IMAGE_FORMAT)
            elif img_result is False:
                await self.temp_send(channel, msgs, self.IMAGE_TOO_BIG)
            else:
                conf["image"] = img_result
                self.save_data()
                await self.temp_send(channel, msgs, self.IMAGE_SAVED)
        elif conf.get("text") is None:
            await self.temp_send(channel, msgs, self.MESSAGE_OR_IMAGE_NEEDED)
        else:
            conf["image"] = None
            self.save_data()
            await self.temp_send(channel, msgs, self.IMAGE_DELETED)
    
    # Utilities
    async def _welcome_member(self, member: discord.Member, *, debug: discord.Channel=None):
        """Send the welcome message to `member`

        If debug is given, it must be a discord.Channel"""
        server = member.server
        conf = self.get_config(server.id)
        if conf.get("enabled", False) is True:
            img_path = conf.get("image")
            text = conf.get("text")
            if img_path is not None:
                try:
                    with open(img_path, "br") as img:
                        await self.bot.send_file(member, img, content=text)
                except discord.errors.Forbidden:
                    self.logger.warning("Failed to send the welcome message to {}.".format(member))
            elif text is not None:
                try:
                    await self.bot.send_message(member, text)
                except discord.errors.Forbidden:
                    self.logger.warning("Failed to send the welcome message to {}.".format(member))
            elif debug is not None:
                await self.bot.send_message(debug, "There is no image or text set on that server.")
        elif debug is not None:
            await self.bot.send_message(debug, "The server is disabled.")

    async def download_image_async(self, url, *, max_length=None):
        """Downloads the content of "url" into a BytesIO object asynchronously"""
        content = None
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=self.DOWNLOAD_TIMEOUT, headers={"User-Agent": "Mozilla"}) as response:
                if max_length is None or response.headers.get("Content-Length", 0) < max_length:
                    content = io.BytesIO(await response.read())
                    content.seek(0)
        return content

    async def download_server_welcome(self, server_id, url):
        img_match = self.IMG_REGEX.fullmatch(url)
        result = None
        if img_match is not None:
            content = await self.download_image_async(url)
            if content is not None:
                img_path = self.DATA_FOLDER + "/" + server_id + "." + img_match.group(4)
                with open(img_path, "bw") as img_file:
                    img_file.write(content)
                result = img_path
            else:
                result = False
        return result

    async def temp_send(self, channel: discord.Channel, messages: MessageList, *args, **kwargs):
        """Sends a message with *args **kwargs in `channel` and deletes it after some time

        If sleep_timeout is given as a named parameter (in kwargs), uses it
        Else it defaults to TEMP_MESSAGE_TIMEOUT

        Deletes all messages in `messages` if we have the manage_messages perms
        Else, deletes only the sent message"""
        sleep_timeout = kwargs.pop("sleep_timeout", self.TEMP_MESSAGE_TIMEOUT)
        messages.append(await self.bot.send_message(channel, *args, **kwargs))
        await asyncio.sleep(sleep_timeout)
        await self.delete_messages(messages)

    async def delete_messages(self, messages: MessageList):
        """Deletes an arbitrary number of messages by batches

        Basically runs discord.Client.delete_messages for every 100 messages until none are left"""
        messages = list(filter(self.message_filter, messages))
        while len(messages) > 0:
            if len(messages) == 1:
                await self.bot.delete_message(messages.pop())
            else:
                await self.bot.delete_messages(messages[-100:])
                messages = messages[:-100]

    def message_filter(self, message: discord.Message):
        result = False
        channel = message.channel
        if not channel.is_private:
            if channel.permissions_for(channel.server.me).manage_messages:
                result = True
        return result
            
    # Config
    def get_config(self, server_id):
        if server_id not in self.config:
            self.config[server_id] = copy.deepcopy(self.CONFIG_DEFAULT)
        return self.config.get(server_id)
    
    def check_configs(self):
        self.check_folders()
        self.check_files()
    
    def check_folders(self):
        if not os.path.exists(self.DATA_FOLDER):
            print("Creating data folder...")
            os.makedirs(self.DATA_FOLDER, exist_ok=True)
    
    def check_files(self):
        self.check_file(self.DATA_FILE_PATH, self.CONFIG_DEFAULT)
    
    def check_file(self, file, default):
        if not dataIO.is_valid_json(file):
            print("Creating empty " + file + "...")
            dataIO.save_json(file, default)
    
    def load_data(self):
        self.config = dataIO.load_json(self.DATA_FILE_PATH)
    
    def save_data(self):
        dataIO.save_json(self.DATA_FILE_PATH, self.config)


def setup(bot):
    bot.add_cog(Welcome(bot))

import asyncio
import discord
import os.path
import os
import logging
import re
import contextlib

from typing import List

from discord.ext import commands
from discord.ext.commands.context import Context
from .utils.dataIO import dataIO
from .utils import checks


class EmbedReactor:
    """Reacts to embeds in specific channels"""

    DATA_FOLDER = "data/embed_reactor"
    CONFIG_FILE_PATH = DATA_FOLDER + "/config.json"

    CONFIG_DEFAULT = {}

    EMOTE_REGEX = re.compile("<a?:[a-zA-Z0-9_]{2,32}:(\d{1,20})>")
    URL_REGEX = re.compile("<?(https?|ftp)://[^\s/$.?#].[^\s]*>?")

    REMOVED_CHANNEL_REACTOR = ":put_litter_in_its_place: Successfully removed the reactor from {}."
    INVALID_EMOTES = ":x: The following emotes are invalid: {}."
    SET_CHANNEL_REACTOR = ":white_check_mark: Successfully set the channel reactor for {} to {}."
    LACKING_PERMISSIONS = ":x: I don't have the permission to add reactions in that channel."
    LACKING_PERMISSIONS_TO_TEST = ":x: I can't test the existence of those emotes because I can't add reactions here."
    MUST_BE_SERVER_CHANNEL = ":x: The channel must be in a server."

    def __init__(self, bot: discord.Client):
        self.bot = bot
        self.logger = logging.getLogger("red.ZeCogs.embed_reactor")
        self.check_configs()
        self.load_data()
        self.emote_cache = {}
        self.preprocessed_config = {}
        asyncio.ensure_future(self.initialize())

    # Events
    async def initialize(self):
        await self.bot.wait_until_ready()
        for server in self.bot.servers:
            for emote in server.emojis:
                self.emote_cache[emote.id] = emote

        for channel_id, reactions in self.config.items():
            channel_cache = self.preprocessed_config.setdefault(channel_id, [])
            for reaction in reactions:
                channel_cache.append(self.find_emote(reaction) or reaction)

    async def on_message(self, message: discord.Message):
        reactions = self.preprocessed_config.get(message.channel.id)
        if reactions is not None:
            match = self.URL_REGEX.search(message.content)
            if len(message.attachments) > 0 or (match and not match.group(0).startswith("<") and not match.group(0).endswith(">")):
                for reaction in reactions:
                    with contextlib.suppress(Exception):
                        await self.bot.add_reaction(message, reaction)

    async def on_server_emojis_update(self, before: List[discord.Emoji], after: List[discord.Emoji]):
        after = {e.id: e for e in after}
        before_ids, after_ids = set(e.id for e in before), set(after)
        for i in before_ids - after_ids:
            del self.emote_cache[i]
        for i in after_ids - before_ids:
            self.emote_cache[i] = after[i]

    # Commands
    @commands.command(name="embed_reactor", pass_context=True)
    @checks.mod_or_permissions(manage_channels=True)
    async def _embed_reactor(self, ctx: Context, channel: discord.Channel, *reactions):
        """Sets the reactions added to embeds in a channel"""
        message = ctx.message
        invalid_emotes = []
        for emote in reactions:
            if await self.is_valid_emote(emote, message) is False:
                invalid_emotes.append(emote)
        if len(reactions) == 0:
            self.config.pop(channel.id, None)
            self.preprocessed_config.pop(channel.id, None)
            response = self.REMOVED_CHANNEL_REACTOR.format(channel.mention)
        elif channel.server is None:
            response = self.MUST_BE_SERVER_CHANNEL
        elif not channel.permissions_for(channel.server.me).add_reactions:
            response = self.LACKING_PERMISSIONS
        elif len(invalid_emotes) > 0:
            if message.channel.server is not None and not channel.permissions_for(channel.server.me).add_reactions:
                response = self.LACKING_PERMISSIONS_TO_TEST
            else:
                response = self.INVALID_EMOTES.format(", ".join(invalid_emotes))
        else:
            self.config[channel.id] = reactions
            self.save_data()
            self.preprocessed_config[channel.id] = [self.find_emote(emote) or emote for emote in reactions]
            response = self.SET_CHANNEL_REACTOR.format(channel.mention, ", ".join(reactions))
        await self.bot.send_message(message.channel, response)

    # Utilities
    async def is_valid_emote(self, emote: str, message: discord.Message) -> bool:
        emote_match = self.EMOTE_REGEX.fullmatch(emote)
        emote_id = emote if emote_match is None else emote_match.group(1)
        server_emote = self.find_emote(emote_id)
        try:
            await self.bot.add_reaction(message, server_emote or emote_id)
        except discord.HTTPException:  # Failed to find the emoji
            result = False
        else:
            await self.bot.remove_reaction(message, server_emote or emote_id, self.bot.user)
            result = True
        return result

    def find_emote(self, emote: str):
        return self.emote_cache.get(emote)

    # Config
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
    bot.add_cog(EmbedReactor(bot))

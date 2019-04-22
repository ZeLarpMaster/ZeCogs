import asyncio
import discord
import os.path
import os
import logging
import typing

from discord.ext import commands
from .utils import checks
from .utils.dataIO import dataIO

MessageList = typing.List[discord.Message]


class VoiceLock:
    """Allows users to lock the voice channel they're in until they leave or unlock it"""

    # File related constants
    DATA_FOLDER = "data/voice_lock"
    DATA_FILE_PATH = DATA_FOLDER + "/config.json"

    # Configuration default
    CONFIG_DEFAULT = {"locks": {}, "not_lockable": [], "exclusivities": {}}

    # Behavior constants
    TEMP_MESSAGE_TIMEOUT = 15

    # Message constants
    NOT_IN_CHANNEL_MSG = ":x: You must be in a voice channel to do that."
    CHANNEL_ALREADY_LOCKED = ":x: {} is already locked."
    CHANNEL_NOT_LOCKABLE = ":x: {} cannot be locked."
    CHANNEL_LOCKED = """:lock: {channel} has been locked.
You can now use `{p}voice permit @user` to allow the user to join you or `{p}voice unlock` to completely unlock it."""
    NOT_A_VOICE_CHANNEL = ":x: Error: {} is not a voice channel."
    CHANNEL_NOT_LOCKED = ":x: Error: {} is not locked."
    CHANNEL_UNLOCKED = ":unlock: {} has been unlocked."
    USER_PERMITTED = ":inbox_tray: {user} has been allowed in {channel}."
    NOW_LOCKABLE = ":white_check_mark: {} can now be locked."
    NOW_NOT_LOCKABLE = ":negative_squared_cross_mark: {} can no longer be locked."
    NOT_A_TEXT_CHANNEL = ":x: Error: {} is not a text channel."
    EXCLUSIVITY_SET = ":white_check_mark: The exclusivity for **{s}** has been set to **{c}**."
    WRONG_CHANNEL = ":x: Wrong channel. Please do that in <#{}>."
    EXCLUSIVITY_RESET = ":put_litter_in_its_place: The exclusivity for **{}** has been removed."
    
    def __init__(self, bot):
        self.bot = bot
        self.logger = logging.getLogger("red.ZeCogs.voice_lock")
        self.check_configs()
        self.load_data()
        self.cant_connect_perms = discord.PermissionOverwrite(connect=False)
        self.can_connect_perms = discord.PermissionOverwrite(connect=True)
        asyncio.ensure_future(self.initialize())
    
    # Events
    async def on_voice_state_update(self, before: discord.Member, after: discord.Member):
        channel = before.voice_channel
        if channel is not None and channel != after.voice_channel and channel.id in self.config["locks"]:
            if before.id == self.config["locks"][channel.id]["who_locked"]:
                await self.unlock_channel(channel, before)
                self.save_data()
            else:
                try:
                    await self.bot.delete_channel_permissions(channel, before)
                except discord.NotFound:
                    self.logger.warning("Couldn't find the channel from which permissions are deleted. Ignoring.")
        elif after.voice_channel is not None and channel != after.voice_channel \
                and after.voice_channel.id in self.config["locks"] \
                and after.id in self.config["locks"][after.voice_channel.id]["permits"]:
            self.config["locks"][after.voice_channel.id]["permits"].remove(after.id)
    
    async def initialize(self):
        await self.bot.wait_until_ready()
        await self.verify_locked_channels()
    
    # Commands
    @commands.group(pass_context=True, no_pm=True, invoke_without_command=True)
    async def voice(self, ctx):
        """Voice lock commands"""
        await self.bot.send_cmd_help(ctx)
    
    @voice.command(name="lock", pass_context=True)
    async def _voice_lock(self, ctx):
        """Locks the voice channel you're in"""
        message = ctx.message
        channel = message.channel
        server = message.server
        exclusive = self.config.get("exclusivities", {}).get(server.id)
        if exclusive is not None and exclusive != channel.id:
            response = self.WRONG_CHANNEL.format(exclusive)
        else:
            who_locked = message.author
            vc = who_locked.voice_channel
            if vc is None:
                response = self.NOT_IN_CHANNEL_MSG
            else:
                if vc.id in self.config["locks"]:
                    response = self.CHANNEL_ALREADY_LOCKED.format(vc.name)
                elif vc.id in self.config["not_lockable"]:
                    response = self.CHANNEL_NOT_LOCKABLE.format(vc.name)
                else:
                    await self.lock_channel(vc, who_locked)
                    self.save_data()
                    response = self.CHANNEL_LOCKED.format(channel=vc.name, p=ctx.prefix)
        await self.temp_send(channel, [message], response)

    @voice.command(name="unlock", pass_context=True)
    async def _voice_unlock(self, ctx):
        """Unlocks the voice channel you're in"""
        message = ctx.message
        author = message.author
        channel = message.channel
        server = message.server
        exclusive = self.config.get("exclusivities", {}).get(server.id)
        if exclusive is not None and exclusive != channel.id:
            response = self.WRONG_CHANNEL.format(exclusive)
        else:
            if author.voice_channel is None:
                response = self.NOT_IN_CHANNEL_MSG
            else:
                channel = author.voice_channel
                if channel.id not in self.config["locks"]:
                    response = self.CHANNEL_NOT_LOCKED.format(channel.name)
                else:
                    await self.unlock_channel(channel)
                    self.save_data()
                    response = self.CHANNEL_UNLOCKED.format(channel.name)
        await self.temp_send(message.channel, [message], response)
    
    @voice.command(name="force_unlock", pass_context=True)
    @checks.mod_or_permissions(manage_channels=True)
    async def _voice_force_unlock(self, ctx, *, channel: discord.Channel):
        """Forcefully unlocks a locked voice channel"""
        if channel.type != discord.ChannelType.voice:
            response = self.NOT_A_VOICE_CHANNEL.format(channel.name)
        elif channel.id not in self.config["locks"]:
            response = self.CHANNEL_NOT_LOCKED.format(channel.name)
        else:
            await self.unlock_channel(channel)
            self.save_data()
            response = self.CHANNEL_UNLOCKED.format(channel.name)
        await self.temp_send(ctx.message.channel, [ctx.message], response)
    
    @voice.command(name="permit", pass_context=True)
    async def _voice_permit(self, ctx, *, user: discord.Member):
        """Permits someone to join your locked voice channel"""
        message = ctx.message
        author = message.author
        if author.voice_channel is None:
            response = self.NOT_IN_CHANNEL_MSG
        else:
            channel = author.voice_channel
            if channel.id not in self.config["locks"]:
                response = self.CHANNEL_NOT_LOCKED.format(channel.name)
            else:
                await self.permit_user(channel, user)
                self.save_data()
                response = self.USER_PERMITTED.format(channel=channel.name, user=user.name)
        await self.temp_send(message.channel, [message], response)
    
    @voice.command(name="not_lockable", pass_context=True)
    @checks.mod_or_permissions(manage_channels=True)
    async def _voice_not_lockable(self, ctx, *, channel: discord.Channel):
        """Toggles the not lockable state of a channel"""
        if channel.type != discord.ChannelType.voice:
            response = self.NOT_A_VOICE_CHANNEL.format(channel.name)
        else:
            if channel.id in self.config["not_lockable"]:
                self.config["not_lockable"].remove(channel.id)
                response = self.NOW_LOCKABLE.format(channel.name)
            else:
                self.config["not_lockable"].append(channel.id)
                response = self.NOW_NOT_LOCKABLE.format(channel.name)
            self.save_data()
        await self.temp_send(ctx.message.channel, [ctx.message], response)
    
    @voice.command(name="set_exclusive", pass_context=True)
    @checks.mod_or_permissions(manage_roles=True)
    async def _voice_set_exclusivity(self, ctx, *, channel: discord.Channel=None):
        """Sets the channel where the voice commands should be done

        if `channel` isn't given, removes the exclusive channel"""
        reply_channel = ctx.message.channel
        server = reply_channel.server
        if channel is None:
            self.config["exclusivities"][server.id] = None
            self.save_data()
            response = self.EXCLUSIVITY_RESET.format(server.name)
        elif channel.type != discord.ChannelType.text:
            response = self.NOT_A_TEXT_CHANNEL.format(channel.name)
        else:
            self.config["exclusivities"][server.id] = channel.id
            self.save_data()
            response = self.EXCLUSIVITY_SET.format(c=channel.name, s=server.name)
        await self.temp_send(reply_channel, [ctx.message], response)
    
    # Utilities
    async def lock_channel(self, channel, who_locked):
        for member in channel.voice_members:
            await self.bot.edit_channel_permissions(channel, member, self.can_connect_perms)
        default_role = channel.server.default_role
        self.config["locks"][channel.id] = {"who_locked": who_locked.id, "permits": [],
                                            "previous_perm": channel.overwrites_for(default_role).connect}
        await self.bot.edit_channel_permissions(channel, default_role, self.cant_connect_perms)
    
    async def unlock_channel(self, channel, *additionnal_members):
        config = self.config["locks"][channel.id]
        permits = [channel.server.get_member(u_id) for u_id in config["permits"]]
        default_perm = discord.PermissionOverwrite(connect=config["previous_perm"])
        try:
            await self.bot.edit_channel_permissions(channel, channel.server.default_role, default_perm)
            for member in channel.voice_members + permits + list(additionnal_members):
                if member is not None:
                    await self.bot.delete_channel_permissions(channel, member)
        except discord.NotFound:
            self.logger.warning("Could not find the channel while unlocking.")
        del self.config["locks"][channel.id]
    
    async def permit_user(self, channel, member):
        self.config["locks"][channel.id]["permits"].append(member.id)
        await self.bot.edit_channel_permissions(channel, member, self.can_connect_perms)
    
    async def verify_locked_channels(self):
        for channel_id in list(self.config["locks"].keys()):
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                del self.config["locks"][channel_id]
                self.logger.debug("Could not find the channel with id {}. "
                                  "Removing it from the locked channels".format(channel_id))
            else:
                who_locked_id = self.config["locks"][channel_id]["who_locked"]
                who_locked = discord.utils.get(channel.voice_members, id=who_locked_id)
                if who_locked is None:
                    who_locked_mem = channel.server.get_member(who_locked_id)
                    additionnal_people = [] if who_locked_mem is None else [who_locked_mem]
                    await self.unlock_channel(channel, *additionnal_people)

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
    def check_configs(self):
        self.check_folders()
        self.check_files()
    
    def check_folders(self):
        if not os.path.exists(self.DATA_FOLDER):
            self.logger.debug("Creating data folder...")
            os.makedirs(self.DATA_FOLDER, exist_ok=True)
    
    def check_files(self):
        self.check_file(self.DATA_FILE_PATH, self.CONFIG_DEFAULT)
    
    def check_file(self, file, default):
        if not dataIO.is_valid_json(file):
            self.logger.debug("Creating empty " + file + "...")
            dataIO.save_json(file, default)
    
    def load_data(self):
        # Here, you load the data from the config file.
        self.config = dataIO.load_json(self.DATA_FILE_PATH)
    
    def save_data(self):
        # Save all the data (if needed)
        dataIO.save_json(self.DATA_FILE_PATH, self.config)


def setup(bot):
    bot.add_cog(VoiceLock(bot))

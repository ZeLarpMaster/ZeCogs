import asyncio
import discord
import os.path
import os
import re
import copy
import discord.http
import contextlib
import logging

from discord.ext import commands
from .utils import checks
from .utils.dataIO import dataIO


class VoiceChannelGenerator:
    """Utilities to manage voice channel generators.

Generators create/delete voice channel to fit a configurable number of channels."""
    
    DATA_FOLDER = "data/voice_channel_gen"
    DATA_FILE_PATH = DATA_FOLDER + "/config_v2.json"
    
    CONFIG_DEFAULT = {}
    SERVER_DEFAULT = {"voice_chat_formats": {}, "afk_at_bottom": False, "delay": 0.2}
    CHAT_FORMAT_DEFAULT = {"empty_voice_channels": 2, "max_channels": 25, "default_permissions": [], "parent": None,
                           "user_limit": 0}
    
    NUMBER_REGEX = "(?P<number>\d+)"  # Just a fancy way of matching 1+ digits
    
    INVALID_CHANNEL_FORMAT_MSG = ":x: Error: Invalid channel name. It must contain {} exactly once."
    CHANNEL_FORMAT_EXISTS_MSG = ":x: Error: Channel generator already exists."
    ADDED_VOICE_FORMAT_MSG = ":white_check_mark: Added new channel generator."
    INVALID_CHANNEL_ID_MSG = ":x: Error: Could not find the channel on this server."
    INVALID_CHANNEL_TYPE_MSG = ":x: Error: The channel is not a voice channel."
    SET_PERMS_MSG = ":white_check_mark: The permissions for generated channels on this server have been set."
    CHANNEL_FORMAT_NOT_FOUND_MSG = ":x: Error: Could not find the channel generator."
    CHANNEL_DELETE_CONFIRM_MSG = "Do you also want to delete the associated channels? (yes/no)"
    CHANNEL_NOT_DELETE_MSG = ":negative_squared_cross_mark: Not deleting the channels."
    CHANNEL_DELETING_MSG = ":put_litter_in_its_place: Deleting the channels."
    CHANNEL_DELETED_MSG = ":white_check_mark: Deleted the channels."
    CHANNEL_EDIT_CONFIRM_MSG = "Do you also want to modify the existing channels? (yes/no)"
    CHANNEL_NOT_EDIT_MSG = ":negative_squared_cross_mark: Not modifying the existing channels."
    CHANNEL_EDITING_MSG = ":pencil: Modifying the existing channels' permissions."
    CHANNEL_EDITED_MSG = ":white_check_mark: Modified the existing channels."
    CONFIG_TITLE_FORMAT = "Configuration for {server.name}"
    CONFIG_DESC_FORMAT = """There is **{nb_chats}** on the server.
The AFK channel **{afk_bot}** be stuck to the bottom.
The delay for this server is **{delay}**."""
    CONFIG_GENERATOR_FORMAT = """User limit: {}
Empty channels: {}
Max channels: {}
Permissions: {}"""
    CHANNEL_FORMAT_DELETED_MSG = ":put_litter_in_its_place: Removed the channel generator."
    CONFIG_SET_MSG = """Configurations:
    **empty_channels** --> **Integer**, how many empty channels per generator (can't exceed 25)
    **max_channels** --> **Integer**, maximum amount of channels per generator (can't exceed 25)
    **afk_bottom** --> **Boolean**, whether or not the afk channel should be at the bottom
    **delay** --> **Float**, how many seconds of delay before moving channels
    **user_limit** --> **Integer**, number of users allowed in generated channels (can't exceed 99; 0 for no limit)

Changes to any of those values will only apply to newly created/deleted channels. \
It will **not** change existing channels.

**Boolean values** are considered **True** for: `yes`, `y`, `1`, `true`
and are considered **False** for: `no`, `n`, `0`, `false`
**Anything else won't do anything.**"""
    CONFIG_VALUE_MISSING_MSG = "You must specify a new configuration value.\n" \
                               "Give no *config_name* to see specifications."
    CONFIG_NOT_FOUND_MSG = ":x: Error: Configuration not found."
    INVALID_CONFIG_VALUE_MSG = ":x: Error: Invalid configuration value."
    CONFIGURATION_CHANGED_MSG = ":white_check_mark: The configuration has been set."
    CONFIG_GENERATOR_MISSING_MSG = ":x: You must provide a generator for that configuration."
    CONFIG_GENERATOR_NOT_NEEDED_MSG = ":x: You can't provide a generator for that configuration."
    GENERATOR_NOT_FOUND_MSG = ":x: That generator was not found in the current server."

    # TODO: Clean it all (or rewrite, might be faster)

    def __init__(self, bot: discord.Client):
        self.bot = bot
        self.logger = logging.getLogger("red.ZeCogs.voice_channel_generator")
        self.check_configs()
        self.load_data()
        self.temp_events = {}
        asyncio.ensure_future(self._init_perms())
    
    # Events
    async def on_voice_state_update(self, before: discord.Member, after: discord.Member):
        with contextlib.suppress():
            before_channel = before.voice.voice_channel
            after_channel = after.voice.voice_channel
            if before_channel != after_channel:
                server = after.server if after.server is not None else before.server
                if server is not None and server.id in self.config:
                    server_conf = self.config[server.id]
                    after_name = "" if after_channel is None else after_channel.name
                    before_name = "" if before_channel is None else before_channel.name
                    for voice_format, format_config in server_conf["voice_chat_formats"].items():
                        reg = re.compile(voice_format.format(self.NUMBER_REGEX))
                        if reg.fullmatch(after_name) or reg.fullmatch(before_name):
                            await self.check_channels(server, voice_format, format_config, server_conf)
                    await asyncio.sleep(self.config[server.id]["delay"])
                    await self.check_afk_channel(server)
    
    async def _init_perms(self):
        """Initializes the permissions from the raw configuration when the bot is ready"""
        await self.bot.wait_until_ready()
        self.perms_overwrites = {}
        for s_id, configs in self.config.items():
            server = self.bot.get_server(s_id)
            if server is not None:
                server_perms = self.perms_overwrites.setdefault(s_id, {})
                for channel_format, channel_config in configs["voice_chat_formats"].items():
                    format_perms = server_perms.setdefault(channel_format, [])
                    raw_perms = channel_config["default_permissions"]
                    for pairs in raw_perms:
                        r_id, perms = pairs
                        role = discord.utils.get(server.roles, id=r_id)
                        allow_perms = discord.Permissions(permissions=perms[0])
                        deny_perms = discord.Permissions(permissions=perms[1])
                        perms_overwrite = discord.PermissionOverwrite.from_pair(allow_perms, deny_perms)
                        format_perms.append((role, perms_overwrite))
    
    # Commands
    @commands.group(name="voice_gen", pass_context=True, invoke_without_command=True)
    @checks.mod_or_permissions(manage_channels=True)
    async def _voice_gen(self, ctx):
        """Voice channel generators manager"""
        await self.bot.send_cmd_help(ctx)
    
    @_voice_gen.command(name="reset")
    @checks.mod_or_permissions(manage_channels=True)
    async def _voice_gen_reset(self):
        """Resets the listener for the channel generation"""
        # Basically call [p]reload on self
        await self.bot.get_command("reload").callback(self.bot.get_cog("Owner"), "cogs.voice_channel_generator")
    
    @_voice_gen.command(name="set", pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_channels=True)
    async def _voice_gen_set_configs(self, ctx, config_name=None, config_value=None, *, generator=None):
        """Sets a configuration value
        If config_name is omitted, shows specifications.
        If config_name is given, config_value must also be given.
        Some configurations are specific to a generator. Those will require the generator to be given."""
        server = ctx.message.server
        server_config = self.get_server_config(server)
        voice_formats = server_config["voice_chat_formats"]
        if config_name is None:
            await self.bot.say(self.CONFIG_SET_MSG)
        elif config_value is None:
            await self.bot.say(self.CONFIG_VALUE_MISSING_MSG)
        elif generator is None and config_name.lower() not in ("afk_bottom", "delay"):
            await self.bot.say(self.CONFIG_GENERATOR_MISSING_MSG)
        elif generator is not None and config_name.lower() in ("afk_bottom", "delay"):
            await self.bot.say(self.CONFIG_GENERATOR_NOT_NEEDED_MSG)
        elif generator is not None and generator not in voice_formats:
            await self.bot.say(self.GENERATOR_NOT_FOUND_MSG)
        else:
            config_name = config_name.lower()
            config_value = config_value.lower()
            key = None
            value = None
            gen_conf = None
            # Parse the configs
            if config_name == "empty_channels":  # TODO: Oh my god spare me from this ugliness
                key = "empty_voice_channels"
                if config_value.isdigit():
                    temp_value = int(config_value)
                    if 0 < temp_value <= 25:
                        value = temp_value
                        gen_conf = voice_formats.get(generator)  # TODO: :facepalm:
            elif config_name == "max_channels":
                key = "max_channels"
                if config_value.isdigit():
                    temp_value = int(config_value)
                    if 0 < temp_value <= 25:
                        value = temp_value
                        gen_conf = voice_formats.get(generator)
            elif config_name == "afk_bottom":
                key = "afk_at_bottom"
                if config_value in ["yes", "y", "1", "true"]:
                    value = True
                elif config_value in ["no", "n", "0", "false"]:
                    value = False
            elif config_name == "delay":
                key = "delay"
                temp_value = self.parse_float(config_value)
                if temp_value is not None:
                    if 0 <= temp_value <= 25:
                        value = temp_value
            elif config_name == "user_limit":
                key = "user_limit"
                if config_value.isdigit():
                    temp_value = int(config_value)
                    if 0 <= temp_value < 100:
                        value = temp_value
                        gen_conf = voice_formats.get(generator)
            else:
                await self.bot.say(self.CONFIG_NOT_FOUND_MSG)
            # Set the configs
            if key is not None:
                if value is None:
                    await self.bot.say(self.INVALID_CONFIG_VALUE_MSG)
                else:
                    (gen_conf or server_config)[key] = value
                    self.save_data()
                    await self.bot.say(self.CONFIGURATION_CHANGED_MSG)
    
    @_voice_gen.command(name="get", pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_channels=True)
    async def _voice_gen_get_configs(self, ctx):
        """Shows the current configuration"""
        server = ctx.message.server
        config = self.get_server_config(server)
        server_overwrites = self.perms_overwrites.get(server.id, {})
        nb_chats = self.plural_format(len(config["voice_chat_formats"]), "{} generators")
        afk_bot_str = "will" if config["afk_at_bottom"] else "won't"
        delay_str = self.plural_format(config["delay"], "{} seconds")
        embed = discord.Embed(title=self.CONFIG_TITLE_FORMAT.format(server=server),
                              colour=discord.Colour.gold(),
                              description=self.CONFIG_DESC_FORMAT.format(nb_chats=nb_chats,
                                                                         afk_bot=afk_bot_str,
                                                                         delay=delay_str))
        for channel_format, channel_config in config["voice_chat_formats"].items():
            def_perms = server_overwrites.get(channel_format, [])
            def_perms = [pair[0].name for pair in def_perms]
            channel_config_str = self.CONFIG_GENERATOR_FORMAT.format(channel_config["user_limit"],
                                                                     channel_config["empty_voice_channels"],
                                                                     channel_config["max_channels"],
                                                                     ", ".join(def_perms))
            embed.add_field(name=channel_format, value=channel_config_str)
        await self.bot.say(embed=embed)
    
    @_voice_gen.command(name="set_perms", pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_channels=True)
    async def _voice_gen_set_perms(self, ctx, channel: discord.Channel, *, generator: str):
        """Set a generator's channels' permissions

        Uses the given channel's current permissions
        You can give the channel by its name. It must by surrounded by "double quotes" if it contains a space."""
        message = ctx.message
        server = message.server
        chat_formats = self.get_server_config(server)["voice_chat_formats"]
        generator_config = chat_formats.get(generator)
        if channel.type != discord.ChannelType.voice:
            await self.bot.say(self.INVALID_CHANNEL_TYPE_MSG)
        elif generator_config is None:
            await self.bot.say(self.GENERATOR_NOT_FOUND_MSG)
        else:
            role_ows = [o for o in channel.overwrites if isinstance(o[0], discord.Role)]
            self.perms_overwrites.setdefault(server.id, {})[generator] = role_ows
            perms_list = []
            for ow in role_ows:
                perms_list.append((ow[0].id, [i.value for i in ow[1].pair()]))
            generator_config["default_permissions"] = perms_list
            self.save_data()
            # Ask before setting to existing channels
            messages = [message]
            messages.append(await self.bot.say(self.CHANNEL_EDIT_CONFIRM_MSG))
            # TODO: WET af
            answer = await self.bot.wait_for_message(timeout=30, author=message.author, channel=message.channel,
                                                     check=lambda m: m.content.lower() in ["yes", "no", "y", "n"])
            messages.append(answer)
            if answer is None or answer.content.lower() in ["no", "n"]:
                messages.append(await self.bot.say(self.CHANNEL_NOT_EDIT_MSG))
            else:
                messages.append(await self.bot.say(self.CHANNEL_EDITING_MSG))
                await self.set_channels_perms(server, generator, role_ows)
                await asyncio.sleep(0.5)  # TODO: Eww magic number
                await self.bot.edit_message(messages[-1], self.CHANNEL_EDITED_MSG)
            await asyncio.sleep(3)
            await self.bot.delete_messages(messages)
            await self.bot.say(self.SET_PERMS_MSG)
    
    @_voice_gen.command(name="add", pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_channels=True)
    async def _voice_gen_add(self, ctx, parent_id: str, *, generator: str):
        """Add a new voice channel generator

        parent_id must be the id of a channel category; if no channel is found, channels will be outside all categories
        The generator must contain "{}" exactly once. Examples: `Voice Chat #{}`, `Channel {}/20` or `Casual {}`
        It will be replaced by a number starting at 1 and going up."""
        server = ctx.message.server
        server_config = self.get_server_config(server)
        voice_formats = server_config["voice_chat_formats"]
        if not self.test_generator(generator):
            await self.bot.say(self.INVALID_CHANNEL_FORMAT_MSG)
        elif generator.lower() in list(map(lambda s: s.lower(), voice_formats.keys())):
            await self.bot.say(self.CHANNEL_FORMAT_EXISTS_MSG)
        else:
            generator_config = voice_formats.setdefault(generator, copy.deepcopy(self.CHAT_FORMAT_DEFAULT))
            channel = self.bot.get_channel(parent_id)
            if channel is None or self.is_channel_not_category(channel):
                parent_id = None
            generator_config["parent"] = parent_id
            self.save_data()
            await self.bot.say(self.ADDED_VOICE_FORMAT_MSG)
            await self.check_channels(server, generator, generator_config, server_config)
            await asyncio.sleep(self.config[server.id]["delay"])
            await self.check_afk_channel(server)
    
    @_voice_gen.command(name="delete", aliases=["del"], pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_channels=True)
    async def _voice_gen_delete(self, ctx, *, generator: str):
        """Delete a voice channel generator

        Asks if you want to delete the channels generated by it."""
        message = ctx.message
        server = message.server
        voice_formats = self.get_server_config(server)["voice_chat_formats"]
        if generator not in voice_formats:
            await self.bot.say(self.CHANNEL_FORMAT_NOT_FOUND_MSG)
        else:
            del voice_formats[generator]
            self.save_data()
            await self.bot.say(self.CHANNEL_FORMAT_DELETED_MSG)
            messages = [message]
            messages.append(await self.bot.say(self.CHANNEL_DELETE_CONFIRM_MSG))
            answer = await self.bot.wait_for_message(timeout=30, author=message.author, channel=message.channel,
                                                     check=lambda m: m.content.lower() in ["yes", "no", "y", "n"])
            messages.append(answer)
            if answer is None or answer.content.lower() in ["no", "n"]:
                messages.append(await self.bot.say(self.CHANNEL_NOT_DELETE_MSG))
            else:
                messages.append(await self.bot.say(self.CHANNEL_DELETING_MSG))
                await self.delete_channels(server, generator)
                # Best case scenario would be waiting until the bot got the information back from the server
                await asyncio.sleep(0.5)  # Completely arbitrary, but works (could prob use config["delay"] though)
                await self.bot.edit_message(messages[-1], self.CHANNEL_DELETED_MSG)
                self.update_channels_position(server)
            await asyncio.sleep(3)
            await self.bot.delete_messages(messages)
    
    # Utilities
    async def check_channels(self, server, channel_format, channel_config, config):
        """Checks for channels following the channel_format.
        If None are found, create them."""
        await self.bot.wait_until_ready()
        if channel_format not in self.temp_events:
            self.temp_events[channel_format] = False
        # Prevent collisions between two simultaneous executions of this event routine on the same generator
        if self.temp_events[channel_format] is False:
            self.temp_events[channel_format] = True
            empty_voice_channels = channel_config["empty_voice_channels"]
            regex = re.compile(channel_format.format(self.NUMBER_REGEX))
            channel_ids = {}
            channels_without_people = []
            # This is the type of thing which should be supported directly in discord.py, but w/e
            # I just copy pasted this line in a bunch of places until it worked
            # I have no idea why it works; Might not be needed anymore
            self.update_channels_position(server)
            for channel in filter(lambda c: c.type == discord.ChannelType.voice, server.channels):
                match = regex.fullmatch(channel.name)
                if match is not None:
                    channel_number = int(match.group("number"))
                    channel_ids[channel_number] = channel
                    if len(channel.voice_members) == 0:
                        channels_without_people.append(channel_number)

            while len(channels_without_people) != empty_voice_channels \
                    and len(channels_without_people) < channel_config["max_channels"] - 1:
                if len(channels_without_people) > empty_voice_channels:  # Delete biggest channel(s)
                    channel_id = max(channels_without_people)
                    try:
                        await self.bot.delete_channel(channel_ids[channel_id])
                    except discord.errors.NotFound:
                        pass  # Only happens if the channel was deleted *just before* we deleted it
                    channels_without_people.remove(channel_id)
                    del channel_ids[channel_id]
                elif len(channels_without_people) < empty_voice_channels:  # Create new channel(s)
                    # Fetch one of the missing numbers from the resulting set
                    missing_number = min(set(range(1, channel_config["max_channels"] + 1)) - set(channel_ids))
                    ows = self.perms_overwrites.get(server.id, {}).get(channel_format, [])
                    chann = await self.create_channel(server, channel_format.format(missing_number), *ows,
                                                      parent_id=channel_config["parent"],
                                                      user_limit=channel_config["user_limit"])
                    if len(channel_ids) > 0:
                        await asyncio.sleep(config["delay"])
                        self.update_channels_position(server)
                        expected_position = channel_ids[missing_number - 1].position + 1  # Next available position
                        if chann.position != expected_position:
                            await self.bot.move_channel(chann, expected_position)
                    channels_without_people.append(missing_number)
                    channel_ids[missing_number] = chann
                self.update_channels_position(server)
            self.temp_events[channel_format] = False
    
    async def delete_channels(self, server, channel_format):
        """Deletes all voice channels on `server` corresponding to the `channel_format`"""
        regex = re.compile(channel_format.format(self.NUMBER_REGEX))
        for channel in list(server.channels):  # Making a copy because it's gonna modify in the loop
            if channel.type == discord.ChannelType.voice:
                match = regex.fullmatch(channel.name)
                if match is not None:
                    await self.bot.delete_channel(channel)
    
    async def set_channels_perms(self, server, generator, perms):
        """Edits all channels' permissions to the given one if they fit a channel format"""
        regex = re.compile(generator.format(self.NUMBER_REGEX))
        for channel in [c for c in server.channels if c.type == discord.ChannelType.voice]:
            match = regex.fullmatch(channel.name)
            if match is not None:
                for pairs in perms:
                    await self.bot.edit_channel_permissions(channel, pairs[0], pairs[1])
                for channel_ow in channel.overwrites:
                    target = channel_ow[0]
                    if isinstance(target, discord.Role) and target.id not in map(lambda p: p[0].id, perms):
                        await self.bot.delete_channel_permissions(channel, target)
    
    async def check_afk_channel(self, server):
        """Checks for an afk channel on `server` and moves it at the end if needed and asked"""
        voice_channels = [c for c in server.channels if c.type == discord.ChannelType.voice]
        if self.config[server.id]["afk_at_bottom"] and server.afk_channel is not None:
            await self.bot.move_channel(server.afk_channel, len(voice_channels) - 1)

    async def create_channel(self, server, name, *overwrites, parent_id, user_limit: int=0):
        """d.py 0.16 recipe for creating a channel including category support"""
        payload = {
            "name": name,
            "type": 2,
            "user_limit": user_limit
        }
        if parent_id is not None:
            payload["parent_id"] = parent_id
        if len(overwrites) > 0:
            perms = []
            for overwrite in overwrites:
                target, perm = overwrite
                allow, deny = perm.pair()
                perms.append({"allow": allow.value, "deny": deny.value,
                              "id": target.id, "type": type(target).__name__.lower()})
            payload["permission_overwrites"] = perms

        data = await self.bot.http.request(discord.http.Route('POST', '/guilds/{guild_id}/channels',
                                                              guild_id=server.id), json=payload)
        return discord.Channel(server=server, **data)

    def test_generator(self, generator: str):
        try:
            generator.format(1)
            return True
        except IndexError:
            return False

    def update_channels_position(self, server, channel_type=discord.ChannelType.voice):
        """Puts all channels in `server` of type `channel_type`'s position back in order"""
        channels = sorted(filter(lambda c: c.type == channel_type, server.channels), key=lambda c: c.position)
        for i, channel in enumerate(channels):
            channel.position = i
    
    def get_server_config(self, server):
        return self.config.setdefault(server.id, copy.deepcopy(self.SERVER_DEFAULT))

    def is_channel_not_category(self, channel):
        if hasattr(discord.ChannelType, "category"):
            result = channel.type != discord.ChannelType["category"]
        else:
            result = channel.type != 4
        return result
    
    def parse_float(self, float_str):
        try:
            result = float(float_str)  # No easier way to do this :(
        except ValueError:
            result = None
        return result
    
    def plural_format(self, amount, format_string):
        """Takes away the last char in format_string before doing .format if amount == 1"""
        return format_string.format(amount)[:-1 if amount == 1 else None]
    
    # Config
    def check_configs(self):
        self.check_folders()
        self.check_files()
    
    def check_folders(self):
        if not os.path.exists(self.DATA_FOLDER):
            self.logger.info("Creating data folder...")
            os.makedirs(self.DATA_FOLDER, exist_ok=True)
    
    def check_files(self):
        self.check_file(self.DATA_FILE_PATH, self.CONFIG_DEFAULT)
    
    def check_file(self, file, default):
        if not dataIO.is_valid_json(file):
            self.logger.info("Creating empty " + file + "...")
            dataIO.save_json(file, default)
    
    def load_data(self):
        self.config = dataIO.load_json(self.DATA_FILE_PATH)
    
    def save_data(self):
        dataIO.save_json(self.DATA_FILE_PATH, self.config)


def setup(bot):
    bot.add_cog(VoiceChannelGenerator(bot))

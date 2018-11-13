import discord
import os.path
import os
import logging
import asyncio
import collections
import random

from discord.ext import commands
from discord.ext.commands import Context

from .utils.dataIO import dataIO
from .utils import checks


def escape(text):
    return text.replace("@", "@\u200b")


class Periodic:
    """Sends messages periodically"""

    DATA_FOLDER = "data/periodic"
    CONFIG_FILE_PATH = DATA_FOLDER + "/config.json"

    CONFIG_DEFAULT = {}
    """
    {
        server.id: {
            channel.id: {
                "messages": [
                    {
                        "type": "message" | "customcommand",
                        "value": "content" | "command",
                    }
                ],
                "cursor": int,
                "time_interval": int,  # seconds
                "message_interval": int,  # messages
                "last_sent_id": str,
            }
        }
    }
    """

    TEMP_MESSAGE_TIMEOUT = 30
    LOOP_DELETE_TIMEOUT = 1
    CUSTOMCOM_PREFIX = "\u200b"

    INTERVAL_POSITIVE = ":x: The given interval(s) must be positive!"
    AT_LEAST_ONE_INTERVAL = ":x: At least one interval must not be 0!"
    CANT_BE_PMS = ":x: The channel must be in a server!"
    ALREADY_EXISTS = ":x: That channel already has a periodic action!"
    CREATED = ":white_check_mark: The periodic action for {channel} has been created."
    DOESNT_EXIST = ":x: That channel doesn't have periodic actions! Use `{p}periodic create` to create it."
    ADDED_MESSAGE = ":white_check_mark: The message has been added!"
    CUSTOM_COMMAND_ADD_FOOTER = "\n**Note**: If the custom command doesn't exist when it's attempted to be sent, " \
                                "the bot will just ignore it and try another message."
    NOTHING_IN_THAT_CHANNEL = ":x: There is no periodic action in that channel."
    SHUFFLED_MESSAGES = "🔀 The messages have been shuffled."
    SUCCESSFULLY_DELETED = ":put_litter_in_its_place: That channel's periodic actions have been deleted."

    LIST_PREV_PAGE = "⬅"
    LIST_NEXT_PAGE = "➡"
    LIST_DELETE = "🚮"
    LIST_STOP = "🛑"
    LIST_DELETE_AFFIRM = "✅"
    LIST_DELETE_CANCEL = "❌"
    LIST_REACTIONS = LIST_PREV_PAGE, LIST_NEXT_PAGE, LIST_STOP, LIST_DELETE
    LIST_TITLE = "Periodic Actions List (#{} out of {})"
    LIST_DESCRIPTION = "**Type**: {type}\n**Content**: {value}"
    LIST_DELETE_CONFIRM = "Are you sure you want to delete the action #{}?"
    LIST_DELETED = "The action #{} has been deleted!"
    LIST_DELETE_CANCELED = "Cancelled."

    def __init__(self, bot: discord.Client):
        self.bot = bot
        self.logger = logging.getLogger("red.ZeCogs.periodic")
        self.check_configs()
        self.load_data()
        self.type_map = {"message": self.bot.send_message, "customcommand": self.send_customcom}
        self.channel_events = {}  # channel.id: asyncio.Event
        self.channel_loops = {}  # channel.id: asyncio.Future
        self.channel_triggers = {}  # channel.id: [asyncio.Future]
        self.channel_messages = collections.Counter()  # channel.id: count (decrements from message_interval to 0)
        asyncio.ensure_future(self.initialize())

    # Events
    async def initialize(self):
        await self.bot.wait_until_ready()
        for server_id, server_conf in self.config.items():
            server = self.bot.get_server(server_id)
            if server is not None:
                for channel_id, config in server_conf.items():
                    channel = server.get_channel(channel_id)
                    if channel is not None:
                        self.start_triggers(config, channel)

    async def wait_for_channel(self, channel: discord.Channel):
        while channel.id in self.channel_events:
            await self.channel_events[channel.id].wait()
            if channel.id in self.channel_events:
                self.stop_triggers(channel.id)
                config = self.get_config(channel.server.id, channel.id)
                messages = config["messages"]
                cursor = (config["cursor"] + len(messages)) % len(messages)
                og_cursor = cursor
                last_sent_id = config.get("last_sent_id")
                if last_sent_id is not None:
                    try:
                        msg = await self.bot.get_message(channel, last_sent_id)
                        await self.bot.delete_message(msg)
                    except discord.errors.DiscordException:
                        pass
                output = None
                while output is None:
                    chosen_one = messages[cursor]
                    consumer = self.type_map[chosen_one["type"]]
                    output = await consumer(channel, chosen_one["value"])
                    cursor = (cursor + 1) % len(messages)
                    if cursor == og_cursor:  # Gone full circle
                        output = output or False
                if isinstance(output, discord.Message):
                    config["last_sent_id"] = output.id
                config["cursor"] = cursor
                self.save_data()
                self.start_triggers(config, channel)

    async def on_message(self, message: discord.Message):
        channel_id = message.channel.id
        if channel_id in self.channel_messages:
            self.channel_messages[channel_id] -= 1
            if self.channel_messages[channel_id] <= 0:
                event = self.channel_events.get(channel_id)
                if event is not None:
                    event.set()

    def __unload(self):
        for channel_id in self.channel_loops:
            asyncio.ensure_future(self.stop_loop(channel_id))

    # Commands
    @commands.group(pass_context=True, invoke_without_command=True, no_pm=True)
    @checks.mod_or_permissions(manage_channels=True)
    async def periodic(self, ctx: Context):
        """Commands to configure the periodic actions"""
        await self.bot.send_cmd_help(ctx)

    @periodic.command(name="delete", aliases=["remove", "del"], pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_channels=True)
    async def periodic_delete(self, ctx: Context, channel: discord.Channel):
        """Deletes a channel's periodic actions

        Note: This completely removes a channel, not just one of its actions"""
        config = self.get_config(channel.server.id, channel.id)
        if config is None:
            response = self.NOTHING_IN_THAT_CHANNEL
        else:
            response = self.SUCCESSFULLY_DELETED
            del self.channel_messages[channel.id]
            del self.config[channel.server.id][channel.id]
            await self.stop_loop(channel.id)
            self.save_data()
        await self.bot.send_message(ctx.message.channel, response)

    @periodic.command(name="create", pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_channels=True)
    async def periodic_create(self, ctx: Context, channel: discord.Channel, time_interval: int, message_interval: int):
        """Creates a periodic action in a channel

        time_interval is the amount of time in between new posts (if zero (0), message_interval must be given)
        message_interval is the number of messages in between posts (if zero (0), time_interval must be given)

        Which ever comes first (time or messages) will trigger a post"""
        server = ctx.message.channel.server
        if time_interval < 0 or message_interval < 0:
            response = self.INTERVAL_POSITIVE
        elif time_interval == 0 and message_interval == 0:
            response = self.AT_LEAST_ONE_INTERVAL
        elif channel.server is None:
            response = self.CANT_BE_PMS
        elif self.get_config(server.id, channel.id) is not None:
            response = self.ALREADY_EXISTS
        else:
            config = self.create_periodic(server.id, channel.id)
            config["time_interval"] = time_interval
            config["message_interval"] = message_interval
            config["cursor"] = 0
            config["last_sent_id"] = None
            self.save_data()
            response = self.CREATED.format(channel=channel.mention)
        await self.bot.send_message(ctx.message.channel, response)

    @periodic.command(name="add_message", aliases=["add_m", "a_m"], pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_channels=True)
    async def periodic_add_message(self, ctx: Context, channel: discord.Channel, *, content):
        """Adds the message to the channel's periodic actions"""
        reply_channel = ctx.message.channel
        config = self.get_config(reply_channel.server.id, channel.id)
        if config is None:
            response = self.DOESNT_EXIST.format(p=ctx.prefix)
        else:
            config["messages"].append({"type": "message", "value": escape(content)})
            self.save_data()
            if len(config["messages"]) == 1:
                self.start_triggers(config, channel)
            response = self.ADDED_MESSAGE
        await self.bot.send_message(reply_channel, response)

    @periodic.command(name="add_command", aliases=["add_c", "a_c"], pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_channels=True)
    async def periodic_add_command(self, ctx: Context, channel: discord.Channel, *, command):
        """Adds the customcommand to the channel's periodic actions"""
        reply_channel = ctx.message.channel
        config = self.get_config(reply_channel.server.id, channel.id)
        if config is None:
            response = self.DOESNT_EXIST.format(p=ctx.prefix)
        else:
            config["messages"].append({"type": "customcommand", "value": command})
            self.save_data()
            if len(config["messages"]) == 1:
                self.start_triggers(config, channel)
            response = self.ADDED_MESSAGE + self.CUSTOM_COMMAND_ADD_FOOTER
        await self.bot.send_message(reply_channel, response)

    @periodic.command(name="shuffle", pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_channels=True)
    async def periodic_shuffle(self, ctx: Context, channel: discord.Channel):
        """Shuffles a channel's periodic actions"""
        reply_channel = ctx.message.channel
        config = self.get_config(reply_channel.server.id, channel.id)
        if config is None:
            response = self.DOESNT_EXIST.format(p=ctx.prefix)
        else:
            random.shuffle(config["messages"])
            self.save_data()
            response = self.SHUFFLED_MESSAGES
        await self.bot.send_message(reply_channel, response)

    @periodic.command(name="list", pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_channels=True)
    async def periodic_list(self, ctx: Context, channel: discord.Channel):
        """Lists and allows you to delete entries from a channel's periodic actions"""
        reply_channel = ctx.message.channel
        author = ctx.message.author
        config = self.get_config(reply_channel.server.id, channel.id)
        if config is None or len(config["messages"]) == 0:
            await self.bot.send_message(reply_channel, self.NOTHING_IN_THAT_CHANNEL)
        else:
            messages = config["messages"]
            current = 0
            embed = discord.Embed(colour=discord.Colour.light_grey(), title="Processing...")
            msg = await self.bot.send_message(reply_channel, embed=embed)
            for e in self.LIST_REACTIONS:
                asyncio.ensure_future(self.bot.add_reaction(msg, e))
            while current >= 0 and len(messages) > 0:
                pages = len(messages)
                current %= pages
                embed.title = self.LIST_TITLE.format(current, pages)
                embed.description = self.LIST_DESCRIPTION.format(**messages[current])
                await self.bot.edit_message(msg, embed=embed)
                r, _ = await self.bot.wait_for_reaction(self.LIST_REACTIONS, user=author, message=msg)
                asyncio.ensure_future(self.bot.remove_reaction(msg, r.emoji, author))
                if r.emoji == self.LIST_PREV_PAGE:
                    current += pages - 1
                elif r.emoji == self.LIST_NEXT_PAGE:
                    current += 1
                elif r.emoji == self.LIST_STOP:
                    current = -1
                elif r.emoji == self.LIST_DELETE:
                    confirm_msg = await self.bot.send_message(reply_channel, self.LIST_DELETE_CONFIRM.format(current))
                    asyncio.ensure_future(self.bot.add_reaction(confirm_msg, self.LIST_DELETE_AFFIRM))
                    asyncio.ensure_future(self.bot.add_reaction(confirm_msg, self.LIST_DELETE_CANCEL))
                    r, _ = await self.bot.wait_for_reaction((self.LIST_DELETE_AFFIRM, self.LIST_DELETE_CANCEL),
                                                            user=author, message=confirm_msg)
                    asyncio.ensure_future(self.bot.delete_message(confirm_msg))
                    if r.emoji == self.LIST_DELETE_AFFIRM:
                        asyncio.ensure_future(self.temp_send(reply_channel, self.LIST_DELETED.format(current)))
                        del messages[current]
                        self.save_data()
                        if len(messages) == 0:
                            await self.stop_loop(channel.id)
                    elif r.emoji == self.LIST_DELETE_CANCEL:
                        asyncio.ensure_future(self.temp_send(reply_channel, self.LIST_DELETE_CANCELED))
            await self.bot.delete_message(msg)

    # Utilities
    async def send_customcom(self, channel: discord.Channel, command_name: str):
        customcom = self.bot.get_cog("CustomCommands")
        cmds = customcom.c_commands.get(channel.server.id, {})
        cmd = cmds.get(command_name) or cmds.get(command_name.lower())
        return cmd and await self.bot.send_message(channel, self.CUSTOMCOM_PREFIX + escape(cmd))

    def start_triggers(self, config: dict, channel: discord.Channel):
        event = self.channel_events.get(channel.id) or asyncio.Event()
        triggers = self.channel_triggers.setdefault(channel.id, [])
        if config.get("time_interval", 0) > 0:
            triggers.append(asyncio.ensure_future(self.call_later(config["time_interval"], event.set)))
        if config.get("message_interval", 0) > 0:
            self.channel_messages[channel.id] = config["message_interval"]
        if channel.id not in self.channel_events:
            self.channel_events[channel.id] = event
        else:
            event.clear()
        if channel.id not in self.channel_loops:
            self.channel_loops[channel.id] = asyncio.ensure_future(self.wait_for_channel(channel))

    def stop_triggers(self, channel_id: str):
        triggers = self.channel_triggers.get(channel_id, [])
        while len(triggers) > 0:
            task = triggers.pop()
            if not task.done():
                task.cancel()

    async def stop_loop(self, channel_id: str):
        self.stop_triggers(channel_id)
        event = self.channel_events.pop(channel_id, None)
        loop = self.channel_loops.pop(channel_id, None)
        if event is not None:
            event.set()  # Gracefully stops the waiter
        if loop is not None:
            try:
                await asyncio.wait_for(loop, self.LOOP_DELETE_TIMEOUT)
            except asyncio.TimeoutError:
                self.logger.info("Had to forcefully cancel the waiter for {} when deleting".format(channel_id))

    async def call_later(self, time_to_sleep: float, func):
        await asyncio.sleep(time_to_sleep)
        func()

    async def temp_send(self, channel: discord.Channel, *args, **kwargs):
        """Sends a message with *args **kwargs in `channel` and deletes it after some time

        If sleep_timeout is given as a named parameter (in kwargs), uses it
        Else it defaults to TEMP_MESSAGE_TIMEOUT"""
        sleep_timeout = kwargs.pop("sleep_timeout", self.TEMP_MESSAGE_TIMEOUT)
        message = await self.bot.send_message(channel, *args, **kwargs)
        await asyncio.sleep(sleep_timeout)
        await self.bot.delete_message(message)

    def create_periodic(self, server_id: str, channel_id: str) -> dict:
        return self.config.setdefault(server_id, {}).setdefault(channel_id, {"messages": []})

    def get_config(self, server_id: str, channel_id: str) -> dict:
        return self.config.setdefault(server_id, {}).get(channel_id)

    # Config
    def check_configs(self):
        self.check_folders()
        self.check_files()

    def check_folders(self):
        self.check_folder(self.DATA_FOLDER)

    def check_folder(self, name: str):
        if not os.path.exists(name):
            self.logger.debug("Creating " + name + " folder...")
            os.makedirs(name, exist_ok=True)

    def check_files(self):
        self.check_file(self.CONFIG_FILE_PATH, self.CONFIG_DEFAULT)

    def check_file(self, file: str, default: dict):
        if not dataIO.is_valid_json(file):
            self.logger.debug("Creating empty " + file + "...")
            dataIO.save_json(file, default)

    def load_data(self):
        self.config = dataIO.load_json(self.CONFIG_FILE_PATH)

    def save_data(self):
        dataIO.save_json(self.CONFIG_FILE_PATH, self.config)


def setup(bot):
    bot.add_cog(Periodic(bot))

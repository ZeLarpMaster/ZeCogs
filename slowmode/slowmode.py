import asyncio
import discord
import os.path
import os
import datetime
import copy
import contextlib
import logging
import typing

from discord.ext import commands
from .cogs.utils import pagify
from .utils import checks
from .utils.dataIO import dataIO
from asyncio.futures import CancelledError


class SlowMode:  # TODO: Rewrite the whole thing. No easier way out of this.
    """Prevent people from sending messages too fast in channels"""
    
    DATA_FOLDER = "data/slowmode"
    DATA_FILE_PATH = DATA_FOLDER + "/channels.json"
    TEMP_TASKS_FILE = DATA_FOLDER + "/tasks.json"
    WELCOMED_USERS_FILE = DATA_FOLDER + "/welcomed.json"
    
    DEFAULT_TEMP_TASKS = {}
    DEFAULT_WELCOMED_USERS = {}
    
    DEFAULT_SLOWMODES = {}
    DEFAULT_SLOWMODE = {"time": 0, "messages": 0, "overwrites": {}, "max_time": 0, "unstoppable_roles": []}
    
    TIME_FORMATS = ["{} seconds", "{} minutes", "{} hours", "{} days", "{} weeks"]
    TIME_FRACTIONS = [60, 60, 24, 7]
    
    SLOWMODE_FORMAT = """The slowmode for {channel} is: **{time}**, **{msgs}**, and **{max_time}**."""
    SLOWMODE_HELP_FORMAT = """Thank you for using {channel}! 

The spam rules for this channel are automatically moderated by {me}, in order to \
reduce spam and to make this channel easier to read.

For you, it means the following:
After sending a message, you won't be able to send any other messages, until \
{time} {time_have} passed **and** {messages} {messages_have} been sent by other users.

{max_time_format}Thank you for reading! Here's a cookie: 🍪"""
    SLOWMODE_MAX_TIME_FORMAT = """If there aren't enough messages sent by other users after a maximum of {max_time}, \
you will be able to send messages again. Keep in mind that your last message will \
get deleted **if** it's within the last {messages}. Don't worry, this won't get you a warning.\n\n"""
    MISSING_MANAGE_PERMISSIONS = "\nI do not have the permission to 'Manage Permissions' on that channel " \
                                 "or 'Manage Roles' in the server. Without it, I cannot prevent people from talking " \
                                 "while they are slowed."
    INVALID_ROLE_OR_SERVER = ":x: The given role and channel don't share the same server."
    UNSLOWABLE_SET = ":white_check_mark: **{role}** is now unslowable in {channel}."
    UNSLOWABLE_LIST_TITLE = "List of unslowable roles in #{channel}."
    ALREADY_UNSLOWABLE = ":x: **{}** is already unslowable."
    ALREADY_SLOWABLE = ":x: **{}** is already slowable."
    SLOWABLE_SET = ":white_check_mark: **{role}** is now slowable in {channel}."
    NO_SLOWMODE_IN_CHANNEL = ":x: There is no slowmode in {}."
    ALREADY_UNSLOWABLE_IN_ALL_CHANNELS = "Nothing changed, **{role}** was already unslowable in all of your slowmodes"
    
    def __init__(self, bot: discord.Client):
        self.bot = bot
        self.logger = logging.getLogger("red.ZeCogs.slowmode")
        self.check_configs()
        self.load_data()
        self.message_trackers = {}
        self.tasks = []
        asyncio.ensure_future(self.check_temp_tasks())
    
    # Events
    async def on_message(self, message):
        if message.channel.id in self.slowmodes:
            channel = message.channel
            slowmode = self.get_channel_slowmode(channel)
            author = message.author
            self.increment_message_counts(channel)
            if not author.bot and not self.check_mod_or_admin(author, *slowmode.get("unstoppable_roles", [])):
                await self.check_for_welcome(author, channel)
                if slowmode["time"] > 0 or slowmode["messages"] > 0:
                    if slowmode["messages"] > 0 and channel.id in self.message_trackers \
                            and author.id in self.message_trackers[channel.id]:
                        await self._delete_last_from(channel, author, slowmode["messages"], message)
                    perms = channel.overwrites_for(author)
                    perms.send_messages = False
                    await self.bot.edit_channel_permissions(channel, author, perms)
                    if channel.id not in self.temp_tasks:
                        self.temp_tasks[channel.id] = []
                    self.temp_tasks[channel.id].append(author.id)
                    if channel.id not in self.message_trackers:
                        self.message_trackers[channel.id] = {}
                    event = asyncio.Event()
                    if slowmode["messages"] == 0:
                        event.set()
                    self.message_trackers[channel.id][author.id] = {"msg_count": 0, "event": event}
                    overwrite = slowmode["overwrites"].get(author.id)
                    self.tasks.append(asyncio.ensure_future(
                                                    self.unmute_user_later(channel, author,
                                                                           slowmode["time"], overwrite)))
                    if slowmode.get("max_time", 0) > 0:
                        self.tasks.append(asyncio.ensure_future(self.cancel_later(self.tasks[-1],
                                                                                  slowmode["max_time"])))
                    self.save_temp_tasks()
    
    def __unload(self):  # Called when the cog is `!unload`ed
        self.save_temp_tasks()
    
    async def check_temp_tasks(self):
        await self.bot.wait_until_ready()
        ded_channels = []
        for channel_id, members_list in self.temp_tasks.items():
            channel = self.bot.get_channel(channel_id)
            if channel is not None:
                slowmode = self.get_channel_slowmode(channel)
                for member_id in members_list:
                    member = channel.server.get_member(member_id)
                    if member is not None:
                        overwrite = slowmode["overwrites"].get(member_id)
                        time = await self.get_member_last_msg_time(channel, member)
                        self.tasks.append(asyncio.ensure_future(self.unmute_user_later(channel, member, max(0, time),
                                                                                       overwrite)))
                        if slowmode.get("max_time", 0) > 0:
                            cancel_time = max(0, slowmode["max_time"] - time)
                            self.tasks.append(asyncio.ensure_future(self.cancel_later(self.tasks[-1], cancel_time)))
            else:
                ded_channels.append(channel_id)
        for c_id in ded_channels:
            del self.temp_tasks[c_id]
    
    # Commands
    @commands.command(pass_context=True)
    @checks.mod_or_permissions(manage_channels=True)
    async def slowmode(self, ctx, channel: discord.Channel=None,
                       time: int=None, messages: int=None, max_time: int=None):
        """Sends the current slowmode of a channel. Default: Current channel
        If `time` is specified, changes the slowmode time to the given one.
        If `messages` is specified, changes the slowmode messages to the given one.
        If `max_time` is specified, changes the slowmode maximum muted time to the given one.
        Sets the slowmode for a `channel` to `time` seconds, `messages` messages, and `max_time` seconds maximum.
        If the `time` is 0, `messages` is 0, and `max_time` is 0, it disables the slowmode in that channel."""
        if channel is None:
            channel = ctx.message.channel
        if time is None and messages is None and max_time is None:
            # Show current slowmode
            slowmode = self.get_channel_slowmode(channel)
            if slowmode["time"] == 0 and slowmode["messages"] == 0 and slowmode["max_time"] == 0:
                await self.bot.say("**There is no slowmode in {}.**".format(channel.mention))
            else:
                await self.bot.say(self.get_slowmode_msg(channel, slowmode))
        else:
            # Update current slowmode
            new_slowmode = copy.deepcopy(self.DEFAULT_SLOWMODE)
            if time is not None:
                new_slowmode["time"] = time
            if messages is not None:
                new_slowmode["messages"] = messages
            if max_time is not None:
                new_slowmode["max_time"] = max_time
            if new_slowmode["time"] == 0 and new_slowmode["messages"] == 0 and new_slowmode["max_time"] == 0:
                del self.slowmodes[channel.id]["unstoppable_roles"]
                del self.slowmodes[channel.id]["overwrites"]
                del self.slowmodes[channel.id]
                await self.bot.say(":put_litter_in_its_place: Slowmode in {} deleted.".format(channel.mention))
            else:
                if channel.id not in self.slowmodes:
                    # Gather the overwrites
                    member_overwrites = list(filter(lambda o: isinstance(o[0], discord.Member), channel.overwrites))
                    new_slowmode["overwrites"].update(map(lambda o: (o[0].id, o[1].send_messages), member_overwrites))
                else:
                    new_slowmode["overwrites"] = copy.deepcopy(self.slowmodes[channel.id]["overwrites"])
                    new_slowmode["unstoppable_roles"] = self.slowmodes[channel.id].get("unstoppable_roles", [])
                self.slowmodes[channel.id] = new_slowmode
                can_manage = channel.permissions_for(channel.server.me).manage_roles
                response = ":white_check_mark: Slowmode updated.\n" + self.get_slowmode_msg(channel, new_slowmode)
                await self.bot.say(response + ("" if can_manage else self.MISSING_MANAGE_PERMISSIONS))
            self.save_data()

    @commands.group(name="check_slow", pass_context=True, invoke_without_command=True)
    @checks.mod_or_permissions(manage_channels=True)
    async def check_user_slowmode(self, ctx, channel: discord.Channel):
        """Checks and fixes the slowmode for all users in a channel"""
        unmuting, muting = await self.check_channel(channel)
        result = "**Unmuting**: " + ", ".join(u.name for u in unmuting) + \
                 "\n**Should be muted**: " + ", ".join(u.name for u in muting)
        overwrites = self.get_channel_slowmode(channel).get("overwrites", {})
        for member in unmuting:
            asyncio.ensure_future(self.unmute_user(channel, member, overwrites.get(member.id)))
        for page in pagify(result, delims=[", "], shorten_by=16):
            await self.bot.send_message(ctx.message.channel, page)

    @check_user_slowmode.command(name="all", pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_channels=True)
    async def check_all_slows(self, ctx):
        """Checks and fixes all slowmodes in the server"""
        msg_channel = ctx.message.channel
        for c_id, slowmode in self.slowmodes.items():
            channel = msg_channel.server.get_channel(c_id)
            if channel is not None:
                unmuting, muting = await self.check_channel(channel, slowmode)
                result = "--- {} --- \n**Unmuting**: {}\n**Should be muted**: {}".format(
                    channel.mention, ", ".join(u.name for u in unmuting), ", ".join(u.name for u in muting))
                overwrites = slowmode.get("overwrites", {})
                for member in unmuting:
                    asyncio.ensure_future(self.unmute_user(channel, member, overwrites.get(member.id)))
                for page in pagify(result, delims=[", "], shorten_by=16):
                    await self.bot.send_message(msg_channel, page)

    @commands.group(name="unslowable", pass_context=True, no_pm=True, invoke_without_command=True)
    @checks.mod_or_permissions(manage_roles=True)
    async def unslowable(self, ctx, channel: discord.Channel, *, role: discord.Role):
        """Add a role to the list of roles which can't be slowed in the given channel"""
        if role.server != channel.server:
            response = self.INVALID_ROLE_OR_SERVER
        else:
            slowmode = self.get_channel_slowmode(channel)
            if channel.id not in self.slowmodes:
                response = self.NO_SLOWMODE_IN_CHANNEL.format(channel.mention)
            elif role.id in slowmode.get("unstoppable_roles", []):
                response = self.ALREADY_UNSLOWABLE.format(role.name)
            else:
                slowmode.setdefault("unstoppable_roles", []).append(role.id)
                self.save_data()
                response = self.UNSLOWABLE_SET.format(role=role.name, channel=channel.mention)
        await self.bot.send_message(ctx.message.channel, response)

    @unslowable.command(name="all", pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_roles=True)
    async def unslowable_all(self, ctx, *, role: discord.Role):
        """Adds a role to the list of roles which can't be slowed in all slowmodes of the server"""
        msg_channel = ctx.message.channel
        if msg_channel.server != role.server:
            response = self.INVALID_ROLE_OR_SERVER
        else:
            modified_channels = []
            for c_id, slowmode in self.slowmodes.items():
                channel = msg_channel.server.get_channel(c_id)
                if channel is not None:
                    if role.id not in slowmode.get("unstoppable_roles", []):
                        slowmode.setdefault("unstoppable_roles", []).append(role.id)
                    modified_channels.append(channel.mention)
            self.save_data()
            if len(modified_channels) > 0:
                response = self.UNSLOWABLE_SET.format(role=role.name, channel=", ".join(modified_channels))
            else:
                response = self.ALREADY_UNSLOWABLE_IN_ALL_CHANNELS.format(role=role.name)
        await self.bot.send_message(msg_channel, response)

    @unslowable.command(name="remove", pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_roles=True)
    async def unslowable_remove(self, ctx, channel: discord.Channel, *, role: discord.Role):
        """Removes a role from the list of roles which can't be slowed in the given channel"""
        if role.server != channel.server:
            response = self.INVALID_ROLE_OR_SERVER
        else:
            slowmode = self.get_channel_slowmode(channel)
            if channel.id not in self.slowmodes:
                response = self.NO_SLOWMODE_IN_CHANNEL.format(channel.mention)
            elif role.id not in slowmode.get("unstoppable_roles", []):
                response = self.ALREADY_SLOWABLE.format(role.name)
            else:
                slowmode["unstoppable_roles"].remove(role.id)
                self.save_data()
                response = self.SLOWABLE_SET.format(role=role.name, channel=channel.mention)
        await self.bot.send_message(ctx.message.channel, response)

    @unslowable.command(name="list", pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_roles=True)
    async def unslowable_list(self, ctx, channel: discord.Channel):
        """Lists the unslowable roles in a channel"""
        slowmode = self.get_channel_slowmode(channel)
        unslowable_roles = []
        for role_id in slowmode.get("unstoppable_roles", []):
            role = discord.utils.get(channel.server.roles, id=role_id)
            if role is not None:
                unslowable_roles.append(role.mention)
        embed = discord.Embed(color=discord.Colour.blue())
        embed.title = self.UNSLOWABLE_LIST_TITLE.format(channel=channel.name)
        embed.description = self.list(unslowable_roles) if len(unslowable_roles) > 0 else "no unslowable roles"
        await self.bot.send_message(ctx.message.channel, embed=embed)

    # Utilities
    async def check_channel(self, channel, slowmode=None):
        members = set(o[0] for o in channel.overwrites if
                      isinstance(o[0], discord.Member) and o[1].send_messages is False)
        slowmode = self.get_channel_slowmode(channel) if slowmode is None else slowmode
        end_time = datetime.datetime.utcnow() - datetime.timedelta(seconds=slowmode.get("time"))
        minimum_messages = slowmode.get("messages") or 0
        should_mute = set()
        async for message in self.bot.logs_from(channel, limit=minimum_messages):
            should_mute.add(message.author)
        async for message in self.bot.logs_from(channel, limit=500, after=end_time):
            should_mute.add(message.author)
        unmuting = members - should_mute
        return unmuting, should_mute

    async def _delete_last_from(self, channel, user, message_limit, ignore_msg):
        msg_count = 0
        async for message in self.bot.logs_from(channel, limit=500):
            if message.author.id == user.id and message.id != ignore_msg.id:
                await self.bot.delete_message(message)
                break
            elif msg_count >= message_limit:
                break
            else:
                msg_count += 1
    
    async def cancel_later(self, task, time):
        with contextlib.suppress(CancelledError, RuntimeError):
            await asyncio.sleep(time)
            task.cancel()
    
    async def unmute_user_later(self, channel, user, time, overwrite):
        with contextlib.suppress(RuntimeError):
            try:
                await asyncio.sleep(time)
                if channel.id in self.message_trackers and user.id in self.message_trackers[channel.id]:
                    await self.message_trackers[channel.id][user.id]["event"].wait()
            except (CancelledError, RuntimeError, GeneratorExit):
                pass
            finally:
                await self.unmute_user(channel, user, overwrite)
                self.temp_tasks[channel.id] = list(filter(lambda i: i != user.id, self.temp_tasks[channel.id]))
                self.save_temp_tasks()
    
    async def unmute_user(self, channel, user, overwrite):
        perms = channel.overwrites_for(user)
        if overwrite is None:
            perms.send_messages = None
        else:
            perms.send_messages = overwrite[1]
        if perms.is_empty():
            await self.bot.delete_channel_permissions(channel, user)
        else:
            await self.bot.edit_channel_permissions(channel, user, perms)
    
    async def get_member_last_msg_time(self, channel, member):
        slowmode = self.get_channel_slowmode(channel)
        latest_message = None
        msg_count = 0
        async for message in self.bot.logs_from(channel, limit=500):
            if message.author.id == member.id:
                latest_message = message
                break
            else:
                msg_count += 1
        if latest_message is not None:
            time_diff = datetime.datetime.utcnow() - latest_message.timestamp
            result = slowmode["time"] - time_diff.total_seconds()
        else:
            result = 0
        if channel.id not in self.message_trackers:
            self.message_trackers[channel.id] = {}
        self.message_trackers[channel.id][member.id] = {"msg_count": msg_count, "event": asyncio.Event()}
        if msg_count >= slowmode["messages"]:
            self.message_trackers[channel.id][member.id]["event"].set()
        return result
    
    async def check_for_welcome(self, user, channel):
        if user.id not in self.welcomed.get(channel.id, []):
            if channel.id not in self.welcomed:
                self.welcomed[channel.id] = []
            self.welcomed[channel.id].append(user.id)
            self.save_welcomed()
            slowmode = self.get_channel_slowmode(channel)
            format_dict = dict(me=self.bot.user.display_name)
            format_dict["time"] = self.humanize_time(slowmode["time"])
            format_dict["messages"] = self.plural_format(slowmode["messages"], "{} messages")
            format_dict["max_time"] = self.humanize_time(slowmode["max_time"])
            format_dict["channel"] = channel.mention
            format_dict["time_have"] = "has" if format_dict["time"].startswith("1 ") else "have"
            format_dict["messages_have"] = "has" if format_dict["messages"].startswith("1 ") else "have"
            if slowmode["max_time"] == 0:
                format_dict["max_time_format"] = ""
            else:
                format_dict["max_time_format"] = self.SLOWMODE_MAX_TIME_FORMAT.format(**format_dict)
            await self.bot.send_message(user, self.SLOWMODE_HELP_FORMAT.format(**format_dict))

    def increment_message_counts(self, channel):
        slowmode = self.get_channel_slowmode(channel)
        if channel.id in self.message_trackers:
            for msg_tracker in self.message_trackers[channel.id].values():
                msg_tracker["msg_count"] += 1
                if msg_tracker["msg_count"] >= slowmode["messages"]:
                    msg_tracker["event"].set()
    
    def get_channel_slowmode(self, channel):
        if channel.id in self.slowmodes.keys():
            if "messages" not in self.slowmodes[channel.id]:
                self.slowmodes[channel.id]["messages"] = self.DEFAULT_SLOWMODE["messages"]
            if "max_time" not in self.slowmodes[channel.id]:
                self.slowmodes[channel.id]["max_time"] = self.DEFAULT_SLOWMODE["max_time"]
            result = self.slowmodes[channel.id]
        else:
            result = self.DEFAULT_SLOWMODE
        return result
    
    def get_slowmode_msg(self, channel, slowmode=None):
        if slowmode is None:
            slowmode = self.get_channel_slowmode(channel)
        max_time_str = "no maximum time" if slowmode["max_time"] == 0 else "maximum " + \
                                                                           self.humanize_time(slowmode["max_time"])
        return self.SLOWMODE_FORMAT.format(channel=channel.mention, time=self.humanize_time(slowmode["time"]),
                                           msgs=self.plural_format(slowmode["messages"], "{} messages"),
                                           max_time=max_time_str)

    def check_mod_or_admin(self, member, *additional_roles):
        result = False
        mod_and_admin_roles = [self.bot.settings.get_server_admin(member.server).lower(),
                               self.bot.settings.get_server_mod(member.server).lower()]
        for role in member.roles:
            if role.name.lower() in mod_and_admin_roles:
                result = True
            elif role.id in additional_roles:
                result = True
        return result

    def list(self, entries: typing.List[str]) -> str:
        """Lists the elements in entries in natural english language as such:
        For 5 entries: entry, entry, entry, entry and entry
        For 3 entries: entry, entry and entry
        For 2 entries: entry and entry
        For 1 entry: entry

        If there's 0 entries, this will throw an error"""
        anded = [entries.pop(-1)]
        if len(entries) > 0:
            anded.insert(0, ", ".join(entries))
        return " and ".join(anded)

    def humanize_time(self, time: int) -> str:
        """Returns a string of the humanized given time keeping only the 2 biggest formats
        Examples:
        1661410 --> 2 weeks 5 days (hours, mins, seconds are ignored)
        30 --> 30 seconds"""
        times = []
        # 90 --> divmod(90, 60) --> (1, 30) --> (1m + 30s)
        for time_f in zip(self.TIME_FRACTIONS, self.TIME_FORMATS):
            time, units = divmod(time, time_f[0])
            if units > 0:
                times.append(self.plural_format(units, time_f[1]))
        if time > 0:
            times.append(self.plural_format(time, self.TIME_FORMATS[-1]))
        return " ".join(reversed(times[-2:]))

    def plural_format(self, raw_amount: typing.Union[int, float], format_string: str, *,
                      singular_format: str=None) -> str:
        """Formats a string for plural and singular forms of an amount

        The amount given is rounded.
        raw_amount is an integer (rounded if something else is given)
        format_string is the string to use when formatting in plural
        singular_format is the string to use for singular
            By default uses the plural and removes the last character"""
        amount = round(raw_amount)
        result = format_string.format(raw_amount)
        if singular_format is None:
            result = format_string.format(raw_amount)[:-1 if amount == 1 else None]
        elif amount == 1:
            result = singular_format.format(raw_amount)
        return result
    
    # Config
    def check_configs(self):
        self.check_folders()
        self.check_files()
    
    def check_folders(self):
        if not os.path.exists(self.DATA_FOLDER):
            self.logger.info("Creating data folder...")
            os.makedirs(self.DATA_FOLDER, exist_ok=True)
    
    def check_files(self):
        self.check_file(self.DATA_FILE_PATH, self.DEFAULT_SLOWMODES)
        self.check_file(self.TEMP_TASKS_FILE, self.DEFAULT_TEMP_TASKS)
        self.check_file(self.WELCOMED_USERS_FILE, self.DEFAULT_WELCOMED_USERS)
    
    def check_file(self, file, default):
        if not dataIO.is_valid_json(file):
            self.logger.info("Creating empty " + file + ".json...")
            dataIO.save_json(file, default)
    
    def load_data(self):
        self.slowmodes = dataIO.load_json(self.DATA_FILE_PATH)
        self.temp_tasks = dataIO.load_json(self.TEMP_TASKS_FILE)
        self.welcomed = dataIO.load_json(self.WELCOMED_USERS_FILE)
        for channel, slowmode in self.slowmodes.items():
            if isinstance(slowmode.get("overwrites"), list):
                self.slowmodes[channel]["overwrites"] = dict(slowmode["overwrites"])
    
    def save_data(self):
        dataIO.save_json(self.DATA_FILE_PATH, self.slowmodes)
    
    def save_temp_tasks(self):
        dataIO.save_json(self.TEMP_TASKS_FILE, self.temp_tasks)
    
    def save_welcomed(self):
        dataIO.save_json(self.WELCOMED_USERS_FILE, self.welcomed)


def setup(bot):
    bot.add_cog(SlowMode(bot))

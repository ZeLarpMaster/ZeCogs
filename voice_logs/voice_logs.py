import asyncio
import contextlib
import discord
import os.path
import os
import logging
import typing

from datetime import date, datetime, timedelta, timezone

from discord.ext import commands
from discord.ext.commands import Context

from .utils import checks
from .utils.dataIO import dataIO


class VoiceLogs:
    """Logs various information about user's voice activity"""

    DATA_FOLDER = "data/voice_logs"
    CONFIG_FILE_PATH = DATA_FOLDER + "/config.json"

    CONFIG_DEFAULT = {"channels": {}, "users": {}}
    """
    {
        channels: { channel.id: [user.id] },
        users: {
            user.id: [
                {
                    id: channel.id,
                    name: channel.name,
                    joined_at: datetime.utcnow().timestamp(),
                    left_at: datetime.utcnow().timestamp()
                }
            ]
        }
    }
    """

    # Time humanization
    TIME_FORMATS = ["{} seconds", "{} minutes", "{} hours", "{} days", "{} weeks"]
    TIME_FRACTIONS = [60, 60, 24, 7]

    ENTRY_TIME_LIMIT = timedelta(weeks=1)
    CLEANUP_DELAY = timedelta(days=1).total_seconds()

    def __init__(self, bot: discord.Client):
        self.bot = bot
        self.logger = logging.getLogger("red.ZeCogs.voice_logs")
        self.check_configs()
        self.load_data()
        asyncio.ensure_future(self.cleanup_loop())

    # Events
    async def on_voice_state_update(self, before: discord.Member, after: discord.Member):
        with contextlib.suppress(RuntimeError):
            await self.record_voice_update(before, after)

    # Commands
    @commands.group(name="voicelog", pass_context=True, invoke_without_command=True)
    @checks.mod_or_permissions(view_audit_logs=True)
    async def _voicelog(self, ctx: Context):
        """Access voice activity data"""
        await self.bot.send_cmd_help(ctx)

    @_voicelog.command(name="user", aliases=["u"], pass_context=True)
    @checks.mod_or_permissions(view_audit_logs=True)
    async def _voicelog_user(self, ctx: Context, user):
        """Looks up the voice activity of a user"""
        entries = self.config["users"].get(user, [])
        embed = discord.Embed(description="**Voice Activity for** <@!{}>".format(user))
        for entry in self.process_entries(entries, limit=25):
            joined_at = self.format_time(entry["joined_at"])
            left_at = entry.get("left_at")
            left_at = self.format_time(left_at) if left_at is not None else "now"
            embed.add_field(name="#{} ({})".format(entry["name"], entry["id"]),
                            value="**{}** until **{}**".format(joined_at, left_at),
                            inline=False)
        if len(embed.fields) == 0:
            embed.description = "No voice activity for <@!{}>".format(user)
        await self.bot.send_message(ctx.message.channel, embed=embed)

    @_voicelog.command(name="channel", aliases=["c"], pass_context=True)
    @checks.mod_or_permissions(view_audit_logs=True)
    async def _voicelog_channel(self, ctx: Context, channel):
        """Looks up the voice activity on a channel"""
        channel_name = channel
        entries = []
        for user_id, user_entries in self.config["users"].items():
            for entry in user_entries:
                if entry["id"] == channel:
                    entries.append(entry)
                    channel_name = entry["name"]

        embed = discord.Embed(title="Voice Activity in #{}".format(channel_name), description="")
        for entry in self.process_entries(entries, limit=25):
            time_spent = ""
            left_at = entry.get("left_at")
            if left_at is None:
                time_spent = "+"
                left_at = datetime.now(timezone.utc)
            time_diff = left_at - entry["joined_at"]
            time_spent = self.humanize_time(round(time_diff.total_seconds())) + time_spent
            embed.description += "**{}** ({}) for **{}**\n".format(entry["user_name"], entry["user_id"], time_spent)
        if len(embed.description) == 0:
            embed.description = "No voice activity in #{}".format(channel_name)
        await self.bot.send_message(ctx.message.channel, embed=embed)

    # Utilities
    async def record_voice_update(self, before: discord.Member, after: discord.Member):
        previous_channel = before.voice.voice_channel
        current_channel = after.voice.voice_channel
        if previous_channel == current_channel:
            return

        should_save = False
        member = before
        entries = self.config["users"].setdefault(member.id, [])
        if previous_channel is not None:  # Left that channel
            entry = discord.utils.find(lambda e: e["id"] == previous_channel.id and "left_at" not in e, entries)
            if entry is not None:
                entry["left_at"] = datetime.now(timezone.utc).timestamp()
                should_save = True

        if current_channel is not None:  # Joined that channel
            entry = {"id": current_channel.id,
                     "name": current_channel.name,
                     "joined_at": datetime.now(timezone.utc).timestamp(),
                     "user_id": after.id,
                     "user_name": after.name,
                     }
            entries.insert(0, entry)
            should_save = True

        if should_save is True:
            self.save_data()

    async def cleanup_loop(self):
        await self.bot.wait_until_ready()
        with contextlib.suppress(RuntimeError, asyncio.CancelledError):  # Suppress the "Event loop is closed" error
            while self == self.bot.get_cog(self.__class__.__name__):
                self.cleanup_entries()
                self.save_data()
                await asyncio.sleep(self.CLEANUP_DELAY)

    def cleanup_entries(self):
        delete_threshold = datetime.now(timezone.utc) - self.ENTRY_TIME_LIMIT
        to_delete = {}
        for user_id, entries in self.config["users"].items():
            for entry in entries:
                left_at = entry.get("left_at")
                if left_at is not None and datetime.fromtimestamp(left_at, timezone.utc) < delete_threshold:
                    to_delete.setdefault(user_id, []).append(entry)

        for user_id, entries in to_delete.items():
            for entry in entries:
                self.config["users"][user_id].remove(entry)

    def process_entries(self, entries, *, limit=None):
        return sorted(self.map_entries(entries), key=lambda o: o["joined_at"], reverse=True)[:limit]

    def map_entries(self, entries):
        for entry in entries:
            new_entry = entry.copy()
            joined_at = datetime.fromtimestamp(entry["joined_at"], timezone.utc)
            new_entry["joined_at"] = joined_at
            left_at = entry.get("left_at")
            if left_at is not None:
                new_entry["left_at"] = datetime.fromtimestamp(left_at, timezone.utc)
            yield new_entry

    def format_time(self, moment: datetime):
        if date.today() == moment.date():
            return "today " + moment.strftime("%X")
        else:
            return moment.strftime("%c")

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
    bot.add_cog(VoiceLogs(bot))

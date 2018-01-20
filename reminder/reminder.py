import asyncio
import discord
import os.path
import os
import re
import collections
import datetime

from discord.ext import commands
from .utils.dataIO import dataIO


class Reminder:
    """Utilities to remind yourself of whatever you want"""

    # File constants
    DATA_FOLDER = "data/reminder"
    DATA_FILE_PATH = DATA_FOLDER + "/reminders.json"

    # Configuration default
    CONFIG_DEFAULT = []

    # Behavior constants
    TIME_AMNT_REGEX = re.compile("([1-9][0-9]*)([a-z]+)", re.IGNORECASE)
    TIME_QUANTITIES = collections.OrderedDict([("seconds", 1), ("minutes", 60),
                                               ("hours", 3600), ("days", 86400),
                                               ("weeks", 604800), ("months", 2.628e+6),
                                               ("years", 3.154e+7)])  # (amount in seconds, max amount)
    MAX_SECONDS = TIME_QUANTITIES["years"] * 2

    # Message constants
    INVALID_TIME_FORMAT = ":x: Invalid time format."
    TOO_MUCH_TIME = ":x: Too long amount of time. Maximum: {} total seconds"
    WILL_REMIND = ":white_check_mark: I will remind you in {} seconds."
    
    def __init__(self, bot):
        self.bot = bot
        self.check_configs()
        self.load_data()
        self.futures = []
        asyncio.ensure_future(self.start_saved_reminders())
    
    # Events
    def __unload(self):
        for future in self.futures:
            future.cancel()
    
    # Commands
    @commands.command(pass_context=True)
    async def remind(self, ctx, time, *, text):
        """Remind yourself of something in a specific amount of time
        Examples for time: `5d`, `10m`, `10m30s`, `1h`, `1y1mo2w5d10h30m15s`
        Abbreviations: s for seconds, m for minutes, h for hours, d for days, w for weeks, mo for months, y for years
        Any longer abbreviation is accepted. `m` assumes minutes instead of months.
        One month is counted as exact 365/12 days.
        Ignores all invalid abbreviations."""
        message = ctx.message
        seconds = self.get_seconds(time)
        if seconds is None:
            response = self.INVALID_TIME_FORMAT
        elif seconds >= self.MAX_SECONDS:
            response = self.TOO_MUCH_TIME.format(round(self.MAX_SECONDS))
        else:
            user = message.author
            time_now = datetime.datetime.utcnow()
            days, secs = divmod(seconds, 3600*24)
            end_time = time_now + datetime.timedelta(days=days, seconds=secs)
            reminder = {"user": user.id, "content": text,
                        "start_time": time_now.timestamp(), "end_time": end_time.timestamp()}
            self.config.append(reminder)
            self.save_data()
            self.futures.append(asyncio.ensure_future(self.remind_later(user, seconds, text, reminder)))
            response = self.WILL_REMIND.format(seconds)
        await self.bot.send_message(message.channel, response)
    
    # Utilities
    async def start_saved_reminders(self):
        await self.bot.wait_until_ready()
        for reminder in list(self.config):  # Making a copy
            user_id = reminder["user"]
            user = None
            for server in self.bot.servers:
                user = user or server.get_member(user_id)
            if user is None:
                self.config.remove(reminder)  # Delete the reminder if the user doesn't have a mutual server anymore
            else:
                time_diff = datetime.datetime.fromtimestamp(reminder["end_time"]) - datetime.datetime.utcnow()
                time = max(0, time_diff.total_seconds())
                self.futures.append(asyncio.ensure_future(self.remind_later(user, time, reminder["content"], reminder)))
    
    async def remind_later(self, user: discord.User, time: float, content: str, reminder):
        """Reminds the `user` in `time` seconds with a message containing `content`"""
        await asyncio.sleep(time)
        embed = discord.Embed(title="Reminder", description=content, color=discord.Colour.blue())
        await self.bot.send_message(user, embed=embed)
        self.config.remove(reminder)
        self.save_data()
    
    def get_seconds(self, time):
        """Returns the amount of converted time or None if invalid"""
        seconds = 0
        for time_match in self.TIME_AMNT_REGEX.finditer(time):
            time_amnt = int(time_match.group(1))
            time_abbrev = time_match.group(2)
            time_quantity = discord.utils.find(lambda t: t[0].startswith(time_abbrev), self.TIME_QUANTITIES.items())
            if time_quantity is not None:
                seconds += time_amnt * time_quantity[1]
        return None if seconds == 0 else seconds
    
    # Config
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
    bot.add_cog(Reminder(bot))

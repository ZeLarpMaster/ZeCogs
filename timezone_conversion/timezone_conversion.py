import datetime
import re
import discord
import os.path
import math

import pytz  # pip install pytz

from discord.ext import commands
from .utils import checks
from .utils.dataIO import dataIO


class TimezoneConversion:
    """Timezone conversion tools"""

    # Config paths
    DATA_FOLDER = "data/timezones/"
    ALIASES_FILE = DATA_FOLDER + "aliases.json"

    # Behavior constants
    ALIASES_DEFAULT = {}
    TIME_REGEX = re.compile("(now|((1?[0-9])([ap]m))|(([0-9]{1,2}):([0-9]{2})))")

    # Message constants
    TIME_USAGE = """:x: Invalid command.
Usage: `{prefix}time <time> <timezone1> [timezone2]`
Where *time* is *now* or a timestamp of format 0am or 00:00 and *timezone* is the name of a tz timezone.
If timezone2 is omitted, it will only respond to *now* requests."""
    LIST_OF_TZ = "For a list of timezones: <https://en.wikipedia.org/wiki/List_of_tz_database_time_zones>"
    INVALID_SOURCE_TZ = ":x: Invalid __source__ timezone. " + LIST_OF_TZ
    INVALID_DESTINATION_TZ = ":x: Invalid __destination__ timezone. " + LIST_OF_TZ
    INEXISTANT_TZ = ":x: The timezone doesn't exist. " + LIST_OF_TZ
    TIME_NOW = "It is {hsource} in **{csource}** right now."
    ALIAS_ADDED = ":white_check_mark: Added alias *{}* refering to {}."
    TZ_HAS_ALIAS_NAME = ":x: A timezone already has this name. Consider changing your alias' name."
    ALIAS_EXISTS = ":x: The alias already exists. Consider removing it before re-adding it."
    ALIAS_NO_SPACE = ":x: There cannot be spaces in aliases and timezones."
    ALIAS_REMOVED = ":white_check_mark: Removed alias *{}*."
    ALIAS_CANT_REMOVE = ":x: Cannot remove alias *{}* because it doesn't exist."
    TIME_DIFF = "{hsource} in **{csource}** is equal to {hdest} in **{cdest}** ({tdiff[0]:+d}:{tdiff[1]:0>2})"

    def __init__(self, bot):
        self.bot = bot
        self.check_configs()
        self.load_data()

    @commands.group(name="time", pass_context=True, invoke_without_command=True)
    async def _time_converter(self, ctx, time, timezone1, timezone2=None):
        """Convert the time from timezone1 to timezone2

        List of supported timezones: <https://en.wikipedia.org/wiki/List_of_tz_database_time_zones>.
        The timezones must be the last part of the TZ column.
        For example, you would check the time in 'Africa/Abidjan' by doing `[p]time now Abidjan`"""
        if time is None and timezone1 is None:
            await self.bot.send_cmd_help(ctx)
        else:
            if None in (time, timezone1) or "" in (time, timezone1, timezone2):
                msg = self.TIME_USAGE
            elif timezone2 is None and time == "now":
                csource, zone = self.match_timezone(timezone1)
                if zone is None:
                    msg = self.INVALID_SOURCE_TZ
                else:
                    date = datetime.datetime.now(tz=zone)
                    hsource = self.format_hours_minutes(date.hour, date.minute)
                    msg = self.TIME_NOW.format(hsource=hsource, csource=csource)
            elif time == "to" and timezone1 == "stop":
                msg = "http://imgur.com/CoWZ05t.gif"
            else:
                msg = self._handle_time(time.lower(), timezone1, timezone2)
            await self.bot.send_message(ctx.message.channel, msg.format(prefix=ctx.prefix))

    @_time_converter.command(name="list", pass_context=True)
    async def _list_zones(self, ctx):
        """Print the link to the list of possible timezones"""
        await self.bot.send_message(ctx.message.channel, self.LIST_OF_TZ)

    @_time_converter.group(name="alias", pass_context=True, invoke_without_command=True)
    async def alias(self, ctx):
        """Manage the timezone aliases"""
        await self.bot.send_cmd_help(ctx)
    
    @alias.command(name="add", pass_context=True)
    @checks.mod_or_permissions(manage_roles=True)
    async def _add_alias(self, ctx, alias_name, timezone):
        """Add a new timezone alias

        For example, with an alias named 'PST' pointing timezone 'GMT+8',
        you would do: `[p]time now PST` and it would be the same as `[p]time now GMT+8`"""
        if " " not in alias_name and " " not in timezone:
            alias_name = alias_name.lower()
            timezone = timezone.lower()
            if alias_name not in self.aliases:
                alias_zone = discord.utils.find(lambda z: z.rsplit("/")[-1].lower() == alias_name,
                                                pytz.all_timezones_set)
                if alias_zone is None:
                    zone = discord.utils.find(lambda z: z.rsplit("/")[-1].lower() == timezone, pytz.all_timezones_set)
                    if zone is not None:
                        self.aliases[alias_name] = zone
                        self.save_data()
                        message = self.ALIAS_ADDED.format(alias_name, zone)
                    else:
                        message = self.INEXISTANT_TZ
                else:
                    message = self.TZ_HAS_ALIAS_NAME
            else:
                message = self.ALIAS_EXISTS
        else:
            message = self.ALIAS_NO_SPACE
        await self.bot.send_message(ctx.message.channel, message)
    
    @alias.command(name="remove", pass_context=True, aliases=["del", "delete"])
    @checks.mod_or_permissions(manage_roles=True)
    async def _remove_alias(self, ctx, alias_name):
        """Delete a timezone alias"""
        alias_name = alias_name.lower()
        if alias_name in self.aliases:
            del self.aliases[alias_name]
            self.save_data()
            response = self.ALIAS_REMOVED.format(alias_name)
        else:
            response = self.ALIAS_CANT_REMOVE.format(alias_name)
        await self.bot.send_message(ctx.message.channel, response)
    
    @alias.command(name="list", pass_context=True, aliases=["ls"])
    async def _list_alias(self, ctx):
        """List all timezone aliases"""
        embed = discord.Embed(title="Alias List", colour=discord.Colour.light_grey(), description="```")
        if len(self.aliases) > 0:
            alias_list = list(self.aliases.items())
            half = math.ceil(len(alias_list) / 2)
            for i, a in enumerate(alias_list[:half]):
                a1_name = "{} → {}".format(*a)
                if i+half < len(alias_list):
                    a2_name = "{} → {}".format(*alias_list[i+half])
                else:
                    a2_name = ""
                embed.description += "{:<30}  {:<30}\n".format(a1_name, a2_name)
            embed.description += "```"
        else:
            embed.description = "No aliases to be listed."
        await self.bot.send_message(ctx.message.channel, embed=embed)

    # Utilities
    def format_hours_minutes(self, hours, minutes):
        format_24 = hours
        format_minutes = ":{:0>2}".format(minutes)
        cropped_minutes = format_minutes if minutes > 0 else ""
        format_12 = self._get_12h_str(hours, cropped_minutes)
        return "**{h12}** ({h24}{m})".format(h12=format_12, h24=format_24, m=format_minutes)

    def match_timezones(self, country):
        return [pytz.timezone(item) for item in pytz.all_timezones if item.lower().endswith(country)]

    def match_timezone(self, country):
        country = country.lower()
        if country in self.aliases:
            zone = self.aliases[country]
            result = zone.rsplit("/")[-1], pytz.timezone(zone)
        else:
            timezone_name = discord.utils.find(lambda z: z.rsplit("/")[-1].lower() == country, pytz.all_timezones_set)
            if timezone_name is not None:
                name = timezone_name.rsplit("/")[-1]
                result = name, pytz.timezone(timezone_name)
            else:
                result = None, None
        return result

    def get_zone_offset(self, zone):
        return datetime.datetime.now(tz=zone).utcoffset()

    def timezone_diff(self, zone_src, zone_dst):
        total_offset = self.get_zone_offset(zone_dst) - self.get_zone_offset(zone_src)
        offset_seconds = total_offset.total_seconds()
        offset_minutes = offset_seconds // 60
        return divmod(offset_minutes, 60)

    def get_zone_time(self, zone):
        dt = datetime.datetime.now(tz=zone)
        return dt.hour, dt.minute

    def format_timezone(self, time_source, country_source, country_dest):
        csource, zone1 = self.match_timezone(country_source)
        cdest, zone2 = self.match_timezone(country_dest)
        if zone1 is None:  # Source timezone not found
            result = self.INVALID_SOURCE_TZ
        elif zone2 is None:  # Destination timezone not found
            result = self.INVALID_DESTINATION_TZ
        else:
            if time_source[0] is None and time_source[1] is None:
                time_source = self.get_zone_time(zone1)
                hours_dest, minutes_dest = self.get_zone_time(zone2)
                time_diff = (hours_dest - time_source[0], minutes_dest - time_source[1])
            else:
                time_diff = self.timezone_diff(zone1, zone2)
                hours_dest = (time_source[0] + time_diff[0] + 24) % 24
                minutes_dest = (time_source[1] + time_diff[1] + 60) % 60
                time_diff = (int(time_diff[0]), int(time_diff[1]))
            hsource = self.format_hours_minutes(*time_source)
            hdest = self.format_hours_minutes(int(hours_dest), int(minutes_dest))
            result = self.TIME_DIFF.format(hsource=hsource, csource=csource, hdest=hdest, cdest=cdest, tdiff=time_diff)
        return result

    def _handle_time(self, time, country_source, country_result):
        regex = self.TIME_REGEX.fullmatch(time)
        msg = ""
        error = False
        if regex.group(2) is not None:  # 0am
            hours_source = int(regex.group(3))
            minutes_source = 0  # TODO: Make this changeable? maybe 00:00am format
            hours_source = self._convert_12h_to_24h(hours_source, regex.group(4) == "pm")
        elif regex.group(5) is not None:  # 00:00
            hours_source = int(regex.group(6))
            minutes_source = int(regex.group(7))
        elif regex.group(1) == "now":
            hours_source = None
            minutes_source = None
        else:  # Invalid format
            hours_source = 0
            minutes_source = 0
            error = True
            msg = ":x: Invalid time format. Use now, 0am or 00:00."
        if hours_source is not None and hours_source >= 24:
            error = True
            msg = ":x: Invalid time. How do you have more than 24h in your day?"
        if not error:
            msg = self.format_timezone([hours_source, minutes_source], country_source, country_result)
        return msg

    def _convert_12h_to_24h(self, hours, is_pm):
        if hours == 12:
            if is_pm:
                result = 12
            else:
                result = 0
        else:
            result = hours + (12 if is_pm else 0)
        return result

    def _get_12h_str(self, hours, mins_str):
        pm_str = "AM" if hours < 12 else "PM"
        if hours == 0:
            hours_12 = "12"
        elif hours > 12:
            hours_12 = str(hours - 12)
        else:
            hours_12 = str(hours)
        return "{}{} {}".format(hours_12, mins_str, pm_str)

    # Config
    def check_configs(self):
        self.check_folders()
        self.check_files()

    def check_folders(self):
        if not os.path.exists(self.DATA_FOLDER):
            print("Creating data folder...")
            os.makedirs(self.DATA_FOLDER, exist_ok=True)

    def check_files(self):
        self.check_file(self.ALIASES_FILE, self.ALIASES_DEFAULT)

    def check_file(self, file, default):
        if not dataIO.is_valid_json(file):
            print("Creating empty " + file + "...")
            dataIO.save_json(file, default)

    def load_data(self):
        # Here, you load the data from the config file.
        self.aliases = dataIO.load_json(self.ALIASES_FILE)

    def save_data(self):
        # Save all the data (if needed)
        dataIO.save_json(self.ALIASES_FILE, self.aliases)


def setup(bot):
    bot.add_cog(TimezoneConversion(bot))

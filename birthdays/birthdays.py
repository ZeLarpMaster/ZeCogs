import asyncio
import discord
import os.path
import os
import datetime
import itertools
import contextlib

from discord.ext import commands
from .utils import checks
from .utils.dataIO import dataIO


class Birthdays:
    """Announces people's birthdays and gives them a birthday role for the whole UTC day"""

    # File related constants
    DATA_FOLDER = "data/birthdays"
    CONFIG_FILE_PATH = DATA_FOLDER + "/config.json"

    # Configuration default
    CONFIG_DEFAULT = {
        "roles": {},  # {server.id: role.id} of the birthday roles
        "channels": {},  # {server.id: channel.id} of the birthday announcement channels
        "birthdays": {},  # {date: {user.id: year}} of the users' birthdays
        "yesterday": []  # List of user ids who's birthday was done yesterday
    }

    # Message constants
    ROLE_SET = ":white_check_mark: The birthday role on **{s}** has been set to: **{r}**."
    BDAY_INVALID = ":x: The birthday date you entered is invalid. It must be `MM-DD`."
    BDAY_SET = ":white_check_mark: Your birthday has been set to: **{}**."
    CHANNEL_SET = ":white_check_mark: The channel for announcing birthdays on **{s}** has been set to: **{c}**."
    BDAY_REMOVED = ":put_litter_in_its_place: Your birthday has been removed."

    def __init__(self, bot: discord.Client):
        self.bot = bot
        self.check_configs()
        self.load_data()
        self.bday_loop = asyncio.ensure_future(self.initialise())  # Starts a loop which checks daily for birthdays

    # Events
    async def initialise(self):
        await self.bot.wait_until_ready()
        with contextlib.suppress(RuntimeError):
            while self == self.bot.get_cog(self.__class__.__name__):  # Stops the loop when the cog is reloaded
                now = datetime.datetime.utcnow()
                tomorrow = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                await asyncio.sleep((tomorrow - now).total_seconds())
                self.clean_yesterday_bdays()
                self.do_today_bdays()
                self.save_data()

    def __unload(self):
        self.bday_loop.cancel()  # Forcefully cancel the loop when unloaded

    # Commands
    @commands.group(pass_context=True, invoke_without_command=True)
    async def bday(self, ctx):
        """Birthday settings"""
        await self.bot.send_cmd_help(ctx)

    @bday.command(name="channel", pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_roles=True)
    async def bday_channel(self, ctx, channel: discord.Channel):
        """Sets the birthday announcement channel for this server"""
        message = ctx.message
        c = message.channel
        server = message.server
        self.config["channels"][server.id] = channel.id
        self.save_data()
        await self.bot.send_message(c, self.CHANNEL_SET.format(s=server.name, c=channel.name))

    @bday.command(name="role", pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_roles=True)
    async def bday_role(self, ctx, role: discord.Role):
        """Sets the birthday role for this server"""
        message = ctx.message
        channel = message.channel
        server = message.server
        self.config["roles"][server.id] = role.id
        self.save_data()
        await self.bot.send_message(channel, self.ROLE_SET.format(s=server.name, r=role.name))

    @bday.command(name="remove", aliases=["del", "clear", "rm"], pass_context=True)
    async def bday_remove(self, ctx):
        """Unsets your birthday date"""
        message = ctx.message
        channel = message.channel
        author = message.author
        self.remove_user_bday(author.id)
        self.save_data()
        await self.bot.send_message(channel, self.BDAY_REMOVED)

    @bday.command(name="set", pass_context=True)
    async def bday_set(self, ctx, date, year: int=None):
        """Sets your birthday date

        The given date must be given as: MM-DD
        Year is optional. If ungiven, the age won't be displayed."""
        message = ctx.message
        channel = message.channel
        author = message.author
        birthday = self.parse_date(date)
        if birthday is None:
            await self.bot.send_message(channel, self.BDAY_INVALID)
        else:
            self.remove_user_bday(author.id)
            self.config["birthdays"].setdefault(str(birthday.toordinal()), {})[author.id] = year
            self.save_data()
            bday_month_str = birthday.strftime("%B")
            bday_day_str = birthday.strftime("%d").lstrip("0")  # To remove the zero-capped
            await self.bot.send_message(channel, self.BDAY_SET.format(bday_month_str + " " + bday_day_str))

    @bday.command(name="list", pass_context=True)
    async def bday_list(self, ctx):
        """Lists the birthdays

        If a user has their year set, it will display the age they'll get after their birthday this year"""
        message = ctx.message
        channel = message.channel
        self.clean_bdays()
        self.save_data()
        bdays = self.config["birthdays"]
        this_year = datetime.date.today().year
        embed = discord.Embed(title="Birthday List", color=discord.Colour.lighter_grey())
        for k, g in itertools.groupby(sorted(datetime.datetime.fromordinal(int(o)) for o in bdays.keys()),
                                      lambda i: i.month):
            # Basically separates days with "\n" and people on the same day with ", "
            value = "\n".join(date.strftime("%d").lstrip("0") + ": "
                              + ", ".join("<@!{}>".format(u_id)
                                          + ("" if year is None else " ({})".format(this_year - int(year)))
                                          for u_id, year in bdays.get(str(date.toordinal()), {}).items())
                              for date in g if len(bdays.get(str(date.toordinal()))) > 0)
            if not value.isspace():  # Only contains whitespace when there's no birthdays in that month
                embed.add_field(name=datetime.datetime(year=1, month=k, day=1).strftime("%B"), value=value)
        await self.bot.send_message(channel, embed=embed)

    # Utilities
    async def clean_bday(self, user_id):
        for server_id, role_id in self.config["roles"].items():
            server = self.bot.get_server(server_id)
            if server is not None:
                role = discord.utils.find(lambda r: r.id == role_id, server.roles)
                # If discord.Server.roles was an OrderedDict instead...
                member = server.get_member(user_id)
                if member is not None and role is not None and role in member.roles:
                    # If the user and the role are still on the server and the user has the bday role
                    await self.bot.remove_roles(member, role)

    async def handle_bday(self, user_id, year):
        embed = discord.Embed(color=discord.Colour.gold())
        if year is not None:
            age = datetime.date.today().year - int(year)  # Doesn't support non-western age counts but whatever
            embed.description = "<@!{}> is now **{} years old**. :tada:".format(user_id, age)
        else:
            embed.description = "It's <@!{}>'s birthday today! :tada:".format(user_id)
        for server_id, channel_id in self.config["channels"].items():
            server = self.bot.get_server(server_id)
            if server is not None:  # Ignore unavailable servers or servers the bot isn't in anymore
                member = server.get_member(user_id)
                if member is not None:
                    role_id = self.config["roles"].get(server_id)
                    if role_id is not None:
                        role = discord.utils.find(lambda r: r.id == role_id, server.roles)
                        if role is not None:
                            try:
                                await self.bot.add_roles(member, role)
                            except (discord.Forbidden, discord.HTTPException):
                                pass
                            else:
                                self.config["yesterday"].append(member.id)
                    channel = server.get_channel(channel_id)
                    if channel is not None:
                        await self.bot.send_message(channel, embed=embed)

    def clean_bdays(self):
        """Cleans the birthday entries with no user's birthday
        Also removes birthdays of users who aren't in any visible server anymore

        Happens when someone changes their birthday and there's nobody else in the same day"""
        birthdays = self.config["birthdays"]
        set(self.bot.get_all_members())
        for date, bdays in birthdays.copy().items():
            for user_id, year in bdays.copy().items():
                if not any(s.get_member(user_id) is not None for s in self.bot.servers):
                    del birthdays[date][user_id]
            if len(bdays) == 0:
                del birthdays[date]

    def remove_user_bday(self, user_id):
        for date, user_ids in self.config["birthdays"].items():
            if user_id in user_ids:
                del self.config["birthdays"][date][user_id]
        # Won't prevent the cleaning problem here cause the users can leave so we'd still want to clean anyway

    def clean_yesterday_bdays(self):
        for user_id in self.config["yesterday"]:
            asyncio.ensure_future(self.clean_bday(user_id))
        self.config["yesterday"].clear()

    def do_today_bdays(self):
        this_date = datetime.datetime.utcnow().date().replace(year=1)
        for user_id, year in self.config["birthdays"].get(str(this_date.toordinal()), {}).items():
            asyncio.ensure_future(self.handle_bday(user_id, year))

    def parse_date(self, date_str):
        result = None
        try:
            result = datetime.datetime.strptime(date_str, "%m-%d").date().replace(year=1)
        except ValueError:
            pass
        return result

    # Config
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
        self.config = dataIO.load_json(self.CONFIG_FILE_PATH)

    def save_data(self):
        dataIO.save_json(self.CONFIG_FILE_PATH, self.config)


def setup(bot):
    # Creating the cog
    cog = Birthdays(bot)
    # Finally, add the cog to the bot.
    bot.add_cog(cog)

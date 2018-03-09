import asyncio
import discord
import os.path
import os
import logging
import datetime
import typing
import aiohttp
import json

from .utils import checks
from .utils.dataIO import dataIO
from discord.ext import commands
from discord.ext.commands.context import Context


MessageList = typing.List[discord.Message]
TreeCache = typing.Dict[str, typing.Set[discord.Message]]


class IndexedSearch:
    """Search through predefined channels to find messages which contain"""

    # Config
    DATA_FOLDER = "data/indexed_search"
    CONFIG_FILE_PATH = DATA_FOLDER + "/cache.json"
    DEFAULT_SERVER_CONFIG = {
        "haystacks": {},  # {name: channel.id}
        "maximum_days": 3,  # Maximum number of days to go back to build the cache
        "abbreviations": {}  # {abbreviation: word}
    }

    # Behavior constants
    TEMP_MESSAGE_TIMEOUT = 60 * 5  # seconds
    DOWNLOAD_TIMEOUT = 15  # seconds
    DOWNLOAD_HEADERS = {"User-Agent": "Mozilla"}

    # Time humanization
    TIME_FORMATS = ["{} seconds", "{} minutes", "{} hours", "{} days", "{} weeks"]
    TIME_FRACTIONS = [60, 60, 24, 7]

    # Messages
    CATEGORY_NOT_FOUND = ":x: Category not found"
    WAITING = "The search cache is not fully loaded yet. Waiting..."
    SEARCH_RESULT_TITLE = "Searched: '{}'"
    SEARCH_RESULT_DESCRIPTION = "Results found in the latest {time} of messages in {channel}"
    SEARCH_RESULT_MESSAGE = "{author} **{time} ago**: {content}"
    SEARCH_RESULT_MORE = "And more..."
    NO_RESULTS_EMBED = {"title": "Searched: '{}'",
                        "description": "No one has posted that term in the last {time}.",
                        "colour": discord.Colour.red()}
    CHANNEL_ALREADY_INDEXED = ":x: The channel {} is already indexed"
    CATEGORY_ALREADY_USED = ":x: The category `{}` is already used"
    INDEX_ADDED = ":white_check_mark: The channel {} is now indexed under `{}`"
    INDEX_LIST_EMBED = {"title": "Indexes in {}", "description": "", "colour": discord.Colour.light_grey()}
    INDEX_LIST_ENTRY = "{name} --> <#{channel}>"
    INDEX_LIST_EMPTY = "There is no index here"
    CHANNEL_NOT_INDEXED = ":x: {} is already not indexed"
    INDEX_REMOVED = ":put_litter_in_its_place: The index for {channel} has been deleted"
    MAX_DAYS_SET = ":white_check_mark: The maximum number of days to go back has been set to {days} in {server}"
    CANNOT_HAVE_NEGATIVE_DAYS = ":x: The maximum number of days cannot be negative"
    NO_SEARCH_TEXT = ":x: You must search for something"
    MUST_PROVIDE_ONE_JSON = ":x: You must provide one .json file"
    SUCCESSFULLY_SET_ABBREVIATIONS = ":white_check_mark: Successfully imported the abbreviations"
    PROVIDED_JSON_INVALID = ":x: The JSON file you provided is invalid"
    ABBREV_LIST_EMBED = {"title": "List of abbreviations in {server}",
                         "colour": discord.Colour.lighter_grey(),
                         "description": ""}
    ABBREV_LIST_EMPTY = "The abbreviation list is empty"

    def __init__(self, bot: discord.Client):
        self.bot = bot
        self.logger = logging.getLogger("red.ZeCogs.indexed_search")
        self.check_configs()
        self.load_data()
        self.cache_ready = asyncio.Event()
        self.cache = {}  # {server.id: {channel.id: TreeCache}}
        self.session = None
        asyncio.ensure_future(self.fetch_cache())

    # Events
    async def on_message(self, message: discord.Message):
        self._on_message_action(parse=message)

    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        self._on_message_action(remove=before, parse=after)

    async def on_message_delete(self, message: discord.Message):
        self._on_message_action(remove=message)

    def __unload(self):
        if self.session is not None:
            self.session.close()

    # Commands
    @commands.group(name="search", pass_context=True, no_pm=True, invoke_without_command=True)
    async def _search(self, ctx: Context, index: str, *, text: str):
        """Search through the index for the given text

        The category must be a valid index given by [p]search list_index"""
        words = text.split(" ")
        message = ctx.message
        server = message.server
        args = []
        kwargs = {}
        server_config = self.config.get(server.id, self.DEFAULT_SERVER_CONFIG)
        channel_id = server_config["haystacks"].get(index.lower())
        trees = self.cache.get(server.id, {}).get(channel_id)
        if len(words) == 0:
            args.append(self.NO_SEARCH_TEXT)
        elif trees is None:
            args.append(self.CATEGORY_NOT_FOUND)
        else:
            if not self.cache_ready.is_set():
                msg = await self.bot.send_message(message.channel, self.WAITING)
                await self.cache_ready.wait()
                await self.bot.delete_message(msg)
            abbrevs = server_config["abbreviations"]
            msgs = self.find_in_trees(self.wordify(abbrevs, words), abbrevs, trees)
            days_diff = datetime.timedelta(days=server_config["maximum_days"])
            humanized_days = self.humanize_time(days_diff.total_seconds())
            minimum_date = datetime.datetime.utcnow() - days_diff
            msgs = sorted(filter(lambda m: m[1].timestamp > minimum_date, msgs),
                          key=lambda m: m[1].timestamp, reverse=True)
            if len(msgs) > 0:
                embed = self.build_search_embed(words, abbrevs, humanized_days, msgs[:10])
                if len(msgs) > 10:
                    embed.set_footer(text=self.SEARCH_RESULT_MORE)
            else:
                embed = discord.Embed(**self.NO_RESULTS_EMBED)
                embed.title = embed.title.format(text)
                embed.description = embed.description.format(time=humanized_days)
            kwargs["embed"] = embed
        await self.temp_send(message.channel, [message], *args, **kwargs)

    @_search.command(name="add_index", pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_channels=True)
    async def _search_add_index(self, ctx: Context, channel: discord.Channel, name: str):
        """Add a channel to the index

        Adds the `channel` to the indexed channels with `name` as it's index name
        The `name` is case insensitive"""
        message = ctx.message
        server = message.server
        server_conf = self.config.setdefault(server.id, self.DEFAULT_SERVER_CONFIG)
        haystacks = server_conf["haystacks"]
        if channel.id in haystacks.values():
            response = self.CHANNEL_ALREADY_INDEXED.format(channel.mention)
        elif name.lower() in haystacks:
            response = self.CATEGORY_ALREADY_USED.format(name)
        else:
            haystacks[name.lower()] = channel.id
            await self.fetch_channel_cache(channel, datetime.timedelta(days=server_conf["maximum_days"]))
            self.save_data()
            response = self.INDEX_ADDED.format(channel.mention, name)
        await self.temp_send(message.channel, [message], response)

    @_search.command(name="list_index", aliases=["list_indexes"], pass_context=True, no_pm=True)
    async def _search_list_indexes(self, ctx: Context):
        """Lists the search indexes of the current server"""
        message = ctx.message
        server = message.server
        haystacks = self.config.get(server.id, {}).get("haystacks", {})
        embed = discord.Embed(**self.INDEX_LIST_EMBED)
        embed.title = embed.title.format(server.name)
        for name, channel_id in haystacks.items():
            embed.description += self.INDEX_LIST_ENTRY.format(channel=channel_id, name=name) + "\n"
        embed.description = embed.description or self.INDEX_LIST_EMPTY
        await self.temp_send(message.channel, [message], embed=embed)

    @_search.command(name="remove_index", aliases=["del_index"], pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_channels=True)
    async def _search_remove_index(self, ctx: Context, channel: discord.Channel):
        """Removes a channel from the index"""
        message = ctx.message
        server = message.server
        server_conf = self.config.get(server.id, self.DEFAULT_SERVER_CONFIG)
        pair = discord.utils.find(lambda o: o[1] == channel.id, server_conf["haystacks"].items())
        if pair is None:
            response = self.CHANNEL_NOT_INDEXED.format(channel.mention)
        else:
            server_conf["haystacks"].pop(pair[0], ...)
            self.save_data()
            self.cache.get(server.id, {}).pop(pair[1], ...)
            response = self.INDEX_REMOVED.format(channel=channel.mention)
        await self.temp_send(message.channel, [message], response)

    @_search.command(name="set_max_days", aliases=["set_days"], pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_channels=True)
    async def _search_set_max_days(self, ctx: Context, days: float):
        """Sets the maximum number of days to search back

        The number of days can have a fractional part (ie.: 2.5 days = 2 days 12 hours)"""
        message = ctx.message
        if days <= 0:
            response = self.CANNOT_HAVE_NEGATIVE_DAYS
        else:
            server = message.server
            server_conf = self.config.setdefault(server.id, self.DEFAULT_SERVER_CONFIG)
            server_conf["maximum_days"] = days
            self.save_data()
            self.cache.clear()
            await self.fetch_cache()
            response = self.MAX_DAYS_SET.format(server=server.name, days=days)
        await self.temp_send(message.channel, [message], response)

    @_search.command(name="import_abbreviations", aliases=["import_abbrevs", "import"], pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_channels=True)
    async def _search_import_abbreviations(self, ctx):
        """Sets the abbreviations

        You must attach a JSON file with the command (must be a .json file)
        The JSON file must be an object containing key/value pairs of abbreviation/word
        Example of a JSON's contents: {
            "hi": "hello",
            "greetings": "hello",
            "ez": "easy"
        }"""
        message = ctx.message
        attachments = [attachment for attachment in message.attachments if attachment["filename"].endswith(".json")]
        if len(attachments) != 1:
            response = self.MUST_PROVIDE_ONE_JSON
        else:
            abbreviations = await self.download_json_file(attachments[0]["url"])
            if abbreviations is None or \
                    not all((isinstance(k, str) and isinstance(v, str))
                            for k, v in abbreviations.items()):
                response = self.PROVIDED_JSON_INVALID
            else:
                server_conf = self.config.setdefault(message.server.id, self.DEFAULT_SERVER_CONFIG)
                server_conf["abbreviations"] = abbreviations
                self.save_data()
                response = self.SUCCESSFULLY_SET_ABBREVIATIONS
        await self.temp_send(message.channel, [message], response)

    @_search.command(name="list_abbreviations", aliases=["list_abbrevs"], pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_channels=True)
    async def _search_list_abbreviations(self, ctx):
        """Lists the current server's abbreviations"""
        message = ctx.message
        embed = discord.Embed(**self.ABBREV_LIST_EMBED)
        embed.title = embed.title.format(server=message.server.name)
        abbrevs = self.config.get(message.server.id, self.DEFAULT_SERVER_CONFIG)["abbreviations"]
        if len(abbrevs) == 0:
            embed.description = self.ABBREV_LIST_EMPTY
        for abbrev, word in abbrevs.items():
            embed.description += "{} **-->** {}\n".format(abbrev, word)
        await self.temp_send(message.channel, [message], embed=embed)

    # Utilities
    async def download_json_file(self, url: str) -> dict:
        """Downloads the content of "url" into a BytesIO object asynchronously"""
        if self.session is None:
            self.session = aiohttp.ClientSession()
        async with self.session.get(url, timeout=self.DOWNLOAD_TIMEOUT, headers=self.DOWNLOAD_HEADERS) as response:
            try:
                content = await response.json()
            except (json.JSONDecodeError, aiohttp.ClientResponseError):
                content = None
            else:
                if not isinstance(content, dict):
                    content = None
        return content

    def build_search_embed(self, search_terms: typing.List[str], abbrevs: typing.Dict[str, str], days: str,
                           results: typing.List[typing.Tuple[int, discord.Message]]) -> discord.Embed:
        embed = discord.Embed(title=self.SEARCH_RESULT_TITLE.format(" ".join(search_terms)))
        embed.description = self.SEARCH_RESULT_DESCRIPTION.format(time=days, channel=results[0][1].channel.mention)
        embed.description += "\n"
        embed.colour = discord.Colour.green()
        search_terms = self.wordify(abbrevs, search_terms)
        now = datetime.datetime.utcnow()
        for i, message in results:
            author = message.author.mention
            content = message.content.splitlines()[i]
            raw_words = content.split(" ")
            words = self.wordify(abbrevs, raw_words)
            match = self.subsequence_in_sequence(words, search_terms)
            raw_words[match] = "`" + raw_words[match]
            raw_words[match + len(search_terms) - 1] = raw_words[match + len(search_terms) - 1] + "`"
            content = " ".join(raw_words)
            time_diff = now - message.timestamp
            time = self.humanize_time(time_diff.total_seconds())
            embed.description += "\n" + self.SEARCH_RESULT_MESSAGE.format(author=author, time=time, content=content)
        return embed

    async def fetch_cache(self):
        self.cache_ready.clear()
        await self.bot.wait_until_ready()
        for server_config in self.config.values():
            go_back = datetime.timedelta(days=server_config["maximum_days"])
            for channel_id in server_config["haystacks"].values():
                channel = self.bot.get_channel(channel_id)
                if channel is not None:
                    await self.fetch_channel_cache(channel, go_back)
        self.cache_ready.set()

    async def fetch_channel_cache(self, channel: discord.Channel, go_back: datetime.timedelta):
        trees = self.cache.setdefault(channel.server.id, {}).setdefault(channel.id, {})
        after = datetime.datetime.utcnow() - go_back
        total = 0
        keep_going = True
        while keep_going:
            count = 0
            async for message in self.bot.logs_from(channel, after=after, reverse=True):
                self.parse_message(message, trees)
                count += 1
                after = message
            keep_going = count == 100
            total += count
        self.logger.info("Cached {} messages for #{}".format(total, channel.name))

    def find_in_trees(self, search_terms: typing.List[str], abbrevs: typing.Dict[str, str], tree: TreeCache) \
            -> typing.List[typing.Tuple[int, discord.Message]]:
        result = []
        msgs = tree.get(search_terms[0], set())
        for msg in msgs:
            try:
                line = discord.utils.find(self.create_matcher(abbrevs, search_terms),
                                          enumerate(msg.content.splitlines()))[0]
            except TypeError as e:
                self.logger.warning("Error: {}, Search terms: {}, message's lines: {}".format(e, search_terms,
                                                                                              msg.content.splitlines()))
            else:
                if line is not None:
                    result.append((line, msg))
        return result

    def create_matcher(self, abbrevs: typing.Dict[str, str], terms: typing.List[str]):
        def match_line(line: str):
            match = self.subsequence_in_sequence(self.wordify(abbrevs, line[1].split(" ")),
                                                 self.wordify(abbrevs, terms))
            return match is not None
        return match_line

    def wordify(self, abbrevs: typing.Dict[str, str], word_list: typing.List[str]) -> typing.List[str]:
        result = []
        for word in word_list:
            word = word.lower().strip()
            result.append(abbrevs.get(word, word))
        return result

    def subsequence_in_sequence(self, source, target, start=0, end=None):
        """Naive search for target in source"""
        m = len(source)
        n = len(target)
        if end is None:
            end = m
        else:
            end = min(end, m)
        if n == 0 or (end - start) < n:
            # target is empty, or longer than source, so obviously can't be found.
            return None
        for i in range(start, end - n + 1):
            if source[i:i + n] == target:
                return i
        return None

    def parse_message(self, message: discord.Message, tree: TreeCache):
        self._do_for_word_on_cache(message, tree, set.add)

    def remove_message(self, message: discord.Message, tree: TreeCache):
        self._do_for_word_on_cache(message, tree, set.discard)

    def _do_for_word_on_cache(self, message: discord.Message, tree: TreeCache,
                              func: typing.Callable[[set, discord.Message], None]):
        if message is not None:
            abbrevs = self.config.get(message.server.id, self.DEFAULT_SERVER_CONFIG)["abbreviations"]
            for word in message.content.split(" "):
                word = word.lower().strip()
                word = abbrevs.get(word, word)
                func(tree.setdefault(word, set()), message)

    def _on_message_action(self, *, parse: discord.Message=None, remove: discord.Message=None):
        either = parse or remove
        server = either.server
        if server is not None and self.cache_ready.is_set():
            tree = self.cache.get(server.id, {}).get(either.channel.id)
            if tree is not None:
                self.remove_message(remove, tree)
                self.parse_message(parse, tree)

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

    def message_filter(self, message: discord.Message) -> bool:
        result = False
        channel = message.channel
        if not channel.is_private:
            if channel.permissions_for(channel.server.me).manage_messages:
                result = True
        return result

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
                times.append(self.plural_format(int(units), time_f[1]))
        if time > 0:
            times.append(self.plural_format(int(time), self.TIME_FORMATS[-1]))
        return times[-1]

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
            self.logger.debug("Creating data folder...")
            os.makedirs(self.DATA_FOLDER, exist_ok=True)

    def check_files(self):
        self.check_file(self.CONFIG_FILE_PATH, {})

    def check_file(self, file, default):
        if not dataIO.is_valid_json(file):
            self.logger.debug("Creating empty " + file + "...")
            dataIO.save_json(file, default)

    def load_data(self):
        self.config = dataIO.load_json(self.CONFIG_FILE_PATH)

    def save_data(self):
        dataIO.save_json(self.CONFIG_FILE_PATH, self.config)


def setup(bot):
    bot.add_cog(IndexedSearch(bot))

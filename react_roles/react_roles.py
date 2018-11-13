import asyncio
import copy
import discord
import os.path
import os
import math
import traceback
import re
import logging
import itertools
import contextlib
import collections

from discord.ext import commands
from .utils import checks
from .utils.dataIO import dataIO


class ReactRoles:
    """Associate emojis on messages with roles to gain/lose roles when clicking on reactions

    Requires ClientModification to properly work"""

    # File related constants
    DATA_FOLDER = "data/react_roles"
    CONFIG_FILE_PATH = DATA_FOLDER + "/config.json"

    # Configuration defaults
    SERVER_DEFAULT = {}
    CONFIG_DEFAULT = {}
    """
    {
        server.id: {
            channel.id: {
                message.id: {
                    emoji.id or str: role.id
                }
            }
            links: {
                name: [channel.id + "_" + message.id]
            }
        }
    }"""

    # Behavior related constants
    MAXIMUM_PROCESSED_PER_SECOND = 5
    EMOTE_REGEX = re.compile("<a?:[a-zA-Z0-9_]{2,32}:(\d{1,20})>")
    LINKS_ENTRY = "links"

    # Message constants
    PROGRESS_FORMAT = "Checked {c} out of {r} reactions out of {t} emojis."
    PROGRESS_COMPLETE_FORMAT = """:white_check_mark: Completed! Checked a total of {c} reactions.
Gave a total of {g} roles."""
    MESSAGE_NOT_FOUND = ":x: Message not found."
    ALREADY_BOUND = ":x: The emoji is already bound on that message."
    NOT_IN_SERVER = ":x: The channel must be in a server."
    ROLE_NOT_FOUND = ":x: Role not found on the given channel's server."
    EMOJI_NOT_FOUND = ":x: Emoji not found in any of my servers or in unicode emojis."
    CANT_ADD_REACTIONS = ":x: I don't have the permission to add reactions in that channel."
    CANT_MANAGE_ROLES = ":x: I don't have the permission to manage users' roles in the channel's server."
    ROLE_SUCCESSFULLY_BOUND = ":white_check_mark: The role has been bound to {} on the message in {}."
    ROLE_NOT_BOUND = ":x: The role is not bound to that message."
    ROLE_UNBOUND = ":put_litter_in_its_place: Unbound the role on the message.\n"
    REACTION_CLEAN_START = ROLE_UNBOUND + "Removing linked reactions..."
    PROGRESS_REMOVED = ROLE_UNBOUND + "Removed **{} / {}** reactions..."
    REACTION_CLEAN_DONE = ROLE_UNBOUND + "Removed **{}** reactions."
    NO_CLIENT_MODIFICATION = "\nYou do not have the client_modification cog installed. " \
                             "You may expect roles to not work after restarting."
    LINK_MESSAGE_NOT_FOUND = "The following messages weren't found: {}"
    LINK_CHANNEL_NOT_FOUND = "The following channels weren't found: {}"
    LINK_PAIR_INVALID = "The following channel-message pairs were invalid: {}"
    NO_LINKED_MESSAGES_SPECIFIED = "You did not specify any channel-message pair"
    LINK_FAILED = ":x: Failed to link reactions.\n"
    LINK_SUCCESSFUL = ":white_check_mark: Successfully linked the reactions."
    LINK_NAME_TAKEN = ":x: That link name is already used in the current server. Remove it before assigning to it."
    UNLINK_NOT_FOUND = ":x: Could not find a link with that name in this server."
    UNLINK_SUCCESSFUL = ":white_check_mark: The link has been removed from this server."
    CANT_CHECK_LINKED = ":x: Cannot run a check on linked messages."
    CANT_GIVE_ROLE = ":x: I can't give that role! Maybe it's higher than my own highest role?"

    def __init__(self, bot: discord.Client):
        self.bot = bot
        self.logger = logging.getLogger("red.ZeCogs.react_roles")
        self.check_configs()
        self.load_data()
        self.role_queue = asyncio.Queue()
        self.role_map = {}
        self.role_cache = {}
        self.links = {}  # {server.id: {channel.id_message.id: [role]}}
        self.processing_wait_time = 0 if self.MAXIMUM_PROCESSED_PER_SECOND == 0 else 1/self.MAXIMUM_PROCESSED_PER_SECOND
        asyncio.ensure_future(self._init_bot_manipulation())
        self.role_processor = asyncio.ensure_future(self.process_role_queue())
    
    # Events
    async def on_reaction_add(self, reaction, user):
        try:
            await self.check_add_role(reaction, user)
        except:  # Didn't want the event listener to stop working when a random error happens
            traceback.print_exc()
    
    async def on_reaction_remove(self, reaction, user):
        try:
            await self.check_remove_role(reaction, user)
        except:  # Didn't want the event listener to stop working when a random error happens
            traceback.print_exc()
    
    async def on_message_delete(self, message: discord.Message):
        # Remove the config too
        channel = message.channel
        if not channel.is_private:
            self.remove_cache_message(message)
            server = channel.server
            server_conf = self.get_config(server.id)
            channel_conf = server_conf.get(channel.id, {})
            if message.id in channel_conf:
                del channel_conf[message.id]
            # And the cache
            self.remove_message_from_cache(server.id, channel.id, message.id)
            # And the links
            pair = channel.id + "_" + message.id
            if pair in self.links.get(server.id, {}):
                del self.links[server.id][pair]
            server_links = server_conf.get(self.LINKS_ENTRY)
            if server_links is not None:
                for links in server_links.values():
                    if pair in links:
                        links.remove(pair)
    
    async def _init_bot_manipulation(self):
        counter = collections.Counter()
        await self.bot.wait_until_ready()
        for server_id, server_conf in self.config.items():
            server = self.bot.get_server(server_id)
            if server is not None:
                for channel_id, channel_conf in filter(lambda o: o[0] != self.LINKS_ENTRY, server_conf.items()):
                    channel = server.get_channel(channel_id)
                    if channel is not None:
                        for msg_id, msg_conf in channel_conf.items():
                            msg = await self.safe_get_message(channel, msg_id)
                            if msg is not None:
                                self.add_cache_message(msg)  # This is where the magic happens.
                                for emoji_str, role_id in msg_conf.items():
                                    role = discord.utils.get(server.roles, id=role_id)
                                    if role is not None:
                                        self.add_to_cache(server_id, channel_id, msg_id, emoji_str, role)
                                        counter.update((channel.name, ))
                            else:
                                self.logger.warning("Could not find message {} in {}".format(msg_id, channel.mention))
                    else:
                        self.logger.warning("Could not find channel with id {} in server {}".format(channel_id,
                                                                                                    server.name))
                link_list = server_conf.get(self.LINKS_ENTRY)
                if link_list is not None:
                    self.parse_links(server_id, link_list.values())
            else:
                self.logger.warning("Could not find server with id {}".format(server_id))
        self.logger.info("Cached bindings: {}".format(", ".join(": ".join(map(str, pair)) for pair in counter.items())))

    def __unload(self):
        # This method is ran whenever the bot unloads this cog.
        self.role_processor.cancel()
    
    # Commands
    @commands.group(name="roles", pass_context=True, no_pm=True, invoke_without_command=True)
    @checks.mod_or_permissions(manage_roles=True)
    async def _roles(self, ctx):
        """Roles giving configuration"""
        await self.bot.send_cmd_help(ctx)

    @_roles.command(name="linklist", pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_roles=True)
    async def _roles_link_list(self, ctx):
        """Lists all reaction links in the current server"""
        message = ctx.message
        server = message.server
        server_conf = self.get_config(server.id)
        server_links = server_conf.get(self.LINKS_ENTRY, {})
        embed = discord.Embed(title="Role Links", colour=discord.Colour.light_grey())
        for name, pairs in server_links.items():
            value = ""
            for channel, messages in itertools.groupby(pairs, key=lambda p: p.split("_")[0]):
                value += "<#{}>: ".format(channel) + ", ".join(p.split("_")[1] for p in messages)
            if len(value) > 0:
                embed.add_field(name=name, value=value)
        if len(embed.fields) == 0:
            embed.description = "There are no links in this server"
        await self.bot.send_message(message.channel, embed=embed)

    @_roles.command(name="unlink", pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_roles=True)
    async def _roles_unlink(self, ctx, name: str):
        """Remove a link of messages by its name"""
        message = ctx.message
        server = message.server
        server_conf = self.get_config(server.id)
        server_links = server_conf.get(self.LINKS_ENTRY)
        name = name.lower()
        if server_links is None or name not in server_links:
            response = self.UNLINK_NOT_FOUND
        else:
            self.remove_links(server.id, name)
            del server_links[name]
            self.save_data()
            response = self.UNLINK_SUCCESSFUL
        await self.bot.send_message(message.channel, response)

    @_roles.command(name="link", pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_roles=True)
    async def _roles_link(self, ctx, name: str, *linked_messages):
        """Link messages together to allow only one role from those messages to be given to a member

        name is the name of the link; used to make removal easier
        linked_messages is an arbitrary number of channelid-messageid
        You can get those channelid-messageid pairs with a shift right click on messages
        Users can only get one role out of all the reactions in the linked messages
        The bot will NOT remove the user's other reaction(s) when clicking within linked messages"""
        message = ctx.message
        server = message.server
        pairs = []
        messages_not_found = []
        channels_not_found = []
        invalid_pairs = []
        for pair in linked_messages:
            split_pair = pair.split("-", 1)
            if len(split_pair) == 2:
                channel_id, message_id = split_pair
                channel = server.get_channel(channel_id)
                if channel is not None:
                    message = await self.safe_get_message(channel, message_id)
                    if message is not None:
                        pairs.append("_".join(split_pair))
                    else:
                        messages_not_found.append(split_pair)
                else:
                    channels_not_found.append(channel_id)
            else:
                invalid_pairs.append(pair)
        confimation_msg = ""
        if len(invalid_pairs) > 0:
            confimation_msg += self.LINK_PAIR_INVALID.format(", ".join(invalid_pairs)) + "\n"
        if len(channels_not_found) > 0:
            confimation_msg += self.LINK_CHANNEL_NOT_FOUND.format(", ".join(channels_not_found)) + "\n"
        if len(messages_not_found) > 0:
            confimation_msg += self.LINK_MESSAGE_NOT_FOUND.format(
                ", ".join("{} in <#{}>".format(p[0], p[1]) for p in messages_not_found)) + "\n"
        if len(linked_messages) == 0:
            confimation_msg += self.NO_LINKED_MESSAGES_SPECIFIED
        if len(confimation_msg) > 0:
            response = self.LINK_FAILED + confimation_msg
        else:
            server_conf = self.get_config(server.id)
            server_links = server_conf.setdefault(self.LINKS_ENTRY, {})
            name = name.lower()
            if name in server_links:
                response = self.LINK_NAME_TAKEN
            else:
                server_links[name] = pairs
                self.save_data()
                self.parse_links(server.id, [pairs])
                response = self.LINK_SUCCESSFUL
        await self.bot.send_message(message.channel, response)
    
    @_roles.command(name="add", pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_roles=True)
    async def _roles_add(self, ctx, message_id, channel: discord.Channel, emoji, *, role: discord.Role):
        """Add a role on a message
        `message_id` must be found in `channel`
        `emoji` can either be a Unicode emoji or a server emote
        `role` must be found in the channel's server"""
        server = channel.server
        message = await self.safe_get_message(channel, message_id)
        if message is None:
            response = self.MESSAGE_NOT_FOUND
        else:
            msg_conf = self.get_message_config(server.id, channel.id, message.id)
            emoji_match = self.EMOTE_REGEX.fullmatch(emoji)
            emoji_id = emoji if emoji_match is None else emoji_match.group(1)
            if emoji_id in msg_conf:
                response = self.ALREADY_BOUND
            elif server is None:
                response = self.NOT_IN_SERVER
            else:
                if role.server != channel.server:
                    response = self.ROLE_NOT_FOUND
                elif channel.server.me.server_permissions.manage_roles is False:
                    response = self.CANT_MANAGE_ROLES
                elif channel.permissions_for(channel.server.me).add_reactions is False:
                    response = self.CANT_ADD_REACTIONS
                else:
                    emoji = None
                    for emoji_server in self.bot.servers:
                        if emoji is None:
                            emoji = discord.utils.get(emoji_server.emojis, id=emoji_id)
                    try:
                        await self.bot.add_reaction(message, emoji or emoji_id)
                    except discord.HTTPException:  # Failed to find the emoji
                        response = self.EMOJI_NOT_FOUND
                    else:
                        try:
                            await self.bot.add_roles(ctx.message.author, role)
                            await self.bot.remove_roles(ctx.message.author, role)
                        except (discord.Forbidden, discord.HTTPException):
                            response = self.CANT_GIVE_ROLE
                            await self.bot.remove_reaction(message, emoji or emoji_id, self.bot.user)
                        else:
                            self.add_to_cache(server.id, channel.id, message_id, emoji_id, role)
                            msg_conf[emoji_id] = role.id
                            self.save_data()
                            response = self.ROLE_SUCCESSFULLY_BOUND.format(str(emoji or emoji_id), channel.mention)
                            if self.bot.get_cog("ClientModification") is None:
                                response += self.NO_CLIENT_MODIFICATION
                            else:
                                self.add_cache_message(message)
        await self.bot.send_message(ctx.message.channel, response)
    
    @_roles.command(name="remove", pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_roles=True)
    async def _roles_remove(self, ctx, message_id, channel: discord.Channel, *, role: discord.Role):
        """Remove a role from a message
        `message_id` must be found in `channel` and be bound to `role`"""
        server = channel.server
        msg_config = self.get_message_config(server.id, channel.id, message_id)
        c = ctx.message.channel
        emoji_config = discord.utils.find(lambda o: o[1] == role.id, msg_config.items())
        if emoji_config is None:
            await self.bot.send_message(c, self.ROLE_NOT_BOUND)
        else:
            emoji_str = emoji_config[0]
            self.remove_role_from_cache(server.id, channel.id, message_id, emoji_str)
            del msg_config[emoji_str]
            self.save_data()
            msg = await self.safe_get_message(channel, message_id)
            if msg is None:
                await self.bot.send_message(c, self.MESSAGE_NOT_FOUND)
            else:
                answer = await self.bot.send_message(c, self.REACTION_CLEAN_START)
                reaction = discord.utils.find(
                    lambda r: r.emoji.id == emoji_str if r.custom_emoji else r.emoji == emoji_str, msg.reactions)
                after = None
                count = 0
                user = None
                for page in range(math.ceil(reaction.count / 100)):
                    for user in await self.bot.get_reaction_users(reaction, after=after):
                        await self.bot.remove_reaction(msg, reaction.emoji, user)
                        count += 1
                    after = user
                    await self.bot.edit_message(answer, self.PROGRESS_REMOVED.format(count, reaction.count))
                await self.bot.edit_message(answer, self.REACTION_CLEAN_DONE.format(count))
    
    @_roles.command(name="check", pass_context=True, no_pm=True)
    @checks.mod_or_permissions(manage_roles=True)
    async def _roles_check(self, ctx, message_id, channel: discord.Channel):
        """Goes through all reactions of a message and gives the roles accordingly
        This does NOT work with messages in a link"""
        server = channel.server
        msg = await self.safe_get_message(channel, message_id)
        server_links = self.links.get(server.id, {})
        if channel.id + "_" + message_id in server_links:
            await self.bot.send_message(ctx.message.channel, self.CANT_CHECK_LINKED)
        elif msg is None:
            await self.bot.send_message(ctx.message.channel, self.MESSAGE_NOT_FOUND)
        else:
            msg_conf = self.get_message_config(server.id, channel.id, msg.id)
            if msg_conf is not None:  # Something is very wrong if this is False but whatever
                progress_msg = await self.bot.send_message(ctx.message.channel, "Initializing...")
                given_roles = 0
                checked_count = 0
                total_count = sum(map(lambda r: r.count, msg.reactions)) - len(msg.reactions)  # Remove the bot's
                total_reactions = 0
                for react in msg.reactions:  # Go through all reactions on the message and add the roles if needed
                    total_reactions += 1
                    emoji_str = react.emoji.id if react.custom_emoji else react.emoji
                    role = self.get_from_cache(server.id, channel.id, msg.id, emoji_str)
                    if role is not None:
                        before = 0
                        after = None
                        user = None
                        while before != after:
                            before = after
                            for user in await self.bot.get_reaction_users(react, after=after):
                                member = server.get_member(user.id)
                                if member is not None and member != self.bot.user and \
                                        discord.utils.get(member.roles, id=role.id) is None:
                                    await self.bot.add_roles(member, role)
                                    given_roles += 1
                                checked_count += 1
                            after = user
                            await self.bot.edit_message(progress_msg, self.PROGRESS_FORMAT.format(
                                                            c=checked_count, r=total_count, t=total_reactions))
                    else:
                        checked_count += react.count
                        await self.bot.edit_message(progress_msg, self.PROGRESS_FORMAT.format(
                                                        c=checked_count, r=total_count, t=total_reactions))
                await self.bot.edit_message(progress_msg, self.PROGRESS_COMPLETE_FORMAT.format(c=checked_count,
                                                                                               g=given_roles))
    
    # Utilities
    async def check_add_role(self, reaction, member):
        message = reaction.message
        channel = message.channel
        if isinstance(member, discord.Member) and member != self.bot.user:
            # Check whether or not the reaction happened on a server and prevent the bot from giving itself the role
            server = channel.server
            emoji_str = reaction.emoji.id if reaction.custom_emoji else reaction.emoji
            role = self.get_from_cache(server.id, channel.id, message.id, emoji_str)
            if role is not None:
                await self.add_role_queue(member, role, True,
                                          linked_roles=self.get_link(server.id, channel.id, message.id))
    
    async def check_remove_role(self, reaction, member):
        message = reaction.message
        channel = message.channel
        if isinstance(member, discord.Member):  # Check whether or not the reaction happened on a server
            server = channel.server
            emoji_str = reaction.emoji.id if reaction.custom_emoji else reaction.emoji
            if member == self.bot.user:  # Safeguard in case a mod removes the bot's reaction by accident
                msg_conf = self.get_message_config(server.id, channel.id, message.id)
                if emoji_str in msg_conf:
                    await self.bot.add_reaction(message, reaction.emoji)
            else:
                role = self.get_from_cache(server.id, channel.id, message.id, emoji_str)
                if role is not None:
                    await self.add_role_queue(member, role, False)
    
    async def add_role_queue(self, member, role, add_bool, *, linked_roles=set()):
        key = "_".join((member.server.id, member.id))  # Doing it this way here to make it simpler a bit
        q = self.role_map.get(key)
        if q is None:  # True --> add   False --> remove
            q = {True: set(), False: {member.server.default_role}, "mem": member}
            # Always remove the @everyone role to prevent the bot from trying to give it to members
            await self.role_queue.put(key)
        q[True].difference_update(linked_roles)  # Remove the linked roles from the roles to add
        q[False].update(linked_roles)  # Add the linked roles to remove them if the user has any of them
        q[not add_bool] -= {role}
        q[add_bool] |= {role}
        self.role_map[key] = q

    async def process_role_queue(self):  # This exists to update multiple roles at once when possible
        """Loops until the cog is unloaded and processes the role assignments when it can"""
        await self.bot.wait_until_ready()
        with contextlib.suppress(RuntimeError, asyncio.CancelledError):  # Suppress the "Event loop is closed" error
            while self == self.bot.get_cog(self.__class__.__name__):
                key = await self.role_queue.get()
                q = self.role_map.pop(key)
                if q is not None and q.get("mem") is not None:
                    mem = q["mem"]
                    all_roles = set(mem.roles)
                    add_set = q.get(True, set())
                    del_set = q.get(False, {mem.server.default_role})
                    try:
                        await self.bot.replace_roles(mem, *((all_roles | add_set) - del_set))
                        # Basically, the user's roles + the added - the removed
                    except (discord.Forbidden, discord.HTTPException):
                        self.role_map[key] = q  # Try again when it fails
                        await self.role_queue.put(key)
                    else:
                        self.role_queue.task_done()
                    finally:
                        await asyncio.sleep(self.processing_wait_time)
        self.logger.debug("The processing loop has ended.")

    async def safe_get_message(self, channel, message_id):
        try:
            result = await self.bot.get_message(channel, message_id)
        except discord.errors.DiscordException:
            result = None
        return result

    def get_link(self, server_id, channel_id, message_id):
        return self.links.get(server_id, {}).get(channel_id + "_" + message_id, set())

    def parse_links(self, server_id, links_list):
        """Parses the links of a server into self.links
        links_list is a list of links each link being a list of channel.id_message.id linked together"""
        link_dict = {}
        for link in links_list:
            role_list = set()
            for entry in link:
                channel_id, message_id = entry.split("_", 1)
                role_list.update(self.get_all_roles_from_message(server_id, channel_id, message_id))
            for entry in link:
                link_dict.setdefault(entry, set()).update(role_list)
        self.links[server_id] = link_dict

    def remove_links(self, server_id, name):
        entry_list = self.get_config(server_id).get(self.LINKS_ENTRY, {}).get(name, [])
        link_dict = self.links.get(server_id, {})
        for entry in entry_list:
            if entry in link_dict:
                channel_id, message_id = entry.split("_", 1)
                role_list = set()
                role_list.update(self.get_all_roles_from_message(server_id, channel_id, message_id))
                link_dict[entry].difference_update(role_list)
                if len(link_dict[entry]) == 0:
                    del link_dict[entry]

    # Cache -- Needed to keep the actual role object in cache instead of looking for it every time in the server's roles
    def add_to_cache(self, server_id, channel_id, message_id, emoji_str, role):
        """Adds an entry to the role cache"""
        server_conf = self.role_cache.setdefault(server_id, {})
        channel_conf = server_conf.setdefault(channel_id, {})
        message_conf = channel_conf.setdefault(message_id, {})
        message_conf[emoji_str] = role

    def get_all_roles_from_message(self, server_id, channel_id, message_id):
        """Fetches all roles from a given message returns an iterable"""
        return self.role_cache.get(server_id, {}).get(channel_id, {}).get(message_id, {}).values()

    def get_from_cache(self, server_id, channel_id, message_id, emoji_str):
        """Fetches the role associated with an emoji on the given message"""
        return self.role_cache.get(server_id, {}).get(channel_id, {}).get(message_id, {}).get(emoji_str)

    def remove_role_from_cache(self, server_id, channel_id, message_id, emoji_str):
        """Removes an entry from the role cache"""
        server_conf = self.role_cache.get(server_id)
        if server_conf is not None:
            channel_conf = server_conf.get(channel_id)
            if channel_conf is not None:
                message_conf = channel_conf.get(message_id)
                if message_conf is not None and emoji_str in message_conf:
                    del message_conf[emoji_str]

    def remove_message_from_cache(self, server_id, channel_id, message_id):
        """Removes a message from the role cache"""
        server_conf = self.role_cache.get(server_id)
        if server_conf is not None:
            channel_conf = server_conf.get(channel_id)
            if channel_conf is not None and message_id in channel_conf:
                del channel_conf[message_id]

    # Client Modification Proxy
    def add_cache_message(self, message):
        cm = self.bot.get_cog("ClientModification")
        if cm is not None:
            cm.add_cached_message(message)
    
    def remove_cache_message(self, message):
        cm = self.bot.get_cog("ClientModification")
        if cm is not None:
            cm.remove_cached_message(message)
    
    # Config
    def get_message_config(self, server_id, channel_id, message_id):
        return self.get_config(server_id).setdefault(channel_id, {}).setdefault(message_id, {})
    
    def get_config(self, server_id):
        config = self.config.get(server_id)
        if config is None:
            self.config[server_id] = copy.deepcopy(self.SERVER_DEFAULT)
        return self.config.get(server_id)
    
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
    c = ReactRoles(bot)
    # Finally, add the cog to the bot.
    bot.add_cog(c)

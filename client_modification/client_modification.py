import asyncio
import discord


class ClientModification:
    """Cog which provides endpoints to use modified Client features
    Usage example: bot.get_cog("ClientModification").add_cached_message(msg)
    
    Currently supports:
        - Adding messages to the client message cache

    This cog mostly exists because I was denied a change on discord.connection to allow adding messages into the cache.
    This could've been done cleanly by adding a side dictionary of messages cached by the client which can be
        manipulated by cogs through discord.Client, but I was told this change didn't have it's place in discord.py.

    We need to be able to add messages into the cache to listen to events on messages which weren't received while the
        bot was online. This is because discord.py doesn't throw events when they happen on an Object which isn't in
        cache. This is because Discord only sends the message id for events which happen on it so the library isn't
        able to send an event with a Message object. The developers decided they just wouldn't send the event if that
        happened. The only alternative they give us is on_socket_raw_receive, but that would me rewriting all the
        parsing internally in every cog which needs it and that's very redundant.

        As an example, if someone wanted to listen to reactions on a specific message which could've been posted months
        ago, you wouldn't be able to go grab the actual message object through endpoints and add it to the cache to then
        receive the events because Danny decided so. So here's a monkey patch to support it.

    If you want to fight for it to be added natively, go ahead. I failed at expressing the need for it when I tried.

    Changes like these are in this centralized cog to prevent conflicts when monkey patching.
        Basically to ensure `revert_modifications` doesn't remove another monkey patch which might've been done later"""
    
    def __init__(self, bot):
        self.bot = bot
        asyncio.ensure_future(self._init_modifications())
        self.cached_messages = {}
    
    # Events
    async def _init_modifications(self):
        await self.bot.wait_until_ready()
        self._init_message_modifs()
    
    def __unload(self):
        # This method is ran whenever the bot unloads this cog.
        self.revert_modifications()

    # Endpoints
    def add_cached_messages(self, messages):
        self.cached_messages.update((m.id, m) for m in messages if isinstance(m, discord.Message))
    
    def add_cached_message(self, message):
        if isinstance(message, discord.Message):
            self.cached_messages[message.id] = message
    
    def remove_cached_message(self, message):
        if isinstance(message, discord.Message):
            if message.id in self.cached_messages:
                del self.cached_messages[message.id]
        elif isinstance(message, str):
            if message in self.cached_messages:
                del self.cached_messages[message]
    
    # Utilities
    def _init_message_modifs(self):
        def _get_modified_message(message_id):
            message = None
            cm = self.bot.get_cog("ClientModification")
            # Checking if ClientModification is still loaded in case it was unloaded without reverting this
            if cm is not None:
                message = cm.cached_messages.get(message_id)
            return message or self.__og_get_message(message_id)
        self.__og_get_message = self.bot.connection._get_message
        self.bot.connection._get_message = _get_modified_message
    
    def revert_modifications(self):
        self.bot.connection._get_message = self.__og_get_message


def setup(bot):
    bot.add_cog(ClientModification(bot))

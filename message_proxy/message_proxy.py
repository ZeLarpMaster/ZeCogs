import asyncio
import copy
import discord
import os.path
import os
import math
import logging
import io

import aiohttp  # Ensured by discord since this is a dependency of discord.py

from discord.ext import commands
from .utils import checks
from .utils.dataIO import dataIO


class MessageProxy:
    """Send and edit messages through the bot"""

    # Message constants
    MESSAGE_LINK = "<https://discordapp.com/channels/{s}/{c}/{m}>"
    MESSAGE_SENT = ":white_check_mark: Sent " + MESSAGE_LINK
    FAILED_TO_FIND_MESSAGE = ":x: Failed to find the message with id {} in {}."
    COMMAND_FORMAT = "{p}msg edit <#{c_id}> {m_id} ```\n{content}```"

    def __init__(self, bot: discord.Client):
        self.bot = bot
        self.logger = logging.getLogger("red.ZeCogs.message_proxy")

    # Commands
    @commands.group(name="message", aliases=["msg"], pass_context=True, no_pm=True, invoke_without_command=True)
    @checks.mod_or_permissions(manage_server=True)
    async def _messages(self, ctx):
        """Message proxy"""
        await self.bot.send_cmd_help(ctx)

    @_messages.command(name="send", pass_context=True)
    @checks.mod_or_permissions(manage_server=True)
    async def _messages_send(self, ctx, channel: discord.Channel, *, content=None):
        """Send a message in the given channel

        An attachment can be provided.
        If no content is provided, at least an attachment must be provided."""
        message = ctx.message
        attachment = self.get_attachment(message)
        if attachment is not None:
            async with aiohttp.ClientSession() as session:
                async with session.get(url=attachment[0], headers={"User-Agent": "Mozilla"}) as response:
                    file = io.BytesIO(await response.read())
            msg = await self.bot.send_file(channel, file, content=content and "Placeholder", filename=attachment[1])
        else:
            msg = await self.bot.send_message(channel, "Placeholder")
        if content is not None:
            await self.bot.edit_message(msg, new_content=content)
            reply = self.COMMAND_FORMAT.format(p=ctx.prefix, content=content, m_id=msg.id, c_id=channel.id)
            await self.bot.delete_message(ctx.message)
        else:
            reply = self.MESSAGE_SENT.format(m=msg.id, c=channel.id, s=channel.server.id)
        await self.bot.send_message(message.channel, reply)

    @_messages.command(name="edit", pass_context=True)
    @checks.mod_or_permissions(manage_server=True)
    async def _messages_edit(self, ctx, channel: discord.Channel, message_id: str, *, new_content):
        """Edit the message with id message_id in the given channel

        No attachment can be provided."""
        try:
            msg = await self.bot.get_message(channel, message_id)
        except discord.errors.HTTPException:
            response = self.FAILED_TO_FIND_MESSAGE.format(message_id, channel.mention)
        else:
            await self.bot.edit_message(msg, new_content=new_content)
            response = self.COMMAND_FORMAT.format(p=ctx.prefix, content=new_content, m_id=msg.id, c_id=channel.id)
            await self.bot.delete_message(ctx.message)
        await self.bot.send_message(ctx.message.channel, response)

    # Utilities
    def get_attachment(self, message):
        if not message.attachments:
            return None
        attachment = message.attachments[0]
        return attachment["url"], attachment["filename"]


def setup(bot):
    bot.add_cog(MessageProxy(bot))

"""Voice channel disconnection command."""

from __future__ import annotations

import logging

from discord.ext import commands

from music_player import ui
from music_player.state import MusicState

log = logging.getLogger(__name__)


class LeaveChannel(commands.Cog):
    """Disconnects the bot from its current voice channel."""

    def __init__(self, bot: commands.Bot, state: MusicState) -> None:
        self.bot = bot
        self.state = state

    @commands.hybrid_command(name="leave", description="Leave current joined voice channel")
    async def leave(self, ctx: commands.Context) -> None:
        state = self.state.get(ctx.guild.id)

        if not state.connected:
            await ctx.send(embed=ui.error("Did not join any voice channel."))
            return

        state.suppress_advance = True
        await state.disconnect()
        await ctx.send(embed=ui.success(":white_check_mark: Have leave the voice channel."))

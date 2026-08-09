"""Voice channel connection commands."""

from __future__ import annotations

import logging
from typing import List

import discord
from discord import Interaction
from discord import app_commands as appCommand
from discord.ext import commands

from music_player import ui
from music_player.state import MusicState

log = logging.getLogger(__name__)


class JoinChannel(commands.Cog):
    """Connects the bot to, and moves it between, voice channels."""

    def __init__(self, bot: commands.Bot, state: MusicState) -> None:
        self.bot = bot
        self.state = state

    @commands.hybrid_command(name="join", description="Join voice channel")
    @appCommand.describe(channel="Choose Voice Channel to join #voice channel id")
    async def join(self, ctx: commands.Context, channel: str) -> None:
        target = self._resolve_channel(ctx, channel)
        if target is None:
            await ctx.send(
                embed=ui.error(
                    "**I couldn't find that voice channel.**\n"
                    "Use **`/join`** and pick one from the list."
                )
            )
            return

        state = self.state.get(ctx.guild.id)

        try:
            # A voice client that dropped its connection cannot be reused;
            # clear it out before reconnecting.
            if state.voice is not None and not state.voice.is_connected():
                await state.disconnect()

            if not state.connected:
                state.voice = await target.connect()
                state.schedule_idle_disconnect()
                await ctx.send(embed=ui.joined(target.name))
                return

            if state.voice.channel.id == target.id:
                await ctx.send(
                    embed=ui.notice(f"👍  **I'm already in {target.name}.**")
                )
                return

            was_playing = state.playing
            if was_playing:
                state.voice.pause()
                state.mark_paused()
                state.suppress_advance = True

            await state.voice.move_to(target)
            state.schedule_idle_disconnect()
            await ctx.send(embed=ui.moved(target.name, was_playing=was_playing))
        except discord.ClientException:
            log.exception("voice connect failed in guild %s", ctx.guild.id)
            await ctx.send(
                embed=ui.error(
                    f"**I couldn't connect to {target.name}.**\n"
                    "Check I'm allowed to join it, then try again."
                )
            )
        except Exception:
            log.exception("unexpected join failure in guild %s", ctx.guild.id)
            await ctx.send(embed=ui.generic_error())

    @staticmethod
    def _resolve_channel(
        ctx: commands.Context, raw: str
    ) -> discord.VoiceChannel | None:
        """Look the channel up in the guild cache instead of hitting the API."""
        try:
            channel_id = int(str(raw).strip())
        except (TypeError, ValueError):
            return None
        return discord.utils.get(ctx.guild.voice_channels, id=channel_id)

    @join.autocomplete("channel")
    async def join_autocomplete(
        self, interaction: Interaction, current: str
    ) -> List[appCommand.Choice[str]]:
        needle = current.lower().strip()
        return [
            appCommand.Choice(name=channel.name[:100], value=str(channel.id))
            for channel in interaction.guild.voice_channels
            if needle in channel.name.lower()
        ][:25]  # Discord caps autocomplete at 25 choices.

    @join.error
    async def join_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(
                embed=ui.error(
                    "**Which channel?**\n"
                    "Use **`/join`** and pick one from the list — it's easier "
                    "than finding the channel ID for **`?join`**."
                )
            )
        else:
            log.error("join command error: %s", error)

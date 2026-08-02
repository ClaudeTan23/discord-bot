"""Bot entrypoint: configuration, cog registration and startup."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import discord
from discord import Game, Status
from discord.ext import commands
from dotenv import load_dotenv

from music_player import ui
from music_player.config import COMMAND_PREFIX, ENV_FILE, HELP_FILE
from music_player.join_channel import JoinChannel
from music_player.leave_channel import LeaveChannel
from music_player.player import Player
from music_player.state import MusicState
from music_player.ytdl import YouTubeService

log = logging.getLogger("music_bot")


def _load_help_text() -> str:
    """Read the help manual once at startup rather than on every invocation."""
    try:
        return HELP_FILE.read_text(encoding="utf-8")
    except OSError:
        log.exception("could not read help file at %s", HELP_FILE)
        return "Help manual is unavailable."


class MusicBot(commands.Bot):
    """Bot that wires one shared :class:`MusicState` into every cog."""

    def __init__(self) -> None:
        intents = discord.Intents.all()
        intents.message_content = True
        super().__init__(
            command_prefix=COMMAND_PREFIX,
            intents=intents,
            help_command=None,
            # Queue listings embed <@id> mentions; make it impossible for the
            # bot to ping a crowd, however a message is constructed.
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.music_state = MusicState()
        self.youtube = YouTubeService()
        self.help_text = _load_help_text()

    async def setup_hook(self) -> None:
        """Register cogs exactly once.

        The previous version did this in ``on_ready``, which Discord fires again
        after every reconnect - re-adding a cog raises and duplicates commands.
        """
        await self.add_cog(JoinChannel(self, self.music_state))
        await self.add_cog(LeaveChannel(self, self.music_state))
        await self.add_cog(Player(self, self.music_state, self.youtube))
        await self.tree.sync()
        log.info("cogs registered and command tree synced")

    async def on_ready(self) -> None:
        await self.change_presence(status=Status.idle, activity=Game("Deeznuts | /help"))
        log.info("%s online", self.user)

    async def close(self) -> None:
        """Tear down worker threads before the loop stops."""
        self.youtube.close()
        await super().close()

    async def on_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        # Commands with their own @cmd.error handler are already covered.
        if getattr(ctx.command, "has_error_handler", lambda: False)():
            return
        if isinstance(error, commands.CommandNotFound):
            return
        log.exception("unhandled command error in %s", ctx.command, exc_info=error)
        try:
            await ctx.send(embed=ui.generic_error())
        except discord.HTTPException:
            log.debug("could not deliver error message")


bot = MusicBot()


@bot.hybrid_command(name="help", description="Display all the commands and manuals")
async def help_command(ctx: commands.Context) -> None:
    embed = discord.Embed(
        colour=discord.Colour.dark_grey(),
        description=ctx.bot.help_text,
        title="All commands and manuals",
    )
    await ctx.send(embed=embed)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    # music_player.config loads .env at import time, before it reads any
    # setting; this is a no-op safety net if that ever changes.
    load_dotenv(ENV_FILE)
    token = os.environ.get("Bot-Token")
    if not token:
        log.error("Bot-Token is not set in the environment or %s", ENV_FILE)
        return 1

    try:
        bot.run(token, log_handler=None)
    except discord.LoginFailure:
        log.error("Discord rejected the bot token")
        return 1
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())

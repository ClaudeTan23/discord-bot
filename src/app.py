"""Bot entrypoint: configuration, cog registration and startup."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

import discord
from discord import Game, Status
from discord.ext import commands
from dotenv import load_dotenv

from music_player.errors import DELIVERY_FAILED
from music_player import logs
from music_player.cogs.player import Player
from music_player.cogs.playlists import Playlists
from music_player.cogs.voice import JoinChannel, LeaveChannel
from music_player.config import (
    COMMAND_PREFIX,
    ENV_FILE,
    HELP_FILE,
    PLAYLIST_DB,
    SYNC_GUILD_ID,
)
from music_player.services.library import PlaylistLibrary
from music_player.services.youtube import YouTubeService
from music_player.state import MusicState
from music_player.ui import embeds as ui
from music_player.ui.manual import HelpManual, send_manual

# Before anything is constructed: MusicBot() reads the help file and logs about
# it, and those records are worth keeping too.
logs.configure()

log = logging.getLogger("music_bot")


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
        self.help_manual = HelpManual(HELP_FILE)
        # Opens the database, creates the tables and imports any legacy JSON
        # store - all before the loop starts. Every query after this is a
        # coroutine that runs its SQL on a worker thread.
        self.playlists = PlaylistLibrary(PLAYLIST_DB)

    async def setup_hook(self) -> None:
        """Register cogs exactly once.

        The previous version did this in ``on_ready``, which Discord fires again
        after every reconnect - re-adding a cog raises and duplicates commands.
        """
        # The loop exists by now, so stray task failures can be captured too.
        logs.install_exception_hooks(self.loop)

        await self.add_cog(JoinChannel(self, self.music_state))
        await self.add_cog(LeaveChannel(self, self.music_state))
        # Playlists drives playback through the Player cog rather than its own
        # copy of the pipeline, so it is handed the instance directly.
        player = Player(self, self.music_state, self.youtube)
        await self.add_cog(player)
        await self.add_cog(
            Playlists(self, self.music_state, self.youtube, self.playlists, player)
        )
        await self._sync_commands()
        log.info("cogs registered and command tree synced")

    async def _sync_commands(self) -> None:
        """Publish the slash commands.

        The global sync is the one that matters - it reaches every server the
        bot is in - but Discord can take up to an hour to roll it out, during
        which a newly added command simply is not there. A guild sync lands
        immediately, so SYNC_GUILD_ID publishes to one server up front while
        the global sync catches up for everyone else.
        """
        if SYNC_GUILD_ID:
            guild = discord.Object(id=SYNC_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            try:
                synced = await self.tree.sync(guild=guild)
                log.info(
                    "published %d commands to guild %s immediately",
                    len(synced),
                    SYNC_GUILD_ID,
                )
            except discord.HTTPException:
                # Wrong id, or the bot is not in that server. The global sync
                # below still has to happen either way.
                log.warning(
                    "could not sync to guild %s - check SYNC_GUILD_ID",
                    SYNC_GUILD_ID,
                    exc_info=True,
                )
        await self.tree.sync()

    async def invoke(self, ctx: commands.Context) -> None:
        """Trace every command, and tag everything it logs along the way.

        Overriding ``invoke`` rather than listening to ``on_command`` puts this
        in the *same task* as the command body - which is what lets the bound
        context reach records logged deep inside ``youtube`` or ``player``. Hybrid
        commands route their slash invocations through here as well, so prefix
        and slash are both covered by the one hook.
        """
        if ctx.command is None:
            await super().invoke(ctx)
            return

        with logs.traced(
            _describe(ctx),
            guild=ctx.guild.name if ctx.guild else "DM",
            user=ctx.author,
            cmd=ctx.command.qualified_name,
        ):
            await super().invoke(ctx)

    async def on_ready(self) -> None:
        await self.change_presence(status=Status.idle, activity=Game("Deeznuts | /help"))
        log.info("%s online", self.user)

    async def close(self) -> None:
        """Tear down worker threads before the loop stops."""
        log.info("shutting down")
        self.youtube.close()
        self.playlists.close()
        await super().close()
        logging.shutdown()  # flush the day's file before the process goes

    async def on_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        # Commands with their own @cmd.error handler are already covered, as
        # is a cog that answers for its whole group in cog_command_error.
        if getattr(ctx.command, "has_error_handler", lambda: False)():
            return
        if ctx.cog is not None and ctx.cog.has_error_handler():
            return
        if isinstance(error, commands.CommandNotFound):
            # Logged, not silent: a burst of these is usually someone using a
            # command that was renamed, or a prefix collision with another bot.
            log.debug("unknown command: %r", getattr(ctx.message, "content", "")[:80])
            return
        log.exception("unhandled command error in %s", ctx.command, exc_info=error)
        await self._report_failure(ctx)

    @staticmethod
    async def _report_failure(ctx: commands.Context) -> None:
        """Tell the user something went wrong, by whatever route still works.

        A slash command is answered through its interaction, and that token
        dies: Discord expects acknowledgement within three seconds and returns
        ``10062 Unknown interaction`` afterwards. A gateway outage is exactly
        when that happens - events arrive late, the token is already stale, and
        the reply 404s.

        Falling back to the channel is what turns "the bot ignored me" into a
        visible failure. Only worth trying for an interaction: a prefix command
        was already sending to the channel, so a second attempt fails the same
        way.
        """
        try:
            await ctx.send(embed=ui.generic_error())
            return
        except DELIVERY_FAILED:
            if ctx.interaction is None:
                log.warning("could not deliver the error message", exc_info=True)
                return
            log.info("interaction is gone; replying in the channel instead")

        try:
            await ctx.channel.send(embed=ui.generic_error())
        except (*DELIVERY_FAILED, AttributeError):
            log.warning("could not deliver the error message", exc_info=True)

    async def on_error(self, event: str, *args: object, **kwargs: object) -> None:
        """Any event handler that raises. discord.py prints these to stderr."""
        log.exception("unhandled exception in event %s", event)


#: Cap on a single argument in the "run" line, so pasting a wall of text as a
#: URL cannot push the useful part of the log off the screen.
_ARG_LIMIT = 120


def _describe(ctx: commands.Context) -> str:
    """One line identifying an invocation: who, where, and with what.

    ``?add`` is recorded with the link it was given, so a failure report is
    reproducible from the log alone.

    Wrapped in a catch-all because this runs on the path to *every* command:
    an attribute that raises on ``str()`` must cost a vague trace line, not
    the command itself.
    """
    try:
        parts = [ctx.command.qualified_name]
        if ctx.kwargs:
            parts.append(
                " ".join(
                    f"{name}={str(value)[:_ARG_LIMIT]!r}"
                    for name, value in ctx.kwargs.items()
                )
            )
        parts.append(
            f"(guild={getattr(ctx.guild, 'id', '-')} "
            f"channel=#{getattr(ctx.channel, 'name', '?')} "
            f"via={'slash' if ctx.interaction is not None else 'prefix'})"
        )
        return " ".join(parts)
    except Exception:
        log.debug("could not describe an invocation", exc_info=True)
        return getattr(getattr(ctx, "command", None), "qualified_name", "?")


bot = MusicBot()


@bot.hybrid_command(name="help", description="Display all the commands and manuals")
async def help_command(ctx: commands.Context) -> None:
    await send_manual(ctx, ctx.bot.help_manual)


def main() -> int:
    logs.configure()
    logs.install_exception_hooks()
    logs.banner(prefix=COMMAND_PREFIX)

    # discord.py streams audio from a thread that must wake every 20ms, and it
    # sends packets back to back to catch up whenever it wakes late. yt-dlp's
    # extraction is pure-Python and CPU-heavy (the signature/nsig interpreter),
    # so a burst of lookups can hold the GIL well past that deadline. Checking
    # five times as often costs a little throughput on those threads and keeps
    # the audio thread on schedule.
    sys.setswitchinterval(0.001)

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

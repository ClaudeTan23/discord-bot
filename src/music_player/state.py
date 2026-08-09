"""Per-guild playback state.

This module replaces the six module-level dictionaries that the cogs used to
share by instantiating throwaway ``JoinChannel`` objects. Everything a guild
needs now lives on one :class:`GuildState`, keyed by integer guild id.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import discord

from music_player.config import DEFAULT_VOLUME, IDLE_DISCONNECT_SECONDS

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Track:
    """One queued song."""

    url: str
    title: str
    duration: int
    requester_id: int
    requester_name: str


class GuildState:
    """Voice connection, queue and playback flags for a single guild."""

    __slots__ = (
        "guild_id",
        "voice",
        "queue",
        "volume",
        "skip_requested",
        "suppress_advance",
        "starting",
        "playback_started",
        "paused_at",
        "paused_total",
        "retried_url",
        "controls",
        "_idle_task",
    )

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        self.voice: Optional[discord.VoiceClient] = None
        self.queue: List[Track] = []
        self.volume: float = DEFAULT_VOLUME
        # Set by ?skip so the after-callback does not advance a second time.
        self.skip_requested = False
        # Set by ?pause / ?leave / ?stop so the after-callback stays put.
        self.suppress_advance = False
        # True while a track's stream URL is being resolved. Guilds are
        # independent, but a single guild has one audio output, so two
        # concurrent ?play calls must not both start a track.
        self.starting = False
        # ``time.monotonic()`` when the current track's audio started, so a
        # track that ends far too early can be told apart from one that
        # finished. Both look identical to the voice after-callback.
        self.playback_started: float = 0.0
        # Pause bookkeeping, so the progress bar shows time *heard* rather than
        # time since the track started. Without it, a song paused for a minute
        # comes back claiming it is a minute further along than it is.
        self.paused_at: Optional[float] = None
        self.paused_total: float = 0.0
        # The one track url already retried after a failed stream, so a URL
        # that is genuinely dead cannot loop.
        self.retried_url: Optional[str] = None
        # The live button strip under the current Now Playing message, greyed
        # out when this track stops being the current one.
        self.controls = None
        self._idle_task: Optional[asyncio.Task] = None

    # -- connection state ---------------------------------------------------

    @property
    def connected(self) -> bool:
        return self.voice is not None and self.voice.is_connected()

    @property
    def playing(self) -> bool:
        return self.connected and self.voice.is_playing()

    @property
    def paused(self) -> bool:
        return self.connected and self.voice.is_paused()

    @property
    def current(self) -> Optional[Track]:
        return self.queue[0] if self.queue else None

    # -- playback position --------------------------------------------------

    @property
    def elapsed(self) -> float:
        """Seconds of audio the channel has actually heard of this track.

        Frozen while paused and net of every previous pause, so it can be
        compared against the track's duration to draw a progress bar.
        """
        if not self.playback_started:
            return 0.0
        end = self.paused_at if self.paused_at is not None else time.monotonic()
        return max(0.0, end - self.playback_started - self.paused_total)

    def mark_started(self) -> None:
        self.playback_started = time.monotonic()
        self.paused_at = None
        self.paused_total = 0.0

    def mark_paused(self) -> None:
        if self.paused_at is None:
            self.paused_at = time.monotonic()

    def mark_resumed(self) -> None:
        if self.paused_at is not None:
            self.paused_total += time.monotonic() - self.paused_at
            self.paused_at = None

    # -- attached views -----------------------------------------------------

    async def retire_controls(self) -> None:
        """Grey out the buttons under the previous Now Playing message."""
        view, self.controls = self.controls, None
        if view is None:
            return
        try:
            await view.retire()
        except Exception:
            log.debug("could not retire controls for guild %s", self.guild_id,
                      exc_info=True)

    # -- idle disconnect ----------------------------------------------------

    def schedule_idle_disconnect(self, delay: float = IDLE_DISCONNECT_SECONDS) -> None:
        """Leave the voice channel after ``delay`` seconds without playback.

        Uses an asyncio task rather than ``threading.Timer`` so the bot does not
        spawn one OS thread per guild per song.
        """
        self.cancel_idle_disconnect()
        self._idle_task = asyncio.create_task(self._disconnect_when_idle(delay))

    def cancel_idle_disconnect(self) -> None:
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    async def _disconnect_when_idle(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            if self.connected and not self.playing:
                await self.disconnect()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("idle disconnect failed for guild %s", self.guild_id)

    # -- teardown -----------------------------------------------------------

    async def disconnect(self) -> None:
        """Drop the voice connection. Safe to call when already disconnected."""
        self.cancel_idle_disconnect()
        voice, self.voice = self.voice, None
        if voice is not None:
            try:
                await voice.disconnect()
            except Exception:
                log.exception("disconnect failed for guild %s", self.guild_id)

    def reset(self) -> None:
        self.queue.clear()
        self.skip_requested = False
        self.suppress_advance = False
        self.starting = False
        self.playback_started = 0.0
        self.paused_at = None
        self.paused_total = 0.0
        self.retried_url = None
        self.controls = None


class MusicState:
    """Registry of :class:`GuildState`, injected into every cog."""

    __slots__ = ("_guilds",)

    def __init__(self) -> None:
        self._guilds: Dict[int, GuildState] = {}

    def get(self, guild_id: int) -> GuildState:
        state = self._guilds.get(guild_id)
        if state is None:
            state = GuildState(guild_id)
            self._guilds[guild_id] = state
        return state

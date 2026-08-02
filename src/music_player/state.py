"""Per-guild playback state.

This module replaces the six module-level dictionaries that the cogs used to
share by instantiating throwaway ``JoinChannel`` objects. Everything a guild
needs now lives on one :class:`GuildState`, keyed by integer guild id.
"""

from __future__ import annotations

import asyncio
import logging
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
        "retried_url",
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
        # The one track url already retried after a failed stream, so a URL
        # that is genuinely dead cannot loop.
        self.retried_url: Optional[str] = None
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
        self.retried_url = None


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

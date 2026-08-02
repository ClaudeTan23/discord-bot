"""Embed construction and display formatting.

Keeping presentation here means the cogs read as playback logic rather than as
a wall of ``Embed(colour=..., description=...)`` calls.
"""

from __future__ import annotations

import asyncio
import logging
import math
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional, Sequence

import discord
from discord import Embed

from music_player.config import (
    FALLBACK_AVATAR,
    NOW_PLAYING_ICON,
    QUEUE_PAGE_SIZE,
)
from music_player.state import Track

log = logging.getLogger(__name__)

_SPIRAL = ":face_with_spiral_eyes:"

#: Discord shows a typing indicator for ~10s. discord.py refreshes every 5s;
#: 8s keeps it continuous with ~40% fewer requests.
_TYPING_REFRESH = 8.0


async def _typing_loop(channel: discord.abc.Messageable) -> None:
    """Keep the typing indicator alive until cancelled."""
    try:
        while True:
            await channel.typing()
            await asyncio.sleep(_TYPING_REFRESH)
    except asyncio.CancelledError:
        pass
    except discord.HTTPException:
        # A missing typing indicator must never break the command itself.
        log.debug("typing indicator failed", exc_info=True)


@asynccontextmanager
async def thinking(ctx) -> AsyncIterator[None]:
    """Signal "working on it" without putting a REST call in front of the work.

    Slash commands *must* be acknowledged within 3 seconds, so an interaction is
    deferred up front - that call is required and happens once.

    Prefix commands are different. ``ctx.typing()`` awaits an HTTP round trip
    before the body starts and then re-sends every 5 seconds, so every ``?add``
    paid that latency even on a cache hit. Here the indicator runs alongside the
    work instead: the response is never delayed by it, and a fast path cancels
    the task before the request is even issued.
    """
    if ctx.interaction is not None:
        await ctx.typing()  # DeferTyping -> interaction.response.defer()
        yield
        return

    task = asyncio.create_task(_typing_loop(ctx.channel))
    try:
        yield
    finally:
        task.cancel()


def format_duration(seconds: int) -> str:
    """``213 -> '3:33'``, ``9350 -> '2:35:50'``."""
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def track_link(track: Track) -> str:
    """Render a track as a non-embedding markdown link.

    The title is used verbatim. Escaping it with backslashes looked correct in
    theory but Discord does not unescape inside a ``[label](url)`` link, so a
    title like "スパークル [original ver.]" rendered with visible ``\\[`` and
    ``\\]``. Titles are display-only, so raw text is the right trade.
    """
    return f"[{track.title}](<{track.url}>)"


def total_pages(item_count: int, page_size: int = QUEUE_PAGE_SIZE) -> int:
    return max(1, math.ceil(item_count / page_size))


# -- generic builders -------------------------------------------------------


def error(description: str) -> Embed:
    return Embed(colour=discord.Colour.brand_red(), description=description)


def success(description: str) -> Embed:
    return Embed(colour=discord.Colour.brand_green(), description=description)


def notice(description: str) -> Embed:
    return Embed(colour=discord.Colour.blurple(), description=description)


def neutral(description: str) -> Embed:
    return Embed(colour=discord.Colour.dark_magenta(), description=description)


# -- specific messages ------------------------------------------------------


def not_in_voice() -> Embed:
    return error(f"Not in voice channel {_SPIRAL}")


def empty_queue() -> Embed:
    return neutral("Doesn't have any song in the queue.")


def generic_error() -> Embed:
    return error(f"Error occurred {_SPIRAL}")


def invalid_link() -> Embed:
    return error(f"Invalid link or unavailable video/playlist {_SPIRAL}")


def all_played() -> Embed:
    return Embed(
        colour=discord.Colour.green(), description="Have played all of the songs."
    )


def added(track: Track, extra_count: int = 0, unavailable: int = 0) -> Embed:
    """Confirmation for ``?add``; ``extra_count`` covers the playlist case.

    ``unavailable`` is stated rather than swallowed: a playlist whose deleted
    and copyright-struck entries are dropped in silence looks like the bot lost
    songs, or skipped them at random once playback reached that point.
    """
    link = track_link(track)
    if extra_count > 0:
        body = f"Added {link} and {extra_count} songs in to playlist."
    else:
        body = f"Added {link} in to playlist."
    if unavailable > 0:
        body += f"\nSkipped {unavailable} unavailable video(s)."
    return Embed(colour=discord.Colour.dark_blue(), description=body)


def now_playing(
    *,
    title: str,
    url: str,
    duration: int,
    thumbnail: Optional[str],
    user: discord.abc.User,
) -> Embed:
    embed = Embed(
        colour=discord.Colour.blurple(),
        title=title,
        description=f"Duration: {format_duration(duration)}",
        url=url,
    )
    embed.set_author(name="Now playing", icon_url=NOW_PLAYING_ICON, url=url)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    avatar_url = user.avatar.url if user.avatar else FALLBACK_AVATAR
    embed.set_footer(text=f"Requested by {user.name}", icon_url=avatar_url)
    return embed


def queue_page(
    tracks: Sequence[Track],
    page: int,
    *,
    status: str = "",
    page_size: int = QUEUE_PAGE_SIZE,
) -> Embed:
    """Render one page of the queue.

    Only the requested slice is formatted, rather than walking the whole queue
    and discarding everything past the tenth entry.
    """
    pages = total_pages(len(tracks), page_size)
    start = (page - 1) * page_size
    window = tracks[start : start + page_size]

    lines = []
    for offset, track in enumerate(window):
        position = start + offset + 1
        marker = status if position == 1 and status else ""
        lines.append(
            f"{position}. {marker}{track_link(track)} "
            f"({format_duration(track.duration)}) - Requested by <@{track.requester_id}>"
        )

    embed = Embed(
        colour=discord.Colour.dark_magenta(),
        description="\n".join(lines) or "Doesn't have any song in the queue.",
        title="Queue",
    )
    embed.set_footer(text=f"Page {page}/{pages}")
    return embed

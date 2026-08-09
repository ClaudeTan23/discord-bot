"""Embed construction and display formatting.

Keeping presentation here means the cogs read as playback logic rather than as
a wall of ``Embed(colour=..., description=...)`` calls.

Three rules hold the look together:

* **Colour carries meaning, not decoration.** See the palette in ``config``.
* **The author line is an eyebrow label** ("Now playing", "Added to queue") and
  the title is the content. Users scan the eyebrow to know what happened and
  the title to know what it happened to.
* **Every dead end offers the way out.** An empty queue says how to fill it; a
  disconnected bot says how to invite it. A message that only states a problem
  makes the user go and read the manual.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Iterable, Optional, Sequence

import discord
from discord import Embed

from music_player.config import (
    COLOUR_ERROR,
    COLOUR_NEUTRAL,
    COLOUR_PAUSED,
    COLOUR_PLAYING,
    COLOUR_QUEUED,
    COLOUR_SUCCESS,
    FALLBACK_AVATAR,
    PROGRESS_BAR_WIDTH,
    QUEUE_PAGE_SIZE,
)
from music_player.state import Track
from music_player.ytdl import video_id

#: YouTube serves a still for every video at a fixed URL keyed on its id.
#: ``mqdefault`` is the 320x180 frame: always present - unlike
#: ``maxresdefault``, which 404s on plenty of videos - and genuinely 16:9, so
#: it fills Discord's thumbnail slot without the black bars ``hqdefault``
#: bakes into its 4:3 canvas.
_ARTWORK = "https://img.youtube.com/vi/{}/mqdefault.jpg"


def artwork(url: str) -> Optional[str]:
    """Cover art for a queued track, derived from its link.

    :class:`Track` carries no thumbnail - the queue is built from a flat
    playlist listing, and resolving one per song would cost a network
    extraction each just to render a list. This gets the same image for free.
    """
    identifier = video_id(url)
    return _ARTWORK.format(identifier) if identifier else None

log = logging.getLogger(__name__)

#: Discord shows a typing indicator for ~10s. discord.py refreshes every 5s;
#: 8s keeps it continuous with ~40% fewer requests.
_TYPING_REFRESH = 8.0

#: Filled / empty cells of the progress bar.
_BAR_FILLED = "▰"
_BAR_EMPTY = "▱"

#: Marker shown against the track at the head of the queue.
PLAYING_MARKER = "▶"
PAUSED_MARKER = "⏸"

#: What the marker is called when the queue heads its live track.
_STATUS_LABEL = {PLAYING_MARKER: "Now playing", PAUSED_MARKER: "Paused"}


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


# -- formatting -------------------------------------------------------------


def format_duration(seconds: int) -> str:
    """``213 -> '3:33'``, ``9350 -> '2:35:50'``."""
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_human(seconds: int) -> str:
    """``2500 -> '41 min'``. For durations a user reads rather than tracks.

    ``3:33`` is the right format against a progress bar, where it is compared
    to another timestamp. It is the wrong format for "how long until the queue
    runs out", which nobody wants to read to the second.
    """
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} sec"
    minutes, _ = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if not hours:
        return f"{minutes} min"
    if not minutes:
        return f"{hours} hr"
    return f"{hours} hr {minutes} min"


def total_duration(tracks: Iterable[Track]) -> int:
    return sum(max(0, track.duration) for track in tracks)


def progress_bar(
    elapsed: float, duration: int, *, width: int = PROGRESS_BAR_WIDTH
) -> str:
    """``▰▰▰▰▱▱▱▱▱▱▱▱▱▱ 1:23 / 3:33``.

    A track of unknown length gets the elapsed time on its own: a bar with no
    end to measure against would be inventing a position.
    """
    if duration <= 0:
        return f"`{format_duration(int(elapsed))}`"
    ratio = min(1.0, max(0.0, elapsed / duration))
    filled = int(round(ratio * width))
    bar = _BAR_FILLED * filled + _BAR_EMPTY * (width - filled)
    return f"`{bar}`  `{format_duration(int(elapsed))} / {format_duration(duration)}`"


def playback_line(
    elapsed: float, duration: int, *, paused: bool = False, now: Optional[float] = None
) -> str:
    """The position read-out under a Now Playing title.

    An embed is a *snapshot*: whatever is drawn here is frozen at the moment
    the message is sent. A bar posted the instant a song starts therefore sits
    at 0:00 for the whole track, and reads as an empty gauge to anyone
    scrolling past two minutes later.

    Two things follow from that:

    * The bar is only drawn once there is progress to draw. At the start,
      the duration alone is the honest statement.
    * The finish time is a Discord ``<t:...:R>`` timestamp, which the *client*
      renders and keeps ticking - "in 2 minutes", then "in a minute", with no
      edits from us. It is the one part of the embed that stays true on its
      own.

    A paused track gets no finish time: the countdown would keep running
    against audio that is not playing.
    """
    line = (
        progress_bar(elapsed, duration)
        if elapsed > 0
        else f"`{format_duration(duration)}`"
    )
    if paused or duration <= 0:
        return line

    remaining = max(0, duration - int(elapsed))
    ends_at = int((now if now is not None else time.time()) + remaining)
    return f"{line}\nEnds <t:{ends_at}:R>"


#: Queue rows are one line each or the list stops being scannable. YouTube
#: titles routinely run past 80 characters, and the overflow is nearly always
#: a "(Official Music Video) [Remastered in 4K]" tail carrying nothing the
#: queue needs - the link is there for anyone who wants the full thing.
TITLE_LIMIT = 45

#: Discord's thumbnail sits in the embed's right-hand column for the *whole*
#: embed height, not just the top - so every row loses width, not only the
#: ones beside the image. Rows are clipped harder on a page showing artwork to
#: buy that back, because a wrapped row costs more than a shorter title.
TITLE_LIMIT_NARROW = 34

#: Discord rejects an embed title past 256 characters. Real YouTube titles stop
#: well short, so this only ever fires on a pathological playlist name.
_EMBED_TITLE_LIMIT = 256


def clip(title: str, limit: int = TITLE_LIMIT) -> str:
    """Shorten a title to ``limit`` characters without breaking markdown."""
    title = title.strip()
    if len(title) <= limit:
        return title

    clipped = title[:limit].rstrip()
    # A cut landing inside "[Remastered in 4K]" leaves an unmatched bracket,
    # which breaks the [label](url) link the title sits inside. Back up past
    # the opener rather than emit markup Discord will render as raw text.
    for opener, closer in (("[", "]"), ("(", ")")):
        if clipped.count(opener) > clipped.count(closer):
            clipped = clipped[: clipped.rfind(opener)].rstrip()

    # Everything up to the limit was one long bracketed run; a hard cut beats
    # an empty label.
    if not clipped:
        clipped = title[:limit].rstrip()
    return clipped + "…"


def track_link(track: Track, *, limit: Optional[int] = None) -> str:
    """Render a track as a non-embedding markdown link.

    The title is used verbatim. Escaping it with backslashes looked correct in
    theory but Discord does not unescape inside a ``[label](url)`` link, so a
    title like "スパークル [original ver.]" rendered with visible ``\\[`` and
    ``\\]``. Titles are display-only, so raw text is the right trade.

    ``limit`` shortens the *label* only - the link still points at the full
    video, so nothing is lost by trimming a row to fit.
    """
    label = clip(track.title, limit) if limit else track.title
    return f"[{label}](<{track.url}>)"


def total_pages(item_count: int, page_size: int = QUEUE_PAGE_SIZE) -> int:
    return max(1, math.ceil(item_count / page_size))


# -- generic builders -------------------------------------------------------


def error(description: str) -> Embed:
    return Embed(colour=COLOUR_ERROR, description=description)


def success(description: str) -> Embed:
    return Embed(colour=COLOUR_SUCCESS, description=description)


def notice(description: str) -> Embed:
    return Embed(colour=COLOUR_PLAYING, description=description)


def neutral(description: str) -> Embed:
    return Embed(colour=COLOUR_NEUTRAL, description=description)


# -- dead ends, each with the way out ---------------------------------------


def not_in_voice() -> Embed:
    return error(
        "**I'm not in a voice channel.**\n"
        "Use **`/join`** and pick your channel to bring me in."
    )


def empty_queue() -> Embed:
    return neutral(
        "**Nothing in the queue yet.**\n"
        "Add a song with **`?add`** and a YouTube link."
    )


def generic_error() -> Embed:
    return error(
        "**Something went wrong.**\nTry that again in a moment."
    )


def invalid_link() -> Embed:
    return error(
        "**I couldn't read that link.**\n"
        "It needs to be a YouTube video or playlist that's public and still up."
    )


def nothing_playing() -> Embed:
    return error(
        "**Nothing is playing.**\nStart the queue with **`?play`**."
    )


def no_such_page(asked: int, pages: int) -> Embed:
    """A bad page number should say what the valid range *is*."""
    return error(
        f"**There's no page {asked}.**\n"
        f"The queue runs to page **{pages}**."
    )


def no_such_song(asked: int, total: int) -> Embed:
    return error(
        f"**There's no song {asked} in the queue.**\n"
        f"There {'is' if total == 1 else 'are'} **{total}** — run **`?queue`** "
        "to see the numbers."
    )


def all_played() -> Embed:
    embed = Embed(
        colour=COLOUR_NEUTRAL,
        description=(
            "**That's the whole queue.**\n"
            "Add more with **`?add`** — I'll wait here for a minute before "
            "leaving."
        ),
    )
    embed.set_author(name="Queue finished")
    return embed


# -- confirmations ----------------------------------------------------------


def _landing_note(position: int, starts_in: Optional[int]) -> str:
    """Where an added track landed, and when it will actually be heard.

    "Added" on its own leaves the real question open - a song dropped at #40
    of a three-hour queue is not the same event as one that plays next.
    ``starts_in`` is ``None`` when nothing is playing, because a queue that
    is not advancing cannot honestly be given a countdown.
    """
    if position <= 0:
        return ""
    if starts_in is None:
        if position == 1:
            return "Next up — run ?play to start"
        return f"#{position} in queue"
    if starts_in <= 0:
        return f"#{position} in queue · plays next"
    return f"#{position} in queue · about {format_human(starts_in)} away"


def added(
    track: Track,
    extra_count: int = 0,
    unavailable: int = 0,
    *,
    playlist_title: Optional[str] = None,
    playlist_url: Optional[str] = None,
    total_seconds: int = 0,
    position: int = 0,
    starts_in: Optional[int] = None,
) -> Embed:
    """Confirmation for ``?add``; ``extra_count`` covers the playlist case.

    A playlist is named by *its own* title rather than by whichever song
    happens to be first - "Added 160 songs" above an unrelated song title
    reads as though the bot queued the wrong thing. Its total running time is
    the other half of that: 160 songs is meaningless until you know whether
    it is forty minutes or nine hours.

    ``unavailable`` is stated rather than swallowed: a playlist whose deleted
    and copyright-struck entries are dropped in silence looks like the bot lost
    songs, or skipped them at random once playback reached that point.
    """
    count = extra_count + 1
    is_playlist = extra_count > 0

    embed = Embed(colour=COLOUR_QUEUED)

    if is_playlist:
        embed.set_author(name=f"Added {count} songs to the queue")
        # An untitled playlist is rare but real; the first song beats nothing.
        embed.title = clip(playlist_title or track.title, _EMBED_TITLE_LIMIT)
        embed.url = playlist_url or track.url
        embed.description = (
            f"**{count} songs** · {format_human(total_seconds or track.duration)}"
        )
        embed.add_field(
            name="First up",
            value=(
                f"{track_link(track, limit=TITLE_LIMIT)} · "
                f"`{format_duration(track.duration)}`"
            ),
            inline=False,
        )
    else:
        embed.set_author(name="Added to the queue")
        embed.title = clip(track.title, _EMBED_TITLE_LIMIT)
        embed.url = track.url
        embed.description = f"`{format_duration(track.duration)}`"

    if unavailable > 0:
        # Discord subtext: small muted type. The caveat stays visible without
        # competing with what was actually added.
        embed.description += (
            f"\n-# Skipped {unavailable} unavailable "
            f"video{'s' if unavailable != 1 else ''} — deleted, private, "
            "or blocked."
        )

    note = _landing_note(position, starts_in)
    if note:
        embed.set_footer(text=note)
    return embed


@dataclass(frozen=True, slots=True)
class NowPlaying:
    """Everything the Now Playing embed shows.

    A value object rather than a dozen keyword arguments, so the caller
    assembles the state once and this module stays purely about layout.
    """

    title: str
    url: str
    duration: int
    thumbnail: Optional[str]
    requester: Optional[discord.abc.User]
    volume: float
    #: 1-based position of this track in the queue, and the queue's length.
    position: int
    total: int
    up_next: Optional[Track]
    #: Seconds of audio left in the whole queue, this track included.
    remaining: int
    elapsed: float = 0.0
    paused: bool = False


def now_playing(np: NowPlaying) -> Embed:
    """The centrepiece embed: what is playing, and what happens next.

    Deliberately answers the three questions that otherwise cost a command
    each - how far in are we, what is after this, and how much is left - so
    ``?queue`` becomes something you run to *edit* the queue, not to read it.
    """
    embed = Embed(
        colour=COLOUR_PAUSED if np.paused else COLOUR_PLAYING,
        title=np.title,
        url=np.url,
        description=playback_line(np.elapsed, np.duration, paused=np.paused),
    )
    embed.set_author(name="Paused" if np.paused else "Now playing")

    if np.thumbnail:
        embed.set_thumbnail(url=np.thumbnail)

    if np.requester is not None:
        embed.add_field(
            name="Requested by", value=np.requester.mention, inline=True
        )
    embed.add_field(name="Volume", value=f"{round(np.volume * 100)}%", inline=True)

    if np.up_next is not None:
        embed.add_field(
            name="Up next",
            value=(
                f"{track_link(np.up_next, limit=TITLE_LIMIT)} · "
                f"`{format_duration(np.up_next.duration)}`"
            ),
            inline=False,
        )

    footer = f"{np.position} of {np.total} in queue"
    if np.total > 1:
        footer += f" · {format_human(np.remaining)} left"
    icon = None
    if np.requester is not None:
        icon = np.requester.avatar.url if np.requester.avatar else FALLBACK_AVATAR
    embed.set_footer(text=footer, icon_url=icon)
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

    ``status`` is the marker put against the head of the queue (``▶`` / ``⏸``),
    shown only on the page that actually contains it.
    """
    pages = total_pages(len(tracks), page_size)
    start = (page - 1) * page_size
    window = tracks[start : start + page_size]

    # Artwork belongs to the live track, so it appears only on the page that
    # actually shows it. A thumbnail floating over page 4 would be claiming
    # something about a song that is nowhere on screen.
    art = artwork(window[0].url) if (status and start == 0 and window) else None
    limit = TITLE_LIMIT_NARROW if art else TITLE_LIMIT

    blocks: list[str] = []
    rows: list[str] = []

    for offset, track in enumerate(window):
        position = start + offset + 1
        if position == 1 and status:
            # The live track is not a list item - it is the thing the list is
            # queued behind. A blockquote gives it Discord's own vertical rule
            # and lifts it out of the numbered column entirely.
            blocks.append(
                f"{status} **{_STATUS_LABEL.get(status, 'Now playing')}**\n"
                f"> **{track_link(track, limit=limit)}**\n"
                f"> `{format_duration(track.duration)}` · <@{track.requester_id}>"
            )
        else:
            # Numbers are the ones ?skipto takes, so they stay absolute - the
            # song after the current one is 2, on every page.
            rows.append(
                f"`{position:>2}.` {track_link(track, limit=limit)} · "
                f"`{format_duration(track.duration)}` · <@{track.requester_id}>"
            )

    if rows:
        # A heading only earns its line when there are two things to tell
        # apart. Page 2, or a stopped queue, is just a list.
        blocks.append(("**Up next**\n" if blocks else "") + "\n".join(rows))

    embed = Embed(
        colour=COLOUR_NEUTRAL,
        title="Queue",
        description="\n\n".join(blocks),
    )
    if art:
        embed.set_thumbnail(url=art)

    songs = f"{len(tracks)} song{'s' if len(tracks) != 1 else ''}"
    embed.set_footer(
        text=f"Page {page}/{pages} · {songs} · "
        f"{format_human(total_duration(tracks))} total"
    )
    return embed


# -- short status replies ---------------------------------------------------


def paused() -> Embed:
    return Embed(colour=COLOUR_PAUSED, description="⏸  **Paused.**")


def resumed() -> Embed:
    return Embed(colour=COLOUR_PLAYING, description="▶  **Resumed.**")


def skipped(track: Optional[Track]) -> Embed:
    if track is None:
        return Embed(colour=COLOUR_NEUTRAL, description="⏭  **Skipped.**")
    return Embed(
        colour=COLOUR_NEUTRAL, description=f"⏭  Skipped {track_link(track)}"
    )


def jumping_to(track: Track) -> Embed:
    return Embed(
        colour=COLOUR_NEUTRAL,
        description=f"⏭  Jumping to {track_link(track)}",
    )


def volume_set(percent: int) -> Embed:
    #: A ten-cell meter makes "how loud is 40" answerable at a glance, which a
    #: bare number never is.
    filled = round(percent / 10)
    meter = _BAR_FILLED * filled + _BAR_EMPTY * (10 - filled)
    return Embed(
        colour=COLOUR_SUCCESS,
        description=f"🔊  Volume set to **{percent}%**\n`{meter}`",
    )


def cleared(kept_playing: bool) -> Embed:
    body = "🧹  **Queue cleared.**"
    if kept_playing:
        body += "\nThe song playing right now wasn't interrupted."
    return Embed(colour=COLOUR_SUCCESS, description=body)


def stopped() -> Embed:
    return Embed(
        colour=COLOUR_SUCCESS,
        description="⏹  **Stopped.** Queue cleared and I've left the channel.",
    )


def joined(channel_name: str) -> Embed:
    return Embed(
        colour=COLOUR_SUCCESS,
        description=(
            f"👋  Joined **{channel_name}**.\n"
            "Queue something up with **`?add`**, then **`?play`**."
        ),
    )


def moved(channel_name: str, *, was_playing: bool) -> Embed:
    body = f"➡️  Moved to **{channel_name}**."
    if was_playing:
        body += "\nPlayback is paused — **`?resume`** picks it back up."
    return Embed(colour=COLOUR_SUCCESS, description=body)


def left() -> Embed:
    return Embed(
        colour=COLOUR_SUCCESS,
        description=(
            "👋  **Left the voice channel.**\n"
            "The queue is still here — **`/join`** and **`?play`** to carry on."
        ),
    )

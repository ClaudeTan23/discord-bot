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
from typing import AsyncIterator, Iterable, Optional, Sequence, Tuple

import discord
from discord import Embed

from music_player.errors import DELIVERY_FAILED
from music_player.config import (
    COLOUR_ERROR,
    COLOUR_NEUTRAL,
    COLOUR_PAUSED,
    COLOUR_PLAYING,
    COLOUR_QUEUED,
    COLOUR_SUCCESS,
    FALLBACK_AVATAR,
    IDLE_DISCONNECT_SECONDS,
    PLAYLIST_NAME_LIMIT,
    PLAYLIST_PAGE_SIZE,
    PROGRESS_BAR_WIDTH,
    QUEUE_PAGE_SIZE,
)
from music_player.services.library import (
    InvalidName,
    NoSuchPlaylist,
    NoSuchSong,
    Playlist,
    PlaylistExists,
    PlaylistFull,
    PlaylistSummary,
    SavedTrack,
    StorageError,
    TooManyPlaylists,
    fold,
)
from music_player.state import Track
from music_player.services.youtube import video_id

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
    except DELIVERY_FAILED:
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


def total_duration(tracks: Iterable[Track | SavedTrack]) -> int:
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

    The bar is drawn from the first frame, empty, the way every music player
    draws one. It used to be held back until there was progress to show, on
    the grounds that an empty gauge on a message somebody scrolls past later
    reads as broken - but the message is repainted on every pause, resume and
    volume change while its buttons live, and once nothing is maintaining it
    any more it is frozen and labelled as such. A song that has just started
    showing ``0:00`` against its length is the plainer statement.

    The finish time is a Discord ``<t:...:R>`` timestamp, which the *client*
    renders and keeps ticking - "in 2 minutes", then "in a minute", with no
    edits from us. It is the one part of the embed that stays true on its own.

    A paused track gets no finish time: the countdown would keep running
    against audio that is not playing.
    """
    # progress_bar already falls back to the elapsed time alone when the
    # length is unknown, so there is nothing left to special-case here.
    line = progress_bar(elapsed, duration)
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


#: The bracket pairs a title can be cut inside of. Both break the
#: ``[label](url)`` a clipped title sits in, so both are balanced.
_PAIRS = (("[", "]"), ("(", ")"))


def _unmatched(text: str) -> Optional[int]:
    """Index of the earliest opener with no closer after it, or ``None``.

    The earliest, across *both* pairs, because they interfere: cutting back
    past a ``(`` can take away the ``]`` that was balancing an earlier ``[``.
    """
    earliest = None
    for opener, closer in _PAIRS:
        if text.count(opener) > text.count(closer):
            index = text.rfind(opener)
            earliest = index if earliest is None else min(earliest, index)
    return earliest


def clip(title: str, limit: int = TITLE_LIMIT) -> str:
    """Shorten a title to ``limit`` characters without breaking markdown.

    A cut landing inside "[Remastered in 4K]" leaves an unmatched bracket,
    which breaks the ``[label](url)`` link the title sits inside. Backing out
    of it has to repeat rather than run once: a title can carry several
    unmatched openers, and backing out of one pair can unbalance the other.
    """
    title = title.strip()
    if len(title) <= limit:
        return title

    # The ellipsis has to fit *inside* the budget. Slicing at the limit and
    # then appending returns limit+1 characters, which Discord rejects
    # outright at the 256-character embed title ceiling.
    clipped = title[: limit - 1].rstrip()
    while (cut := _unmatched(clipped)) is not None:
        clipped = clipped[:cut].rstrip()

    if not clipped:
        # Everything up to the limit was one long bracketed run. A hard cut
        # beats an empty label - `[](url)` renders as nothing at all - so take
        # one and drop the openers that would still be dangling.
        clipped = title[: limit - 1].rstrip()
        for opener, closer in _PAIRS:
            while clipped.count(opener) > clipped.count(closer):
                clipped = clipped.replace(opener, "", 1)
    return clipped + "…"


def track_link(track: Track | SavedTrack, *, limit: Optional[int] = None) -> str:
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


def _safe(name: str, limit: int = PLAYLIST_NAME_LIMIT) -> str:
    """A user-supplied playlist name, made fit to sit in an embed."""
    return clip(" ".join(str(name).split()), limit) or "—"


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
    #: The playlist this song arrived with, if any.
    source: Optional[str] = None
    #: Set once nothing is going to repaint this message again. The bar was
    #: always a snapshot; the finish time is the one part that would keep
    #: moving, and keep being wrong.
    stale: bool = False
    #: Whoever last pressed the pause button, shown while it is paused. The
    #: buttons edit this card in place rather than posting, so without this
    #: the most-used way to pause is the one that says least.
    actor: Optional[discord.abc.User] = None


def now_playing(np: NowPlaying) -> Embed:
    """The centrepiece embed: what is playing, and what happens next.

    Deliberately answers the three questions that otherwise cost a command
    each - how far in are we, what is after this, and how much is left - so
    ``?queue`` becomes something you run to *edit* the queue, not to read it.
    """
    embed = Embed(
        colour=COLOUR_PAUSED if np.paused else COLOUR_PLAYING,
        # Clipped like every other builder: a title past 256 characters makes
        # Discord reject the embed outright, and the song plays to silence.
        title=clip(np.title, _EMBED_TITLE_LIMIT),
        url=np.url,
        # A frozen message gets the same treatment as a paused one: the bar
        # stays, the countdown goes. A stale bar reads as "this was true when
        # posted"; a stale countdown reads as "this is true now", and is not.
        description=playback_line(
            np.elapsed, np.duration, paused=np.paused or np.stale
        ),
    )
    if np.paused:
        label, icon = _actor("Paused", np.actor)
    else:
        label, icon = "Now playing", None
    embed.set_author(name=label, icon_url=icon)

    if np.thumbnail:
        embed.set_thumbnail(url=np.thumbnail)

    # Three inline fields fill one row in Discord's layout, which is exactly
    # what "who / where from / how loud" wants to be.
    if np.requester is not None:
        embed.add_field(
            name="Requested by", value=np.requester.mention, inline=True
        )
    if np.source:
        embed.add_field(name="From", value=f"**{_safe(np.source)}**", inline=True)
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
    if np.stale:
        footer += " · no longer updating"
    icon = None
    if np.requester is not None:
        icon = np.requester.avatar.url if np.requester.avatar else FALLBACK_AVATAR
    embed.set_footer(text=footer, icon_url=icon)
    return embed


def _live_block(track: Track, status: str, limit: int) -> str:
    """The head of the queue, lifted out of the numbered list.

    The live track is not item one of the list - it is the thing the list is
    queued behind - so a blockquote gives it Discord's own vertical rule and
    takes it out of the numbered column. It names the playlist the song came
    from for the same reason Now Playing does: that is usually the answer to
    "why is this on".
    """
    meta = f"`{format_duration(track.duration)}` · <@{track.requester_id}>"
    if track.source:
        meta += f" · from **{_safe(track.source)}**"
    return (
        f"{status} **{_STATUS_LABEL.get(status, 'Now playing')}**\n"
        f"> **{track_link(track, limit=limit)}**\n"
        f"> {meta}"
    )


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
            blocks.append(_live_block(track, status, limit))
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


def _actor(name: str, who: Optional[discord.abc.User]) -> Tuple[str, Optional[str]]:
    """An eyebrow label and the avatar to sit beside it.

    ``"Paused"`` on its own leaves a channel wondering who did it, which is
    the first thing anyone asks when the music stops.
    """
    if who is None:
        return name, None
    label = f"{name} by {getattr(who, 'display_name', None) or who.name}"
    return label, (who.avatar.url if who.avatar else FALLBACK_AVATAR)


def paused(
    track: Optional[Track] = None,
    elapsed: float = 0.0,
    *,
    by: Optional[discord.abc.User] = None,
    leaves_in: float = IDLE_DISCONNECT_SECONDS,
) -> Embed:
    """Confirmation for ``?pause``.

    Three things a bare "Paused." left the user to guess at: *what* is paused,
    *where* it stopped, and that pausing starts a countdown - ``apply_pause``
    schedules the idle disconnect, so the bot leaves on its own shortly after.
    A listener who came back to an empty channel with no warning would think
    it crashed.

    Carries the song's cover, like every other card that names a song. Costs
    nothing to derive - :func:`artwork` reads it off the link - and it is what
    makes the reply recognisable at a glance while scrolling.
    """
    if track is None:
        return Embed(colour=COLOUR_PAUSED, description="⏸  **Paused.**")

    embed = Embed(
        colour=COLOUR_PAUSED,
        title=clip(track.title, _EMBED_TITLE_LIMIT),
        url=track.url,
        description=(
            f"{playback_line(elapsed, track.duration, paused=True)}\n"
            "**`?resume`** — or the **▶ Resume** button — picks up from "
            "exactly here."
        ),
    )
    label, icon = _actor("Paused", by)
    embed.set_author(name=label, icon_url=icon)
    cover = artwork(track.url)
    if cover:
        embed.set_thumbnail(url=cover)
    if leaves_in > 0:
        embed.set_footer(
            text=f"I'll leave the channel after about {format_human(int(leaves_in))} "
            "of nothing playing."
        )
    return embed


def resumed(
    track: Optional[Track] = None,
    elapsed: float = 0.0,
    *,
    by: Optional[discord.abc.User] = None,
) -> Embed:
    """Confirmation for ``?resume``.

    The mirror of :func:`paused`, and the finish time comes back with it:
    ``playback_line`` draws a live ``<t:...:R>`` stamp that the Discord client
    keeps counting down on its own, so the answer to "how long is left" is
    right there and stays right.
    """
    if track is None:
        return Embed(colour=COLOUR_PLAYING, description="▶  **Resumed.**")

    embed = Embed(
        colour=COLOUR_PLAYING,
        title=clip(track.title, _EMBED_TITLE_LIMIT),
        url=track.url,
        description=playback_line(elapsed, track.duration),
    )
    label, icon = _actor("Resumed", by)
    embed.set_author(name=label, icon_url=icon)
    cover = artwork(track.url)
    if cover:
        embed.set_thumbnail(url=cover)
    return embed


def skipped(
    track: Optional[Track] = None, *, by: Optional[discord.abc.User] = None
) -> Embed:
    """Confirmation for ``?skip`` and for the ⏭ button.

    Deliberately light. The Now Playing card for whatever comes next posts
    immediately after this one, and that is the thing to look at - giving the
    skipped song its own cover here would put the wrong artwork directly above
    the right one. The eyebrow carries who did it, which is the part a channel
    actually asks about when a song disappears.
    """
    label, icon = _actor("Skipped", by)
    embed = Embed(
        colour=COLOUR_NEUTRAL,
        description=(
            f"⏭  {track_link(track, limit=_EMBED_TITLE_LIMIT)}"
            if track
            else "⏭  Nothing was playing."
        ),
    )
    embed.set_author(name=label, icon_url=icon)
    return embed


def jumping_to(track: Track, *, by: Optional[discord.abc.User] = None) -> Embed:
    """Confirmation for ``?skipto`` - a skip that passes several songs."""
    label, icon = _actor("Skipped ahead", by)
    embed = Embed(
        colour=COLOUR_NEUTRAL,
        description=f"⏭  {track_link(track, limit=_EMBED_TITLE_LIMIT)}",
    )
    embed.set_author(name=label, icon_url=icon)
    return embed


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


# -- saved playlists --------------------------------------------------------
#
# A playlist belongs to the server it was made in, so these read as facts about
# "this server" rather than about the person who typed the command.
#
# A playlist name is whatever a user typed, so it is never dropped into an
# embed raw: _safe collapses the whitespace that would break a one-line row and
# clips it to the length the picker can actually show.


def _songs(count: int) -> str:
    return f"{count:,} song{'s' if count != 1 else ''}"


def playlist_summary(playlist: Playlist | PlaylistSummary) -> str:
    """``12 songs · 47 min`` - the two facts every playlist row repeats.

    Takes either a loaded playlist or the aggregate a listing reads, since
    both carry ``songs`` and ``duration`` and neither needs more than that.
    """
    if not playlist.songs:
        return "empty"
    return f"{_songs(playlist.songs)} · {format_human(playlist.duration)}"


def no_playlists() -> Embed:
    return neutral(
        "**This server hasn't saved any playlists yet.**\n"
        "Make one with **`?playlist create`** and a name, then fill it up with "
        "**`?playlist add`** — everyone here can add to it."
    )


def no_such_playlist(name: str) -> Embed:
    return error(
        f"**This server doesn't have a playlist called “{_safe(name)}”.**\n"
        "Run **`?playlist`** to see the ones it does have."
    )


def playlist_exists(name: str) -> Embed:
    return error(
        f"**This server already has a playlist called “{_safe(name)}”.**\n"
        "Pick another name, or add to that one with **`?playlist add`**."
    )


def too_many_playlists(limit: int) -> Embed:
    return error(
        f"**This server is at the limit of {limit} playlists.**\n"
        "Delete one that's done with — **`?playlist delete`** — to make room."
    )


def playlist_full(name: str, limit: int) -> Embed:
    return error(
        f"**“{_safe(name)}” is full at {limit:,} songs.**\n"
        "Take a few out with **`?playlist remove`**, or start another playlist."
    )


def bad_playlist_name(reason: str) -> Embed:
    if reason == "long":
        return error(
            f"**That name is too long.**\nKeep it under {PLAYLIST_NAME_LIMIT} "
            "characters so it fits in the picker."
        )
    return error(
        "**A playlist needs a name.**\n"
        "For example: **`?playlist create Late Night`**"
    )


def no_such_playlist_song(name: str, asked: int, total: int) -> Embed:
    if total == 0:
        return error(
            f"**“{_safe(name)}” is empty.**\n"
            "Put something in it with **`?playlist add`** first."
        )
    return error(
        f"**There's no song {asked} in “{_safe(name)}”.**\n"
        f"There {'is' if total == 1 else 'are'} **{total}** — run "
        "**`?playlist show`** to see the numbers."
    )


def playlist_is_empty(playlist: Playlist) -> Embed:
    return neutral(
        f"**“{_safe(playlist.name)}” is empty.**\n"
        "Add a song to it with **`?playlist add`** and a YouTube link."
    )


def pick_a_playlist() -> Embed:
    return neutral("**Choose a playlist first** — the menu is just above.")


def playlist_needs_a_server() -> Embed:
    return error(
        "**Playlists live in a server.**\n"
        "Each one belongs to the server it was made in, so there's nothing to "
        "reach from a direct message. Run this in the server instead."
    )


def not_your_playlist(playlist: Playlist) -> Embed:
    """Refusal for renaming or deleting somebody else's playlist.

    Anyone in the server can add and remove *songs*; throwing away a list the
    whole channel built is the part kept to whoever started it. The wording has
    to make that distinction, or it reads as though the playlists are not
    shared at all.
    """
    owner = (
        f"<@{playlist.created_by}> started it"
        if playlist.created_by is not None
        else "it has no recorded creator"
    )
    return error(
        f"**“{_safe(playlist.name)}” isn't yours to rename or delete.**\n"
        f"{owner}, so they or a moderator with **Manage Server** can. You can "
        "still add and remove songs with **`?playlist add`** and "
        "**`?playlist remove`**."
    )


def playlist_not_saved() -> Embed:
    """The database refused the change.

    It says nothing changed because nothing did: the write happens inside a
    transaction, so a failure rolls the whole thing back rather than leaving
    half of it applied.
    """
    return error(
        "**I couldn't save that.**\n"
        "Nothing was changed — try again in a moment."
    )


# -- playlist listings ------------------------------------------------------


def playlist_overview(
    playlists: Sequence[Playlist | PlaylistSummary],
    *,
    title: str = "Playlists",
    icon_url: Optional[str] = None,
    author: Optional[str] = None,
    playing: Optional[str] = None,
) -> Embed:
    """Everything this server has saved, one row each.

    ``playing`` is the name of the playlist the current song came from, if
    any. Marking that row is what connects the listing to what the channel is
    actually hearing - without it, "which of these is on right now" costs a
    separate command.

    Each row also credits whoever started the playlist, because that is the
    person - along with moderators - who can rename or delete it.
    """
    live = fold(playing) if playing else None

    rows = []
    for index, playlist in enumerate(playlists, start=1):
        marker = f"{PLAYING_MARKER} " if live and fold(playlist.name) == live else ""
        row = f"`{index:>2}.` {marker}**{_safe(playlist.name)}** · {playlist_summary(playlist)}"
        owner = getattr(playlist, "created_by", None)
        if owner is not None:
            row += f" · <@{owner}>"
        rows.append(row)

    embed = Embed(
        colour=COLOUR_NEUTRAL,
        title=title,
        description="\n".join(rows),
    )
    if author is not None:
        embed.set_author(name=author, icon_url=icon_url)

    songs = sum(playlist.songs for playlist in playlists)
    embed.set_footer(
        text=f"{len(playlists)} playlist{'s' if len(playlists) != 1 else ''} · "
        f"{_songs(songs)} · pick one below to open it"
    )
    return embed


def playlist_page(
    playlist: Playlist,
    page: int,
    *,
    page_size: int = PLAYLIST_PAGE_SIZE,
    playing_url: Optional[str] = None,
) -> Embed:
    """One page of a saved playlist.

    Numbered the way ``?playlist remove`` counts, so the number beside a row is
    the number that takes it out.
    """
    if not playlist.tracks:
        embed = playlist_is_empty(playlist)
        embed.set_author(name=_safe(playlist.name))
        return embed

    pages = total_pages(len(playlist.tracks), page_size)
    page = min(max(1, page), pages)
    start = (page - 1) * page_size
    window = playlist.tracks[start : start + page_size]

    # The playlist's own cover: its first song, whichever page is open. Unlike
    # the live queue there is no "now playing" here for it to misdescribe.
    art = artwork(playlist.tracks[0].url)
    limit = TITLE_LIMIT_NARROW if art else TITLE_LIMIT

    rows = []
    for offset, track in enumerate(window):
        row = (
            f"`{start + offset + 1:>2}.` {track_link(track, limit=limit)} · "
            f"`{format_duration(track.duration)}`"
        )
        if playing_url and track.url == playing_url:
            row += f"  {PLAYING_MARKER}"
        rows.append(row)

    embed = Embed(
        colour=COLOUR_NEUTRAL,
        title=clip(playlist.name, _EMBED_TITLE_LIMIT),
        description="\n".join(rows),
    )
    embed.set_author(name="Playlist")
    if art:
        embed.set_thumbnail(url=art)
    embed.set_footer(text=f"Page {page}/{pages} · {playlist_summary(playlist)}")
    return embed


# -- playlist confirmations -------------------------------------------------


def playlist_created(playlist: Playlist) -> Embed:
    name = _safe(playlist.name)
    return success(
        f"✅  **Created “{name}”.**\n"
        f"Fill it up with **`?playlist add \"{name}\" <YouTube link>`**.\n"
        "-# Anyone in this server can add to it. Only you or a moderator can "
        "rename or delete it."
    )


def playlist_deleted(name: str) -> Embed:
    return success(f"🗑  **Deleted “{_safe(name)}”.**")


def playlist_renamed(old: str, new: str) -> Embed:
    return success(f"✏️  **“{_safe(old)}” is now “{_safe(new)}”.**")


def playlist_added(
    playlist: Playlist,
    track: SavedTrack,
    *,
    added: int,
    requested: int,
    unavailable: int = 0,
) -> Embed:
    """Confirmation for ``?playlist add``.

    ``added`` and ``requested`` differ when a YouTube playlist was bigger than
    the room left. That is stated rather than quietly truncated - the tail
    would otherwise be found missing much later, with no explanation.
    """
    embed = Embed(colour=COLOUR_QUEUED)
    name = _safe(playlist.name)
    embed.title = clip(track.title, _EMBED_TITLE_LIMIT)
    embed.url = track.url

    if added > 1:
        embed.set_author(name=f"Added {added} songs to {name}")
        embed.description = f"**First up:** {track_link(track, limit=TITLE_LIMIT)}"
    else:
        embed.set_author(name=f"Added to {name}")
        embed.description = f"`{format_duration(track.duration)}`"

    if added < requested:
        embed.description += (
            f"\n-# {requested - added:,} more didn't fit — the playlist is full."
        )
    if unavailable > 0:
        embed.description += (
            f"\n-# Skipped {unavailable} unavailable "
            f"video{'s' if unavailable != 1 else ''} — deleted, private, or "
            "blocked."
        )

    embed.set_footer(text=f"“{name}” now holds {playlist_summary(playlist)}")
    return embed


def playlist_removed(playlist: Playlist, track: SavedTrack) -> Embed:
    embed = success(
        f"🗑  Removed {track_link(track, limit=TITLE_LIMIT)} from "
        f"**“{_safe(playlist.name)}”**"
    )
    embed.set_footer(text=f"{playlist_summary(playlist)} left")
    return embed


def _first_songs(playlist: Playlist) -> Tuple[Optional[SavedTrack], Optional[SavedTrack]]:
    """The song a playlist starts with, and the one after it."""
    tracks = playlist.tracks
    return (
        tracks[0] if tracks else None,
        tracks[1] if len(tracks) > 1 else None,
    )


def _song_field(track: SavedTrack) -> str:
    return f"{track_link(track, limit=TITLE_LIMIT)} · `{format_duration(track.duration)}`"


def playlist_playing(
    playlist: Playlist, count: int, *, connected: bool = True
) -> Embed:
    """Confirmation for ``?playlist play`` - the one that replaces the queue.

    Built to the same shape as the ``?add`` confirmation beside it: the
    playlist is the title, its cover is the first song's artwork, and the two
    songs about to be heard are named. Announcing "40 songs" without saying
    which one starts leaves the obvious question unanswered for the second or
    two before the Now Playing message arrives.

    The replacement is stated outright in the footer, along with the command
    that would have done the other thing: somebody who meant to line the
    playlist up behind what was already waiting needs to know it is gone.
    """
    first, second = _first_songs(playlist)

    embed = Embed(
        colour=COLOUR_PLAYING,
        title=clip(playlist.name, _EMBED_TITLE_LIMIT),
        description=f"**{_songs(count)}** · {format_human(playlist.duration)}",
    )
    embed.set_author(name=f"Playing {_songs(count)}")
    if first is not None:
        art = artwork(first.url)
        if art:
            embed.set_thumbnail(url=art)
        embed.add_field(name="Starting with", value=_song_field(first), inline=False)
    if second is not None:
        embed.add_field(name="Up next", value=_song_field(second), inline=False)

    if not connected:
        embed.description += (
            "\n-# I'm not in a voice channel yet — **`/join`**, then **`?play`**."
        )
    embed.set_footer(
        text="The queue was replaced · ?playlist queue adds to it instead"
    )
    return embed


def playlist_queued(
    playlist: Playlist,
    count: int,
    *,
    position: int = 0,
    starts_in: Optional[int] = None,
) -> Embed:
    """Confirmation for ``?playlist queue`` - the one that appends.

    Same shape again, but the question is different: nothing starts now, so
    what matters is which song this playlist opens with, where it landed, and
    how long until it is heard. ``_landing_note`` answers the last two.
    """
    first, second = _first_songs(playlist)

    embed = Embed(
        colour=COLOUR_QUEUED,
        title=clip(playlist.name, _EMBED_TITLE_LIMIT),
        description=f"**{_songs(count)}** · {format_human(playlist.duration)}",
    )
    embed.set_author(name=f"Added {_songs(count)} to the queue")
    if first is not None:
        art = artwork(first.url)
        if art:
            embed.set_thumbnail(url=art)
        embed.add_field(name="First up", value=_song_field(first), inline=False)
    if second is not None:
        embed.add_field(name="Then", value=_song_field(second), inline=False)

    note = _landing_note(position, starts_in)
    if note:
        embed.set_footer(text=note)
    return embed


def explain(error: Exception) -> discord.Embed:
    """Turn a library refusal into the embed that says what to do about it.

    The library raises facts; the wording lives in :mod:`music_player.ui` with
    every other message the bot sends. This is the one place they meet.

    :class:`StorageError` is folded in so a command needs one ``except`` rather
    than two: "the database said no" and "you asked for something impossible"
    are both just a message to the user by the time they get here.
    """
    if isinstance(error, StorageError):
        return playlist_not_saved()
    if isinstance(error, NoSuchPlaylist):
        return no_such_playlist(error.name)
    if isinstance(error, PlaylistExists):
        return playlist_exists(error.name)
    if isinstance(error, TooManyPlaylists):
        return too_many_playlists(error.limit)
    if isinstance(error, PlaylistFull):
        return playlist_full(error.name, error.limit)
    if isinstance(error, NoSuchSong):
        return no_such_playlist_song(error.name, error.asked, error.total)
    if isinstance(error, InvalidName):
        return bad_playlist_name(error.reason)
    log.error("unmapped playlist error: %r", error)
    return generic_error()

"""YouTube metadata and stream resolution.

Two things matter here:

* **Nothing blocks the event loop.** ``yt_dlp`` is synchronous and does network
  I/O, so every call runs on a dedicated thread pool.
  The previous implementation shelled out to the ``yt-dlp`` binary and called
  ``Popen.communicate()`` inline, freezing the whole bot for the duration.
* **No user string is ever interpolated into a command line.** Using the
  ``yt_dlp`` Python API removes that surface entirely.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional, Tuple, TypeVar
from urllib.parse import parse_qs, urlparse

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, YoutubeDLError

from music_player.config import (
    AUTOCOMPLETE_TIMEOUT,
    EXTRACTOR_WORKERS,
    METADATA_CACHE_SIZE,
    PREVIEW_CACHE_SIZE,
    STREAM_CACHE_SIZE,
    STREAM_DEFAULT_TTL,
    STREAM_MIN_VALIDITY,
    YOUTUBE_ID_LENGTH,
)

log = logging.getLogger(__name__)

T = TypeVar("T")

class _YtdlLogger:
    """Routes yt-dlp output into ``logging`` instead of stderr."""

    def debug(self, msg: str) -> None:
        log.debug("%s", msg)

    def info(self, msg: str) -> None:
        log.debug("%s", msg)

    def warning(self, msg: str) -> None:
        log.debug("%s", msg)

    def error(self, msg: str) -> None:
        # Expected for private/deleted videos; the caller turns this into a
        # user-facing message, so it is not an application-level error.
        log.info("yt-dlp: %s", msg)


_COMMON_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "ignoreerrors": True,
    "noplaylist": False,
    "logger": _YtdlLogger(),
}

#: Listing a playlist only needs titles/durations, not per-video extraction.
_FLAT_OPTS = {**_COMMON_OPTS, "extract_flat": "in_playlist"}

#: Resolving audio for playback needs the real format list.
#:
#: ``ignoreerrors`` is deliberately off here, unlike everywhere else. With it on,
#: yt-dlp returned ``None`` for *any* failure, so a transient 429 or a network
#: blip was reported as the same "no audio stream" as a permanently blocked
#: video - and the track was skipped for good with the real reason discarded.
_STREAM_OPTS = {**_COMMON_OPTS, "format": "bestaudio/best", "ignoreerrors": False}

#: Autocomplete only needs a label, so stop after the playlist's first entry
#: instead of walking every video in it.
_PREVIEW_OPTS = {**_FLAT_OPTS, "playlist_items": "1"}

_YOUTUBE_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
_PLAYABLE_PATH_PREFIXES = ("/shorts/", "/embed/", "/live/", "/v/")


class ExtractionError(RuntimeError):
    """Raised when a URL cannot be turned into playable metadata."""


@dataclass(frozen=True, slots=True)
class TrackInfo:
    url: str
    title: str
    duration: int


@dataclass(frozen=True, slots=True)
class FetchResult:
    """A resolved ``?add`` query: one video, or every video in a playlist."""

    entries: List[TrackInfo]
    playlist_title: Optional[str] = None
    #: Playlist entries dropped as unplayable (deleted, private, or pulled by a
    #: copyright claim). Reported to the user, who otherwise just sees a queue
    #: shorter than the playlist with no explanation.
    unavailable: int = 0

    @property
    def is_playlist(self) -> bool:
        return self.playlist_title is not None


@dataclass(frozen=True, slots=True)
class StreamInfo:
    stream_url: str
    title: str
    duration: int
    thumbnail: Optional[str]
    #: The headers yt-dlp used to obtain ``stream_url``. ffmpeg fetches that URL
    #: as a separate client, and googlevideo is more willing to 403 a request
    #: whose User-Agent does not match the one the URL was issued to.
    http_headers: Optional[Dict[str, str]] = None


def normalize_url(raw: str) -> str:
    """Canonicalise a user-supplied YouTube link.

    Handles the ``<...>`` wrapper Discord uses to suppress embeds, and collapses
    ``watch?v=<id>&list=<id>`` to the single video the user actually clicked.
    Bare playlist links are left alone so they still queue in full.
    """
    url = raw.strip()

    # Discord wraps links in <> to suppress the preview embed.
    if len(url) > 2 and url.startswith("<") and url.endswith(">"):
        url = url[1:-1].strip()

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return url

    host = (parsed.hostname or "").lower()
    query = parse_qs(parsed.query)

    # youtu.be/<id> -> watch?v=<id>
    if host in _YOUTUBE_SHORT_HOSTS:
        video_id = parsed.path.lstrip("/").split("/")[0]
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
        return url

    if not host.endswith("youtube.com"):
        return url

    # A ?v= parameter means the user linked a video, even if a playlist rides
    # along in &list=. Drop everything else (list, index, t, si, ...).
    video_ids = query.get("v")
    if video_ids and video_ids[0]:
        return f"https://www.youtube.com/watch?v={video_ids[0]}"

    return url


def is_youtube_url(url: str) -> bool:
    """Cheap, offline check that ``url`` is a YouTube link worth resolving.

    Autocomplete fires on every keystroke, so this gate keeps half-typed input
    from costing a network round trip.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False

    host = (parsed.hostname or "").lower()
    if host in _YOUTUBE_SHORT_HOSTS:
        return bool(parsed.path.strip("/"))
    if host != "youtube.com" and not host.endswith(".youtube.com"):
        return False

    query = parse_qs(parsed.query)
    if query.get("v") or query.get("list"):
        return True
    return parsed.path.startswith(_PLAYABLE_PATH_PREFIXES)


def is_complete_query(url: str) -> bool:
    """As :func:`is_youtube_url`, but also rejects a half-typed video id."""
    if not is_youtube_url(url):
        return False
    video_ids = parse_qs(urlparse(url).query).get("v")
    if video_ids:
        return len(video_ids[0]) == YOUTUBE_ID_LENGTH
    return True


def _stream_expiry(stream_url: str) -> float:
    """Absolute unix time at which a googlevideo URL stops working."""
    expire = parse_qs(urlparse(stream_url).query).get("expire")
    if expire:
        try:
            return float(expire[0])
        except (TypeError, ValueError):
            pass
    return time.time() + STREAM_DEFAULT_TTL


def _coerce_duration(value: object) -> Optional[int]:
    """yt-dlp reports duration as int, float, str or None depending on source."""
    if value is None:
        return None
    try:
        duration = int(float(value))
    except (TypeError, ValueError):
        return None
    return duration if duration > 0 else None


def _to_track(entry: dict) -> Optional[TrackInfo]:
    """Build a :class:`TrackInfo`, or ``None`` for unavailable videos."""
    if not entry:
        return None
    duration = _coerce_duration(entry.get("duration"))
    if duration is None:
        return None  # private, deleted, or a live stream with no fixed length
    url = entry.get("webpage_url") or entry.get("url")
    title = entry.get("title")
    if not url or not title:
        return None
    return TrackInfo(url=url, title=title, duration=duration)


class YouTubeService:
    """Async wrapper over ``yt_dlp`` with a bounded metadata cache."""

    __slots__ = (
        "_cache",
        "_preview_cache",
        "_stream_cache",
        "_inflight",
        "_executor",
    )

    def __init__(self, max_workers: int = EXTRACTOR_WORKERS) -> None:
        self._cache: "OrderedDict[str, FetchResult]" = OrderedDict()
        self._preview_cache: "OrderedDict[str, str]" = OrderedDict()
        self._stream_cache: "OrderedDict[str, Tuple[StreamInfo, float]]" = OrderedDict()
        # One shared task per key, so N callers wanting the same thing at the
        # same moment cost one extraction, not N.
        self._inflight: Dict[str, "asyncio.Task"] = {}
        # A private pool: extraction for one guild cannot exhaust the threads
        # another guild (or anything else calling asyncio.to_thread) needs.
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="ytdl"
        )

    def close(self) -> None:
        """Release the worker threads. Call on bot shutdown."""
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def _run(self, url: str, opts: dict) -> Optional[dict]:
        """Run a blocking extraction on the dedicated pool."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._extract, url, opts)

    async def _single_flight(
        self, key: str, factory: Callable[[], Awaitable[T]]
    ) -> T:
        """Collapse concurrent requests for ``key`` into one extraction.

        Shielded, so a caller that gives up (an autocomplete deadline, a
        cancelled command) leaves the work running for everyone still waiting.
        """
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(factory())
            self._inflight[key] = task
            task.add_done_callback(lambda t, k=key: self._release_inflight(t, k))
        return await asyncio.shield(task)

    # -- cache --------------------------------------------------------------

    def _cache_get(self, url: str) -> Optional[FetchResult]:
        result = self._cache.get(url)
        if result is not None:
            self._cache.move_to_end(url)
        return result

    def _cache_put(self, url: str, result: FetchResult) -> None:
        self._cache[url] = result
        self._cache.move_to_end(url)
        while len(self._cache) > METADATA_CACHE_SIZE:
            self._cache.popitem(last=False)

    # -- extraction ---------------------------------------------------------

    @staticmethod
    def _extract(url: str, opts: dict) -> Optional[dict]:
        """Blocking yt-dlp call. Always run via :meth:`_run`.

        A fresh ``YoutubeDL`` per call keeps concurrent worker threads from
        sharing mutable extractor state.
        """
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    async def fetch(self, raw_url: str) -> FetchResult:
        """Resolve a user query into one or more playable tracks.

        Raises:
            ExtractionError: the link is invalid, private, or has no usable video.
        """
        url = normalize_url(raw_url)
        if not url:
            raise ExtractionError("empty url")

        cached = self._cache_get(url)
        if cached is not None:
            return cached

        # Several guilds adding the same link at once share one extraction.
        return await self._single_flight(f"fetch:{url}", lambda: self._fetch(url))

    async def _fetch(self, url: str) -> FetchResult:
        cached = self._cache_get(url)
        if cached is not None:
            return cached

        try:
            info = await self._run(url, _FLAT_OPTS)
        except DownloadError as exc:
            raise ExtractionError(str(exc)) from exc

        if not info:
            raise ExtractionError(f"no metadata for {url}")

        if info.get("_type") == "playlist":
            raw = info.get("entries") or []
            entries = [t for t in map(_to_track, raw) if t]
            if not entries:
                raise ExtractionError("playlist contained no playable videos")
            result = FetchResult(
                entries=entries,
                playlist_title=info.get("title") or "",
                unavailable=len(raw) - len(entries),
            )
        else:
            track = _to_track(info)
            if track is None:
                raise ExtractionError(f"video is unavailable: {url}")
            result = FetchResult(entries=[track])

        self._cache_put(url, result)
        return result

    # -- autocomplete -------------------------------------------------------

    async def preview(
        self, raw_url: str, *, timeout: float = AUTOCOMPLETE_TIMEOUT
    ) -> Optional[str]:
        """Return a display label for ``raw_url``, or ``None``.

        Built for autocomplete, which Discord abandons after 3 seconds. Three
        things keep it inside that budget:

        * half-typed input is rejected offline, with no network call at all;
        * a playlist stops after its first entry instead of every video;
        * the lookup is shielded, so a caller that times out still leaves the
          extraction running - the next keystroke then hits a warm cache.
        """
        url = normalize_url(raw_url)
        if not is_complete_query(url):
            return None

        cached = self._preview_cache.get(url)
        if cached is not None:
            self._preview_cache.move_to_end(url)
            return cached

        try:
            return await asyncio.wait_for(
                self._single_flight(f"preview:{url}", lambda: self._fetch_preview(url)),
                timeout,
            )
        except asyncio.TimeoutError:
            log.debug("preview timed out after %.1fs for %s", timeout, url)
            return None
        except Exception:
            return None

    def _release_inflight(self, task: "asyncio.Task", key: str) -> None:
        self._inflight.pop(key, None)
        # Retrieve any exception so asyncio does not log it as never-retrieved.
        if not task.cancelled() and task.exception() is not None:
            log.debug("preview failed for %s: %s", key, task.exception())

    async def _fetch_preview(self, url: str) -> Optional[str]:
        info = await self._run(url, _PREVIEW_OPTS)
        if not info:
            return None
        # For a playlist this is the playlist title; for a video, the title.
        label = info.get("title")
        if not label:
            return None
        self._preview_cache[url] = label
        self._preview_cache.move_to_end(url)
        while len(self._preview_cache) > PREVIEW_CACHE_SIZE:
            self._preview_cache.popitem(last=False)
        return label

    # -- playback -----------------------------------------------------------

    def _stream_cache_get(self, url: str) -> Optional[StreamInfo]:
        entry = self._stream_cache.get(url)
        if entry is None:
            return None
        stream, expires_at = entry
        # Only reuse a link with plenty of life left, so a queued track never
        # starts on a URL that dies part-way through the song.
        if expires_at - time.time() < STREAM_MIN_VALIDITY:
            self._stream_cache.pop(url, None)
            return None
        self._stream_cache.move_to_end(url)
        return stream

    def _stream_cache_put(self, url: str, stream: StreamInfo) -> None:
        self._stream_cache[url] = (stream, _stream_expiry(stream.stream_url))
        self._stream_cache.move_to_end(url)
        while len(self._stream_cache) > STREAM_CACHE_SIZE:
            self._stream_cache.popitem(last=False)

    async def resolve_stream(self, url: str) -> StreamInfo:
        """Fetch a direct audio URL for playback.

        Cached until shortly before the signed URL expires, so several guilds
        queueing the same popular song pay for one extraction between them.
        """
        cached = self._stream_cache_get(url)
        if cached is not None:
            return cached
        return await self._single_flight(
            f"stream:{url}", lambda: self._resolve_stream(url)
        )

    def invalidate_stream(self, url: str) -> None:
        """Forget the cached stream URL for ``url``.

        Used when ffmpeg could not actually read a URL we handed it, so the
        retry resolves a fresh one instead of replaying the dead link.
        """
        self._stream_cache.pop(url, None)

    async def _resolve_stream(self, url: str) -> StreamInfo:
        cached = self._stream_cache_get(url)
        if cached is not None:
            return cached

        try:
            info = await self._run(url, _STREAM_OPTS)
        except YoutubeDLError as exc:
            raise ExtractionError(str(exc)) from exc

        if not info or not info.get("url"):
            raise ExtractionError(f"no audio stream for {url}")

        stream = StreamInfo(
            stream_url=info["url"],
            title=info.get("title") or "Unknown title",
            duration=_coerce_duration(info.get("duration")) or 0,
            thumbnail=info.get("thumbnail"),
            http_headers=info.get("http_headers") or None,
        )
        self._stream_cache_put(url, stream)
        return stream

    def prefetch_stream(self, url: str) -> None:
        """Warm the stream cache for an upcoming track, in the background.

        Called when a track starts playing so the *next* one is already
        resolved by the time it is needed - removing the silent gap between
        songs and spreading extraction load off the moment of transition.
        """

        async def warm() -> None:
            try:
                await self.resolve_stream(url)
            except Exception:
                log.debug("prefetch failed for %s", url, exc_info=True)

        task = asyncio.create_task(warm())
        # warm() swallows everything; this just stops "never retrieved" noise.
        task.add_done_callback(lambda t: t.cancelled() or t.exception())

"""Static configuration and environment discovery for the music player."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

# Repo root: <root>/src/music_player/config.py -> parents[2] == <root>
PROJECT_ROOT = Path(__file__).resolve().parents[2]
FFMPEG_ROOT = PROJECT_ROOT / "ffmpeg"
ENV_FILE = PROJECT_ROOT / "src" / ".env"

# Load .env here, before anything below reads os.environ. Doing it in app.py's
# main() was too late: this module is imported at module scope, so every
# setting had already been read with its default and .env was ignored.
load_dotenv(ENV_FILE)

COMMAND_PREFIX = "?"
HELP_FILE = PROJECT_ROOT / "src" / "help.txt"

#: Volume applied to a guild that has never used ``?volume``.
DEFAULT_VOLUME = 0.10
#: Idle time before the bot leaves a voice channel on its own.
IDLE_DISCONNECT_SECONDS = 69.0
#: Songs listed per ``?queue`` / ``?queueto`` page.
QUEUE_PAGE_SIZE = 10
#: Upper bound on cached yt-dlp metadata lookups.
METADATA_CACHE_SIZE = 128
#: Upper bound on cached autocomplete labels.
PREVIEW_CACHE_SIZE = 256
#: Discord discards an autocomplete response after 3s ("The application did not
#: respond"). Answer well inside that, even if yt-dlp is still working.
AUTOCOMPLETE_TIMEOUT = 1.5
#: A YouTube video id is always 11 characters; anything shorter is still being
#: typed, so there is no point paying for a network round trip.
YOUTUBE_ID_LENGTH = 11
#: Threads dedicated to yt-dlp extraction. This work is network-bound, not
#: CPU-bound, so it is sized above the core count. A private pool keeps a burst
#: of lookups from starving anything else that uses the default executor.
#:
#: This is the ceiling on simultaneous extractions; past it, requests queue.
#: Measured with 1.3s extractions and 100 users all wanting different links:
#:
#:     16 workers -> 9.1s worst case
#:     32 workers -> 5.2s
#:     64 workers -> 2.6s
#:
#: Raising it is cheap locally (the threads only wait on the network) but sends
#: proportionally more concurrent traffic to YouTube, which throttles agressive
#: clients. 16 is deliberately conservative; caching and request coalescing
#: already remove most repeat load. Override with MUSIC_EXTRACTOR_WORKERS.
EXTRACTOR_WORKERS = max(1, int(os.environ.get("MUSIC_EXTRACTOR_WORKERS", "16")))
# Per-user throttles. Discord allows 5 messages per 5 seconds *per channel*,
# shared by everyone in it, so one user spamming commands delays everybody
# else's replies. These keep a single user from consuming that whole budget.
#: ``?add`` is the expensive one - network plus a possible 100+ song playlist.
ADD_RATE, ADD_PER = 3, 10.0
#: Queue displays are cheap to compute but still cost a message each.
QUEUE_RATE, QUEUE_PER = 4, 6.0

#: Upper bound on cached stream URLs.
STREAM_CACHE_SIZE = 256
#: A cached googlevideo URL is only reused while it still has this much life
#: left, so a track never starts on a link that expires mid-song.
STREAM_MIN_VALIDITY = 3600.0
#: Fallback when a stream URL carries no ``expire`` parameter.
STREAM_DEFAULT_TTL = 7200.0

#: ``-reconnect_on_http_error`` is the important one: googlevideo intermittently
#: answers a valid stream URL with 403/5xx, and without it ffmpeg treats that as
#: end-of-input and exits 0. The track then "played" for a fraction of a second
#: and the after-callback advanced, so a perfectly available song was announced
#: as now-playing, produced silence, and vanished.
FFMPEG_BEFORE_OPTIONS = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 "
    "-reconnect_on_http_error 4xx,5xx -reconnect_delay_max 5 -probesize 300M"
)
FFMPEG_OPTIONS = "-vn"

#: Audio shorter than this never counts as a played track, however short the
#: song is. Covers the instant-EOF case where ffmpeg could not open the stream.
PLAYBACK_FAILURE_SECONDS = 5.0
#: Below this share of a track's known duration, playback is treated as a
#: stream failure worth one retry rather than a song that finished.
MIN_PLAYED_FRACTION = 0.5

NOW_PLAYING_ICON = (
    "https://encrypted-tbn0.gstatic.com/images"
    "?q=tbn:ANd9GcST8wdNfmD5TMIpIQ71p2O1MiBx2GFS8M9NzyHFsmyplw&s"
)
FALLBACK_AVATAR = (
    "https://lisboaparapessoas.pt/wp-content/uploads/2022/02/discordlxparapessoas.png"
)


_FFMPEG_NAMES = ("ffmpeg.exe", "ffmpeg")


def _executable_in(directory: Path) -> str | None:
    for name in _FFMPEG_NAMES:
        candidate = directory / name
        if candidate.is_file():
            return str(candidate)
    return None


def resolve_ffmpeg() -> str:
    """Locate the ffmpeg executable.

    Resolution order:

    1. ``FFMPEG_PATH`` from the environment / ``.env`` - either the executable
       itself or the folder containing it;
    2. any build bundled under ``<root>/ffmpeg`` (globbed, so upgrading ffmpeg
       does not need a code change);
    3. ``ffmpeg`` on ``PATH``.

    A configured ``FFMPEG_PATH`` that does not exist raises rather than falling
    through, so a typo is reported instead of silently using a different build.
    """
    configured = os.environ.get("FFMPEG_PATH", "").strip().strip('"').strip("'")
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return str(path)
        if path.is_dir():
            # Accept either .../bin or the extracted root.
            found = _executable_in(path) or _executable_in(path / "bin")
            if found:
                return found
            raise FileNotFoundError(
                f"FFMPEG_PATH is set to the directory {configured!r} but no "
                "ffmpeg executable was found in it (or its bin/ subfolder)."
            )
        raise FileNotFoundError(
            f"FFMPEG_PATH is set to {configured!r}, which does not exist."
        )

    for name in _FFMPEG_NAMES:
        for candidate in sorted(FFMPEG_ROOT.glob(f"*/bin/{name}"), reverse=True):
            if candidate.is_file():
                return str(candidate)

    found = shutil.which("ffmpeg")
    if found:
        return found

    raise FileNotFoundError(
        f"ffmpeg not found. Set FFMPEG_PATH in {ENV_FILE}, place a build under "
        f"{FFMPEG_ROOT}, or install ffmpeg on PATH."
    )

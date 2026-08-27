"""Saved playlists: a server's song lists, kept in SQLite.

A playlist belongs to the **server it was made in**. Everyone there can see it,
add songs, take songs out, and play it; nobody outside can reach it. The one
thing recorded about a person is :attr:`Playlist.created_by` - "whoever started
this list" - because renaming or deleting a list the whole channel built is
kept to them and to moderators, and that has to survive a restart too.

None of this is the live queue in :mod:`music_player.state`, which is also
per-guild but is live, unnamed, and gone when the process stops.

**Why SQLite rather than a JSON file.** The store began as one JSON document
rewritten in full on every change, which is fine while the whole library is a
few hundred rows. At the current cap - 25 playlists of 10,000 songs - a server
can hold 250,000 songs, and rewriting 43MB to add one song is the wrong shape:
the cost tracked the size of *everything* instead of the size of the change.
Here, adding a song is one ``INSERT`` whatever else is stored.

Two things come along for free, and they are half the reason to do it:

* **The schema holds the invariants.** ``UNIQUE (guild_id, name_key)`` is what
  stops one server having two playlists whose names differ only in case; it
  used to be a Python check that was only as good as the code around it.
  ``ON DELETE CASCADE`` is what stops a deleted playlist leaving its songs
  behind.
* **A change is committed or it is not.** There is no "live in memory but not
  on disk" state to report, because a failed transaction rolls back.

``sqlite3`` is blocking, so every public method here is a coroutine that does
its work on a worker thread. Reads return fully detached
:class:`Playlist` / :class:`SavedTrack` values, so callers hold plain data
rather than anything tied to a cursor.

Nothing here talks to Discord. Refusals are raised as :class:`PlaylistError`
subclasses carrying the facts; the cog turns those into the wording a user
reads, so the copy stays in :mod:`music_player.ui` with the rest of it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, TypeVar

from music_player.config import (
    MAX_PLAYLIST_TRACKS,
    MAX_PLAYLISTS_PER_GUILD,
    PLAYLIST_NAME_LIMIT,
)

log = logging.getLogger(__name__)

T = TypeVar("T")

#: Bumped when the tables below change shape. Read from ``PRAGMA user_version``.
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS playlists (
    id          INTEGER PRIMARY KEY,
    guild_id    INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    -- The case- and width-folded name. Lookups match on this while the
    -- listing shows `name` exactly as it was typed.
    name_key    TEXT    NOT NULL,
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL,
    created_by  INTEGER,
    UNIQUE (guild_id, name_key)
);

CREATE TABLE IF NOT EXISTS tracks (
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    url         TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    duration    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (playlist_id, position)
);
"""

#: Control characters become a space rather than vanishing: a newline
#: in a name would break the layout of every embed and dropdown it
#: appears in, but deleting it outright would weld the words on either
#: side of it together.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def fold(name: str) -> str:
    """The lookup key for a playlist name.

    Names are matched case- and width-insensitively but shown exactly as they
    were typed, so ``?playlist play chill`` finds the playlist called "Chill"
    and the listing still says "Chill".
    """
    return unicodedata.normalize("NFKC", str(name)).casefold().strip()


# -- refusals ---------------------------------------------------------------


class PlaylistError(RuntimeError):
    """Base for everything a user can ask for that cannot be done."""


class StorageError(RuntimeError):
    """The database refused the change. Nothing was written."""


class InvalidName(PlaylistError):
    """``reason`` is a token the cog maps to wording: ``empty`` or ``long``."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"invalid playlist name: {reason}")
        self.reason = reason


class NoSuchPlaylist(PlaylistError):
    def __init__(self, name: str) -> None:
        super().__init__(f"no playlist named {name!r}")
        self.name = name


class PlaylistExists(PlaylistError):
    def __init__(self, name: str) -> None:
        super().__init__(f"a playlist named {name!r} already exists")
        self.name = name


class TooManyPlaylists(PlaylistError):
    def __init__(self, limit: int) -> None:
        super().__init__(f"already at the {limit} playlist limit")
        self.limit = limit


class PlaylistFull(PlaylistError):
    def __init__(self, name: str, limit: int) -> None:
        super().__init__(f"{name!r} already holds {limit} songs")
        self.name = name
        self.limit = limit


class NoSuchSong(PlaylistError):
    def __init__(self, name: str, asked: int, total: int) -> None:
        super().__init__(f"{name!r} has no song {asked}")
        self.name = name
        self.asked = asked
        self.total = total


def normalise_name(raw: str) -> str:
    """Clean a user-supplied name, or raise :class:`InvalidName`.

    Runs of whitespace collapse to one space, so "my   mix" and "my mix" are
    the same playlist rather than two that look identical in the listing.
    """
    name = " ".join(_CONTROL.sub(" ", str(raw)).split())
    if not name:
        raise InvalidName("empty")
    if len(name) > PLAYLIST_NAME_LIMIT:
        raise InvalidName("long")
    return name


# -- data -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SavedTrack:
    """One song in a playlist.

    Deliberately not :class:`~music_player.state.Track`: that one records who
    queued it and when, which is a fact about a playback session rather than
    about the song. The cog builds a ``Track`` from this at load time.
    """

    url: str
    title: str
    duration: int


@dataclass(frozen=True, slots=True)
class PlaylistSummary:
    """A playlist's headline figures, without its songs.

    What a listing shows: a name, how many songs, how long. Reading those from
    a ``GROUP BY`` instead of materialising every track is the difference
    between one query and a quarter of a million rows.

    Shares ``name`` / ``songs`` / ``duration`` with :class:`Playlist` on
    purpose, so the embed builders take either.
    """

    name: str
    songs: int
    duration: int
    #: Who started it - the listing credits them, and it is who may delete it.
    created_by: Optional[int] = None

    @property
    def key(self) -> str:
        return fold(self.name)


@dataclass(slots=True)
class Playlist:
    """A named list of songs, belonging to one server.

    A detached snapshot: reading one again after a change gives a new value
    rather than mutating this. Mutating methods return the updated playlist so
    a confirmation never quotes a stale song count.
    """

    name: str
    tracks: List[SavedTrack] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    #: Who created it. Everyone in the server may edit the *songs*; this is
    #: what lets the person who started a list, and moderators, rename or
    #: delete the list itself. ``None`` when it was not recorded.
    created_by: Optional[int] = None

    @property
    def key(self) -> str:
        return fold(self.name)

    @property
    def songs(self) -> int:
        """Named to match :class:`PlaylistSummary`, so both render the same."""
        return len(self.tracks)

    @property
    def duration(self) -> int:
        return sum(track.duration for track in self.tracks)


# -- the library ------------------------------------------------------------


class PlaylistLibrary:
    """Every server's playlists, in one SQLite file.

    Constructed synchronously - opening the file, creating the tables and
    importing any legacy JSON all happen before the bot is running. Every
    query after that is a coroutine, because ``sqlite3`` blocks and the event
    loop also drives audio.
    """

    __slots__ = ("_path", "_db", "_lock")

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._db = self._connect()
        self._migrate()

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        """Release the connection. Call on shutdown."""
        try:
            self._db.close()
        except sqlite3.Error:
            log.debug("could not close the playlist database", exc_info=True)

    # -- setup --------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open the database, moving an unreadable file aside if need be.

        ``check_same_thread=False`` because every query runs on a worker
        thread; the asyncio lock is what guarantees only one of them touches
        the connection at a time.
        """
        try:
            return self._open()
        except sqlite3.DatabaseError:
            log.exception("playlist database at %s could not be opened", self._path)

        spoiled = self._path.with_name(
            f"{self._path.stem}.corrupt-{int(time.time())}{self._path.suffix}"
        )
        try:
            os.replace(self._path, spoiled)
        except OSError:
            # Refusing to start beats silently running with no playlists and
            # then overwriting whatever the file actually holds.
            raise
        log.error(
            "playlist database %s was unreadable; moved to %s and starting empty",
            self._path,
            spoiled,
        )
        return self._open()

    def _open(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self._path), check_same_thread=False)
        try:
            # WAL survives a crash mid-write without the temp-file dance the
            # JSON store needed, and NORMAL is durable under it for everything
            # short of losing power mid-commit.
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA synchronous = NORMAL")
            # Off by default in SQLite, and the whole point of the tracks
            # table's ON DELETE CASCADE.
            db.execute("PRAGMA foreign_keys = ON")
            # Touch the schema so a file that is not a database fails here,
            # while the caller can still do something about it.
            db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        except Exception:
            # The handle has to go before the caller can move the file aside -
            # on Windows an open handle makes os.replace fail outright.
            db.close()
            raise
        return db

    def _migrate(self) -> None:
        """Create the tables, then import a legacy JSON store if one is there."""
        with self._db:
            self._db.executescript(_SCHEMA)
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            with self._db:
                self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._import_legacy()

    # -- one-time import from the old JSON store ----------------------------

    def _import_legacy(self) -> None:
        """Bring a ``playlists.json`` next door into the database, once.

        The JSON file is renamed rather than deleted: it is the only copy of
        that data, and an import that turns out to be wrong should be
        recoverable by hand.
        """
        legacy = self._path.with_suffix(".json")
        if not legacy.is_file():
            return
        if self._db.execute("SELECT 1 FROM playlists LIMIT 1").fetchone():
            log.warning(
                "%s exists but the database already has playlists; leaving it "
                "alone. Move it away to silence this.",
                legacy,
            )
            return

        try:
            guilds = _read_legacy_json(legacy.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.exception("could not import the legacy playlist file %s", legacy)
            return

        imported = 0
        try:
            with self._db:
                for guild_id, playlists in guilds.items():
                    for playlist in playlists:
                        self._insert(guild_id, playlist)
                        imported += 1
        except sqlite3.Error:
            log.exception("importing %s failed; the database is unchanged", legacy)
            return

        done = legacy.with_name(f"{legacy.stem}.imported-{int(time.time())}.json")
        try:
            os.replace(legacy, done)
        except OSError:
            log.exception("imported %s but could not rename it out of the way", legacy)
        log.info(
            "imported %d playlist(s) from %s into %s (the original is at %s)",
            imported,
            legacy,
            self._path,
            done,
        )

    def _insert(self, guild_id: int, playlist: Playlist) -> None:
        """Write one whole playlist. Caller owns the transaction."""
        cursor = self._db.execute(
            "INSERT INTO playlists "
            "(guild_id, name, name_key, created_at, updated_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                guild_id,
                playlist.name,
                fold(playlist.name),
                playlist.created_at,
                playlist.updated_at,
                playlist.created_by,
            ),
        )
        self._db.executemany(
            "INSERT INTO tracks (playlist_id, position, url, title, duration) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (cursor.lastrowid, index, t.url, t.title, t.duration)
                for index, t in enumerate(playlist.tracks)
            ],
        )

    # -- plumbing -----------------------------------------------------------

    async def _run(self, work: Callable[[], T]) -> T:
        """Run one unit of SQL on a worker thread.

        The lock serialises access, which is what makes a single connection
        shared across threads safe, and what makes each ``work`` an atomic
        read-modify-write against the rest of the bot.
        """
        async with self._lock:
            try:
                return await asyncio.to_thread(work)
            except PlaylistError:
                raise
            except sqlite3.Error as exc:
                log.exception("playlist database error")
                raise StorageError(str(exc)) from exc

    def _playlist_row(self, guild_id: int, name: str):
        return self._db.execute(
            "SELECT id, name, created_at, updated_at, created_by "
            "FROM playlists WHERE guild_id = ? AND name_key = ?",
            (guild_id, fold(name)),
        ).fetchone()

    def _require_row(self, guild_id: int, name: str):
        row = self._playlist_row(guild_id, name)
        if row is None:
            raise NoSuchPlaylist(str(name))
        return row

    def _tracks_of(self, playlist_id: int) -> List[SavedTrack]:
        return [
            SavedTrack(url, title, duration)
            for url, title, duration in self._db.execute(
                "SELECT url, title, duration FROM tracks "
                "WHERE playlist_id = ? ORDER BY position",
                (playlist_id,),
            )
        ]

    def _materialise(self, row) -> Playlist:
        playlist_id, name, created_at, updated_at, created_by = row
        return Playlist(
            name=name,
            tracks=self._tracks_of(playlist_id),
            created_at=created_at,
            updated_at=updated_at,
            created_by=created_by,
        )

    def _touch(self, playlist_id: int) -> float:
        now = time.time()
        self._db.execute(
            "UPDATE playlists SET updated_at = ? WHERE id = ?", (now, playlist_id)
        )
        return now

    # -- reads --------------------------------------------------------------

    async def summaries(self, guild_id: int) -> List[PlaylistSummary]:
        """Every playlist in a server, as name / count / duration.

        One aggregate query. The listing and the picker show nothing but these
        three things, so loading the songs to count them was work thrown away -
        a quarter of a second on a server near the cap.
        """

        def work() -> List[PlaylistSummary]:
            return [
                PlaylistSummary(
                    name=name, songs=songs, duration=duration, created_by=created_by
                )
                for name, songs, duration, created_by in self._db.execute(
                    "SELECT p.name, COUNT(t.playlist_id), "
                    "       COALESCE(SUM(t.duration), 0), p.created_by "
                    "FROM playlists p "
                    "LEFT JOIN tracks t ON t.playlist_id = p.id "
                    "WHERE p.guild_id = ? "
                    "GROUP BY p.id ORDER BY p.created_at, p.id",
                    (guild_id,),
                )
            ]

        return await self._run(work)

    async def names(self, guild_id: int) -> List[str]:
        """Just the names, oldest first.

        Autocomplete fires on every keystroke and shows nothing but names, so
        it must not pay for the songs: :meth:`playlists` materialises every
        track in the server, which is a quarter of a second once a server is
        full.
        """

        def work() -> List[str]:
            return [
                row[0]
                for row in self._db.execute(
                    "SELECT name FROM playlists WHERE guild_id = ? "
                    "ORDER BY created_at, id",
                    (guild_id,),
                )
            ]

        return await self._run(work)

    async def find(self, guild_id: int, name: str) -> Optional[Playlist]:
        def work() -> Optional[Playlist]:
            row = self._playlist_row(guild_id, name)
            return None if row is None else self._materialise(row)

        return await self._run(work)

    async def require(self, guild_id: int, name: str) -> Playlist:
        def work() -> Playlist:
            return self._materialise(self._require_row(guild_id, name))

        return await self._run(work)

    # -- writes -------------------------------------------------------------

    async def create(
        self, guild_id: int, name: str, *, created_by: Optional[int] = None
    ) -> Playlist:
        clean = normalise_name(name)

        def work() -> Playlist:
            with self._db:
                if self._playlist_row(guild_id, clean) is not None:
                    # Checked before the cap: "pick another name" is the useful
                    # answer even for a server that is also at its limit.
                    raise PlaylistExists(clean)
                held = self._db.execute(
                    "SELECT COUNT(*) FROM playlists WHERE guild_id = ?", (guild_id,)
                ).fetchone()[0]
                if held >= MAX_PLAYLISTS_PER_GUILD:
                    raise TooManyPlaylists(MAX_PLAYLISTS_PER_GUILD)
                playlist = Playlist(name=clean, created_by=created_by)
                self._insert(guild_id, playlist)
                return playlist

        return await self._run(work)

    async def delete(self, guild_id: int, name: str) -> Playlist:
        """Remove a playlist and, by cascade, everything in it."""

        def work() -> Playlist:
            with self._db:
                row = self._require_row(guild_id, name)
                playlist = self._materialise(row)
                self._db.execute("DELETE FROM playlists WHERE id = ?", (row[0],))
                return playlist

        return await self._run(work)

    async def rename(self, guild_id: int, old: str, new: str) -> Playlist:
        clean = normalise_name(new)

        def work() -> Playlist:
            with self._db:
                row = self._require_row(guild_id, old)
                clash = self._playlist_row(guild_id, clean)
                # A clash with *itself* is the "chill" -> "Chill" case: a
                # capitalisation fix, not a collision with another playlist.
                if clash is not None and clash[0] != row[0]:
                    raise PlaylistExists(clean)
                self._db.execute(
                    "UPDATE playlists SET name = ?, name_key = ? WHERE id = ?",
                    (clean, fold(clean), row[0]),
                )
                self._touch(row[0])
                return self._materialise(self._require_row(guild_id, clean))

        return await self._run(work)

    async def extend(
        self, guild_id: int, name: str, tracks: Sequence[SavedTrack]
    ) -> tuple[Playlist, int]:
        """Append what fits. Returns the updated playlist and how many landed.

        A playlist with room takes as many as it can hold rather than refusing
        the lot, so adding a 5,000-song YouTube playlist to one with 400 slots
        left is a partial success the caller can report honestly.
        """

        def work() -> tuple[Playlist, int]:
            with self._db:
                row = self._require_row(guild_id, name)
                playlist_id = row[0]
                held = self._db.execute(
                    "SELECT COUNT(*) FROM tracks WHERE playlist_id = ?",
                    (playlist_id,),
                ).fetchone()[0]
                room = MAX_PLAYLIST_TRACKS - held
                if room <= 0:
                    raise PlaylistFull(row[1], MAX_PLAYLIST_TRACKS)
                added = list(tracks)[:room]
                if added:
                    self._db.executemany(
                        "INSERT INTO tracks "
                        "(playlist_id, position, url, title, duration) "
                        "VALUES (?, ?, ?, ?, ?)",
                        [
                            (playlist_id, held + offset, t.url, t.title, t.duration)
                            for offset, t in enumerate(added)
                        ],
                    )
                    self._touch(playlist_id)
                return self._materialise(self._require_row(guild_id, name)), len(added)

        return await self._run(work)

    async def remove_at(
        self, guild_id: int, name: str, number: int
    ) -> tuple[Playlist, SavedTrack]:
        """Drop the 1-based ``number``th song - the numbers ``show`` prints."""

        def work() -> tuple[Playlist, SavedTrack]:
            with self._db:
                row = self._require_row(guild_id, name)
                playlist_id = row[0]
                held = self._db.execute(
                    "SELECT COUNT(*) FROM tracks WHERE playlist_id = ?",
                    (playlist_id,),
                ).fetchone()[0]
                if not 1 <= number <= held:
                    raise NoSuchSong(row[1], number, held)

                position = number - 1
                found = self._db.execute(
                    "SELECT url, title, duration FROM tracks "
                    "WHERE playlist_id = ? AND position = ?",
                    (playlist_id, position),
                ).fetchone()
                track = SavedTrack(*found)

                self._db.execute(
                    "DELETE FROM tracks WHERE playlist_id = ? AND position = ?",
                    (playlist_id, position),
                )
                # Close the gap. Done via negative positions because
                # (playlist_id, position) is unique and SQLite does not promise
                # an update order - shifting straight down could collide with a
                # row that has not moved yet.
                self._db.execute(
                    "UPDATE tracks SET position = -(position - 1) "
                    "WHERE playlist_id = ? AND position > ?",
                    (playlist_id, position),
                )
                self._db.execute(
                    "UPDATE tracks SET position = -position "
                    "WHERE playlist_id = ? AND position < 0",
                    (playlist_id,),
                )
                self._touch(playlist_id)
                return self._materialise(self._require_row(guild_id, name)), track

        return await self._run(work)


# -- reading the store this one replaced ------------------------------------


def _read_legacy_json(raw: str) -> Dict[int, List[Playlist]]:
    """Parse any of the three JSON schemas the file store ever used.

    1. ``users``  - playlists belonged to a person; nothing to import.
    2. ``owners`` - ``user:<id>`` and ``guild:<id>``; the guild ones import.
    3. ``guilds`` - what the file store ended on.
    """
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("the top level is not an object")

    version = data.get("version")
    if version == 3:
        source = data.get("guilds")
        if not isinstance(source, dict):
            raise ValueError("'guilds' is not an object")
    elif version == 2:
        owners = data.get("owners")
        if not isinstance(owners, dict):
            raise ValueError("'owners' is not an object")
        source = {}
        skipped = 0
        for key, entries in owners.items():
            scope, _, rest = str(key).partition(":")
            if scope == "guild":
                source[rest] = entries
            elif isinstance(entries, list):
                skipped += len(entries)
        if skipped:
            log.warning(
                "%d playlist(s) in the legacy file belonged to a person rather "
                "than a server and were not imported",
                skipped,
            )
    elif version == 1:
        users = data.get("users")
        skipped = sum(
            len(v) for v in (users or {}).values() if isinstance(v, list)
        )
        log.warning(
            "the legacy file is version 1, where all %d playlist(s) belonged to "
            "a person; none can be imported",
            skipped,
        )
        return {}
    else:
        raise ValueError(f"unsupported schema version {version!r}")

    guilds: Dict[int, List[Playlist]] = {}
    for raw_id, entries in source.items():
        try:
            guild_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if not isinstance(entries, list):
            continue

        seen: set[str] = set()
        owned: List[Playlist] = []
        for entry in entries:
            playlist = _legacy_playlist(entry)
            if playlist is None or playlist.key in seen:
                continue
            seen.add(playlist.key)
            owned.append(playlist)
            if len(owned) >= MAX_PLAYLISTS_PER_GUILD:
                break
        if owned:
            guilds[guild_id] = owned
    return guilds


def _legacy_playlist(raw: object) -> Optional[Playlist]:
    """One stored playlist, or ``None`` if the row is unusable.

    Everything is re-validated: the file was editable by hand, and one bad row
    should cost that row rather than the import.
    """
    if not isinstance(raw, dict):
        return None
    try:
        name = normalise_name(raw.get("name") or "")
    except InvalidName:
        return None

    rows = raw.get("tracks")
    tracks = [
        track
        for track in map(_legacy_track, rows if isinstance(rows, list) else [])
        if track is not None
    ][:MAX_PLAYLIST_TRACKS]

    now = time.time()
    return Playlist(
        name=name,
        tracks=tracks,
        created_at=_coerce_float(raw.get("created_at"), now),
        updated_at=_coerce_float(raw.get("updated_at"), now),
        created_by=_coerce_int(raw.get("created_by")),
    )


def _legacy_track(raw: object) -> Optional[SavedTrack]:
    if not isinstance(raw, dict):
        return None
    url = raw.get("url")
    title = raw.get("title")
    if not isinstance(url, str) or not url.strip():
        return None
    return SavedTrack(
        url=url.strip(),
        title=title.strip() if isinstance(title, str) and title.strip() else url,
        duration=max(0, _coerce_int(raw.get("duration")) or 0),
    )


def _coerce_float(value: object, fallback: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback


def _coerce_int(value: object) -> Optional[int]:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None

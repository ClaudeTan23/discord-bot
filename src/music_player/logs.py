"""Logging setup: where records go, what they carry, and what they must not.

The bot already logged in the right places - what was missing was everywhere
for those records to *go*. Console output dies with the process, so the one
question troubleshooting always starts with ("what happened last night?") had
no answer at all.

Four things this module adds:

* **A file per day, in a dated folder.** ``logs/2026/08/09/bot.log``. Errors
  are duplicated into ``errors.log`` beside it, so "did anything break today"
  is answered by the size of one file.
* **Context on every line.** Which guild, which user, which command - carried
  in a :class:`~contextvars.ContextVar` so a record logged deep inside
  ``ytdl`` knows who asked for it, without every function taking a guild id.
* **Encoding that cannot fail.** See :func:`_harden_stream`.
* **Redaction.** The token and signed stream URLs never reach disk, so a log
  file is always safe to paste into a bug report.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import shutil
import sys
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from logging.handlers import BaseRotatingHandler
from pathlib import Path
from typing import Any, Iterator, Optional

from music_player.config import (
    LOG_DIR,
    LOG_DISCORD_LEVEL,
    LOG_LEVEL,
    LOG_MAX_BYTES,
    LOG_RETENTION_DAYS,
)

log = logging.getLogger(__name__)

#: Guards against a second ``configure()`` stacking duplicate handlers, which
#: would double every line in the file.
_configured = False

_LINE = "%(asctime)s %(levelname)-8s %(name)-28s%(ctx)s %(message)s"
_TIME = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------------------
# invocation context
# ---------------------------------------------------------------------------

_CONTEXT: ContextVar[dict] = ContextVar("music_log_context", default={})


def bind(**fields: Any) -> Token:
    """Attach fields to every record logged from here on in this task.

    Returns a token for :func:`unbind`. Each command invocation runs in its
    own asyncio task, so one command's context can never leak into another's.
    """
    merged = {**_CONTEXT.get(), **{k: v for k, v in fields.items() if v is not None}}
    return _CONTEXT.set(merged)


def unbind(token: Token) -> None:
    _CONTEXT.reset(token)


@contextmanager
def context(**fields: Any) -> Iterator[None]:
    token = bind(**fields)
    try:
        yield
    finally:
        unbind(token)


@contextmanager
def traced(description: str, **fields: Any) -> Iterator[None]:
    """Tag and time a block of work, without being able to break it.

    Every step around the ``yield`` is guarded. Tracing is an observability
    feature wrapped around the command it observes, so a malformed field, a
    full disk or a closed handler must cost a log line - never the command.
    The ``yield`` itself is deliberately *not* guarded: the caller's own
    exceptions still propagate exactly as they did before.

    The unmatched "run" line left behind by work that never returns is the
    cheapest hang detector there is, so the opening line is logged eagerly
    rather than buffered until the end.
    """
    token: Optional[Token] = None
    started = time.perf_counter()
    try:
        token = bind(**fields)
        log.info("run %s", description)
    except Exception:  # pragma: no cover - defensive
        log.debug("could not open a command trace", exc_info=True)

    try:
        yield
    finally:
        try:
            log.info("done in %.0fms", (time.perf_counter() - started) * 1000)
        except Exception:  # pragma: no cover - defensive
            pass
        if token is not None:
            try:
                unbind(token)
            except Exception:  # pragma: no cover - defensive
                pass


class _ContextFilter(logging.Filter):
    """Renders the bound context into a ``ctx`` field for the format string.

    Absent context renders as empty rather than a row of placeholders: the
    background tasks that log without a command attached should not each cost
    a line of ``guild=None user=None``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        fields = _CONTEXT.get()
        record.ctx = (
            " [" + " ".join(f"{k}={v}" for k, v in fields.items()) + "]"
            if fields
            else ""
        )
        return True


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------

#: googlevideo signs its stream URLs; those parameters are credentials for the
#: duration of the link. yt-dlp and ffmpeg both echo full URLs on failure,
#: which is exactly when someone copies a log into a bug report.
_SECRET_PARAMS = re.compile(
    r"\b((?:signature|sig|pot|potc|key|token)=)[^&\s\"']+", re.IGNORECASE
)
_COOKIE_HEADER = re.compile(r"(Cookie:\s*)[^\r\n]+", re.IGNORECASE)


class _SafeFormatter(logging.Formatter):
    """Formatter that scrubs credentials out of the finished line.

    Done at format time rather than on the record, so nothing mutates state
    shared between handlers and a value is never scrubbed twice.
    """

    def __init__(self, *args: Any, secrets: tuple[str, ...] = (), **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Longest first: a token and its prefix must not race to match.
        self._secrets = tuple(sorted((s for s in secrets if s), key=len, reverse=True))

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, "***redacted***")
        text = _SECRET_PARAMS.sub(r"\1***", text)
        return _COOKIE_HEADER.sub(r"\1***", text)


# ---------------------------------------------------------------------------
# the dated file handler
# ---------------------------------------------------------------------------


def _today() -> dt.date:
    """One place the calendar is read, so tests can drive midnight."""
    return dt.date.today()


class DatedFolderHandler(BaseRotatingHandler):
    """Writes to ``<root>/YYYY/MM/DD/<stem>.log``, rolling over at midnight.

    ``TimedRotatingFileHandler`` renames yesterday's file in place and never
    leaves the directory it was given, so a long-running bot ends up with a
    heap of suffixed files in one folder. This keeps each day's records in
    their own folder instead.

    Rolls on two conditions:

    * the date changed - a bot started on Tuesday keeps logging into Tuesday's
      folder without this, which is the failure mode that matters;
    * the file hit ``max_bytes`` - the day continues in ``<stem>.2.log``, so a
      runaway error loop cannot fill the disk with one enormous file.
    """

    def __init__(
        self,
        root: Path,
        stem: str,
        *,
        max_bytes: int = 0,
        encoding: str = "utf-8",
    ) -> None:
        self.root = Path(root)
        self.stem = stem
        self.max_bytes = max_bytes
        self._day: Optional[dt.date] = None
        self._part = 1
        # errors="backslashreplace" is the point of this whole class being
        # explicit about encoding - see _harden_stream.
        super().__init__(
            str(self._target()), mode="a", encoding=encoding,
            delay=False, errors="backslashreplace",
        )

    def _target(self) -> Path:
        today = _today()
        if today != self._day:
            self._day = today
            self._part = 1
        folder = self.root / f"{today:%Y}" / f"{today:%m}" / f"{today:%d}"
        folder.mkdir(parents=True, exist_ok=True)
        name = self.stem if self._part == 1 else f"{self.stem}.{self._part}"
        return folder / f"{name}.log"

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        if _today() != self._day:
            return True
        if self.max_bytes > 0 and self.stream is not None:
            if self.stream.tell() >= self.max_bytes:
                self._part += 1  # same day, next part
                return True
        return False

    def doRollover(self) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None  # type: ignore[assignment]
        self.baseFilename = str(self._target())
        if not self.delay:
            self.stream = self._open()


def prune(root: Path, keep_days: int) -> int:
    """Delete day folders older than ``keep_days``. Returns how many went.

    Walks the ``YYYY/MM/DD`` tree rather than trusting mtimes, so a folder
    touched by an editor is still judged on the date it is named for.
    """
    if keep_days <= 0 or not root.is_dir():
        return 0

    cutoff = _today() - dt.timedelta(days=keep_days)
    removed = 0
    for day_dir in sorted(root.glob("*/*/*")):
        if not day_dir.is_dir():
            continue
        try:
            year, month, day = day_dir.parts[-3:]
            stamp = dt.date(int(year), int(month), int(day))
        except (ValueError, IndexError):
            continue  # not one of ours; leave it alone
        if stamp < cutoff:
            shutil.rmtree(day_dir, ignore_errors=True)
            removed += 1

    # Tidy the year/month shells left behind once their days are gone.
    for shell in sorted(root.glob("*/*"), reverse=True) + sorted(root.glob("*")):
        if shell.is_dir() and not any(shell.iterdir()):
            shell.rmdir()
    return removed


# ---------------------------------------------------------------------------
# encoding
# ---------------------------------------------------------------------------


def _harden_stream(stream: Any) -> None:
    """Make a console stream incapable of failing on a track title.

    Windows hands a redirected stdout the locale codepage, so the first
    Japanese or emoji-bearing title to reach a log line raises
    ``UnicodeEncodeError`` *inside logging* - the record is lost and a stack
    trace is printed in its place. A log that breaks on the interesting cases
    is worse than no log, so the stream is switched to UTF-8 and told to
    escape anything it still cannot represent.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8", errors="backslashreplace")
    except (ValueError, OSError):
        pass  # already detached, or not a real stream - not worth failing over


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------


def configure(*, level: Optional[str] = None, log_dir: Optional[Path] = None) -> Path:
    """Install console and file logging. Safe to call more than once.

    Returns the directory the logs are being written under.
    """
    global _configured

    root_dir = Path(log_dir) if log_dir else LOG_DIR
    if _configured:
        return root_dir

    resolved = getattr(logging, (level or LOG_LEVEL), None)
    if not isinstance(resolved, int):
        resolved = logging.INFO

    _harden_stream(sys.stdout)
    _harden_stream(sys.stderr)

    # The token is the one value that must never reach a file, since logs get
    # pasted into bug reports.
    formatter = _SafeFormatter(
        _LINE, _TIME, secrets=(os.environ.get("Bot-Token", "").strip(),)
    )
    context_filter = _ContextFilter()

    root = logging.getLogger()
    root.setLevel(min(resolved, logging.INFO))
    for existing in list(root.handlers):
        root.removeHandler(existing)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(resolved)
    handlers: list[logging.Handler] = [console]

    try:
        everything = DatedFolderHandler(root_dir, "bot", max_bytes=LOG_MAX_BYTES)
        everything.setLevel(resolved)
        # A second file carrying only the bad news, so "did anything break
        # today" is answered without reading the full log.
        problems = DatedFolderHandler(root_dir, "errors", max_bytes=LOG_MAX_BYTES)
        problems.setLevel(logging.WARNING)
        handlers.extend((everything, problems))
    except OSError:
        # A read-only or missing volume must not stop the bot from running;
        # console logging still works.
        console.setLevel(min(resolved, logging.INFO))
        logging.getLogger(__name__).warning(
            "could not open the log directory %s - console only", root_dir,
            exc_info=True,
        )

    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(context_filter)
        root.addHandler(handler)

    # discord.py narrates every gateway heartbeat at INFO and buries our own
    # records; its warnings and errors still come through.
    for noisy in ("discord", "websockets", "yt_dlp"):
        logging.getLogger(noisy).setLevel(
            getattr(logging, LOG_DISCORD_LEVEL, logging.WARNING)
        )
    # Two discord.py loggers are exempt from that, because they carry this
    # bot's actual failure modes:
    #
    #   discord.voice_client - disconnects and reconnects.
    #   discord.player       - the ffmpeg command line (at DEBUG) and the
    #                          process's exit code (at INFO). The exit code
    #                          matters more here than anywhere: _ended_early
    #                          exists precisely because ffmpeg returns 0 after
    #                          a 403, and this is the only place that shows it.
    for wanted in ("discord.voice_client", "discord.player"):
        logging.getLogger(wanted).setLevel(min(resolved, logging.INFO))

    _configured = True

    removed = prune(root_dir, LOG_RETENTION_DAYS)
    if removed:
        log.info("pruned %d log folder(s) older than %d days",
                 removed, LOG_RETENTION_DAYS)
    return root_dir


def banner(**extra: Any) -> None:
    """Log what this process actually is. Answers half of every bug report.

    "Works on my machine" is usually a different yt-dlp, a different ffmpeg,
    or a setting nobody remembered overriding - so the log says up front.
    """
    import platform

    try:
        import discord

        discord_version = discord.__version__
    except Exception:  # pragma: no cover - discord is a hard dependency
        discord_version = "unknown"
    try:
        from yt_dlp.version import __version__ as ytdlp_version
    except Exception:  # pragma: no cover
        ytdlp_version = "unknown"

    log.info("=" * 62)
    log.info("music bot starting")
    log.info("  python      %s (%s)", platform.python_version(), sys.platform)
    log.info("  discord.py  %s", discord_version)
    log.info("  yt-dlp      %s", ytdlp_version)
    log.info("  log level   %s -> %s", LOG_LEVEL, LOG_DIR)
    for key, value in extra.items():
        log.info("  %-11s %s", key, value)
    log.info("=" * 62)


def install_exception_hooks(loop: Any = None) -> None:
    """Route the exceptions nothing else catches into the log.

    Without these, a failure on the audio pump thread or in a fire-and-forget
    task prints to a stderr nobody is reading and leaves no trace on disk -
    which is precisely the failure you most need a record of.
    """

    def _thread_hook(args: Any) -> None:
        if args.exc_type is SystemExit:
            return
        log.critical(
            "unhandled exception on thread %s",
            getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _thread_hook

    def _sys_hook(exc_type: Any, exc_value: Any, exc_tb: Any) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        log.critical("unhandled exception", exc_info=(exc_type, exc_value, exc_tb))

    sys.excepthook = _sys_hook

    if loop is not None:
        def _async_hook(_loop: Any, ctx: dict) -> None:
            message = ctx.get("message", "unhandled error in the event loop")
            log.error("%s", message, exc_info=ctx.get("exception"))

        loop.set_exception_handler(_async_hook)

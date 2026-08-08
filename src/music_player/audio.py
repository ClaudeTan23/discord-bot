"""A jitter buffer between ffmpeg and discord.py's audio player.

discord.py drives playback from a plain loop (``discord/player.py``)::

    play_audio(data)
    self.loops += 1
    next_time = self._start + self.DELAY * self.loops
    delay = max(0, self.DELAY + (next_time - time.perf_counter()))
    time.sleep(delay)

Every packet is meant to leave 20ms after the last one. The catch is the
``max(0, ...)``: if ``source.read()`` blocked for, say, 400ms because ffmpeg was
reconnecting to googlevideo, the deadline for the next twenty packets is already
in the past when it returns. The sleep is skipped for each of them and they go
out as fast as the pipe can supply, until the loop has caught up with the wall
clock. The listener hears exactly that - a burst of sped-up, stuttering audio -
and then normal playback resumes.

Nothing in that loop can be fixed from the outside, but it never misbehaves if
``read()`` simply never blocks. :class:`BufferedAudioSource` guarantees that: a
background thread keeps several seconds of decoded audio queued ahead of the
player, and ``read()`` is a dequeue from memory. A stall shorter than the buffer
is invisible; a longer one is padded with silence, which delays the rest of the
track rather than compressing it.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading

from discord import AudioSource
from discord.opus import Encoder as OpusEncoder

from music_player.config import (
    AUDIO_BUFFER_SECONDS,
    AUDIO_PREFILL_SECONDS,
    AUDIO_PREFILL_TIMEOUT,
    AUDIO_UNDERRUN_LIMIT_SECONDS,
)

log = logging.getLogger(__name__)

#: One 20ms frame of 48kHz stereo 16-bit PCM - the unit discord.py reads in.
FRAME_SIZE = OpusEncoder.FRAME_SIZE
FRAMES_PER_SECOND = 1000 // OpusEncoder.FRAME_LENGTH
_SILENCE = b"\x00" * FRAME_SIZE


def _frames(seconds: float, *, minimum: int = 1) -> int:
    return max(minimum, int(seconds * FRAMES_PER_SECOND))


class BufferedAudioSource(AudioSource):
    """Reads ``source`` on its own thread so the player never waits on I/O.

    The wrapper is transparent: it can sit under a
    :class:`discord.PCMVolumeTransformer` so ``?volume`` still applies to the
    live source, and it forwards ``cleanup`` down the chain.
    """

    __slots__ = (
        "_source",
        "_label",
        "_frames",
        "_prefill_target",
        "_ready",
        "_exhausted",
        "_closing",
        "_thread",
        "_underruns",
        "_starved",
        "_starved_limit",
    )

    def __init__(
        self,
        source: AudioSource,
        *,
        label: str = "",
        buffer_seconds: float = AUDIO_BUFFER_SECONDS,
        prefill_seconds: float = AUDIO_PREFILL_SECONDS,
    ) -> None:
        capacity = _frames(buffer_seconds, minimum=FRAMES_PER_SECOND)

        self._source = source
        self._label = label or "stream"
        self._frames: "queue.Queue[bytes]" = queue.Queue(maxsize=capacity)
        self._prefill_target = min(capacity, _frames(prefill_seconds))
        #: Set once the buffer holds enough audio to start - or once the source
        #: ended first, so a very short track does not wait out the timeout.
        self._ready = threading.Event()
        #: Set when the producer will add nothing further.
        self._exhausted = threading.Event()
        self._closing = threading.Event()
        self._underruns = 0
        self._starved = 0
        self._starved_limit = _frames(AUDIO_UNDERRUN_LIMIT_SECONDS)

        self._thread = threading.Thread(
            target=self._pump, name=f"audio-buffer:{id(self):#x}", daemon=True
        )
        self._thread.start()

    # -- producer -----------------------------------------------------------

    def _pump(self) -> None:
        """Drain ``source`` into the queue for as long as it yields audio."""
        try:
            while not self._closing.is_set():
                data = self._source.read()
                if not data:
                    break
                # A bounded put with retries, rather than a blocking one, so a
                # paused player (nothing consuming) still lets cleanup() through.
                while not self._closing.is_set():
                    try:
                        self._frames.put(data, timeout=0.25)
                        break
                    except queue.Full:
                        continue
                if not self._ready.is_set() and (
                    self._frames.qsize() >= self._prefill_target
                ):
                    self._ready.set()
        except Exception:
            log.exception("audio buffer failed for %s", self._label)
        finally:
            self._exhausted.set()
            self._ready.set()

    async def wait_until_ready(
        self, timeout: float = AUDIO_PREFILL_TIMEOUT
    ) -> bool:
        """Await the initial prefill without blocking the event loop.

        Playback starts on time even if this times out - a partially filled
        buffer is still better than none, and ffmpeg may simply be slow to
        connect.
        """
        return await asyncio.get_running_loop().run_in_executor(
            None, self._ready.wait, timeout
        )

    # -- consumer -----------------------------------------------------------

    def is_opus(self) -> bool:
        return self._source.is_opus()

    def read(self) -> bytes:
        try:
            data = self._frames.get_nowait()
        except queue.Empty:
            return self._read_empty()
        self._starved = 0
        return data

    def _read_empty(self) -> bytes:
        """Nothing buffered: end of stream, or ffmpeg falling behind."""
        if self._exhausted.is_set():
            # The producer may have queued a last frame between the get above
            # and this check, so look once more before calling it the end.
            try:
                return self._frames.get_nowait()
            except queue.Empty:
                return b""

        self._underruns += 1
        self._starved += 1
        if self._starved == 1:
            log.info("audio buffer underrun for %s; padding with silence", self._label)
        if self._starved >= self._starved_limit:
            log.warning(
                "no audio for %.0fs from %s; ending the track",
                AUDIO_UNDERRUN_LIMIT_SECONDS,
                self._label,
            )
            return b""
        # Real-time silence, not a blocked read: the player keeps its 20ms
        # cadence, so the rest of the track is delayed rather than sped up.
        return _SILENCE

    @property
    def underruns(self) -> int:
        """Frames of silence inserted so far. Non-zero means a stalled stream."""
        return self._underruns

    def cleanup(self) -> None:
        self._closing.set()
        self._ready.set()
        # Free the pump if it is parked on a full queue, then kill ffmpeg so a
        # pump blocked inside source.read() sees EOF and exits.
        try:
            while True:
                self._frames.get_nowait()
        except queue.Empty:
            pass
        try:
            self._source.cleanup()
        finally:
            # discord.py's AudioSource.__del__ calls cleanup(), so a garbage
            # collection inside the pump thread can land us here on the very
            # thread we are waiting for.
            if threading.current_thread() is not self._thread:
                self._thread.join(timeout=2.0)
        if self._underruns:
            log.info(
                "%s finished after %d underrun frame(s) (%.1fs of silence)",
                self._label,
                self._underruns,
                self._underruns / FRAMES_PER_SECOND,
            )

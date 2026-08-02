"""Playback commands: add, play, queue control, volume."""

from __future__ import annotations

import asyncio
import logging
import shlex
import time
from typing import List, Optional

import discord
from discord import Embed, Interaction, app_commands
from discord.ext import commands

from music_player import ui
from music_player.config import (
    ADD_PER,
    ADD_RATE,
    FFMPEG_BEFORE_OPTIONS,
    FFMPEG_OPTIONS,
    MIN_PLAYED_FRACTION,
    PLAYBACK_FAILURE_SECONDS,
    QUEUE_PAGE_SIZE,
    QUEUE_PER,
    QUEUE_RATE,
    resolve_ffmpeg,
)
from music_player.state import GuildState, MusicState, Track
from music_player.ytdl import (
    ExtractionError,
    StreamInfo,
    YouTubeService,
    is_complete_query,
    normalize_url,
)

log = logging.getLogger(__name__)

#: Discord rejects autocomplete choices whose name or value exceeds 100 chars.
_CHOICE_LIMIT = 100

#: Forwarded to ffmpeg so its request for the stream looks like the one the URL
#: was issued to. Anything else yt-dlp reports is irrelevant to fetching audio.
_FORWARDED_HEADERS = ("User-Agent", "Cookie", "Referer")


class _FFmpegLog:
    """File-like sink that routes ffmpeg's stderr into ``logging``.

    discord.py leaves ffmpeg's stderr attached to the bot's own, so the reason a
    stream failed - typically ``Server returned 403 Forbidden`` - never reached
    the log file. A song that ffmpeg could not read was therefore
    indistinguishable from one that simply ended.

    Deliberately has no ``fileno``: that is how discord.py decides to pump the
    subprocess's stderr through a reader thread into this object.
    """

    __slots__ = ("_url",)

    def __init__(self, url: str) -> None:
        self._url = url

    def write(self, data: bytes) -> int:
        text = data.decode("utf-8", "replace").strip()
        if text:
            log.warning("ffmpeg [%s]: %s", self._url, text)
        return len(data)

    def flush(self) -> None:  # pragma: no cover - required by the file protocol
        pass


def _before_options(stream: StreamInfo) -> str:
    """Static ffmpeg input options plus the headers this stream needs.

    discord.py parses the string with ``shlex.split``, so the header block is
    quoted rather than interpolated raw - a title or cookie containing a space
    would otherwise split into separate arguments.
    """
    headers = stream.http_headers or {}
    lines = [
        f"{name}: {value}"
        for name in _FORWARDED_HEADERS
        # A newline here would let an upstream value inject extra headers.
        if (value := str(headers.get(name, "")).replace("\r", "").replace("\n", ""))
    ]
    if not lines:
        return FFMPEG_BEFORE_OPTIONS
    block = "".join(f"{line}\r\n" for line in lines)
    return f"-headers {shlex.quote(block)} {FFMPEG_BEFORE_OPTIONS}"


class Player(commands.Cog):
    """Owns the queue and the audio pipeline for every guild."""

    def __init__(
        self,
        bot: commands.Bot,
        state: MusicState,
        youtube: Optional[YouTubeService] = None,
    ) -> None:
        self.bot = bot
        self.state = state
        self.youtube = youtube or YouTubeService()

        try:
            self.ffmpeg_path: Optional[str] = resolve_ffmpeg()
            log.info("using ffmpeg at %s", self.ffmpeg_path)
        except FileNotFoundError:
            self.ffmpeg_path = None
            log.error("ffmpeg not found - playback commands will fail", exc_info=True)

    # ------------------------------------------------------------------
    # playback engine
    # ------------------------------------------------------------------

    async def _play_current(
        self,
        destination: discord.abc.Messageable,
        state: GuildState,
        *,
        forced: bool,
    ) -> bool:
        """Start the track at the head of the queue.

        ``destination`` receives this call's reply. For a command invocation it
        is the :class:`~discord.ext.commands.Context`, so that a slash command
        whose response was deferred by ``ctx.typing()`` actually gets answered -
        posting to the channel instead leaves it stuck on "thinking...".
        Follow-up tracks go to the plain channel, since an interaction can only
        be responded to once.

        Returns:
            ``False`` only when nothing was sent because audio already plays.
        """
        channel = getattr(destination, "channel", destination)

        if not state.connected:
            await destination.send(embed=ui.not_in_voice())
            return True

        # Something is already playing; the after-callback will advance us.
        if state.playing:
            return False

        if self.ffmpeg_path is None:
            await destination.send(
                embed=ui.error("ffmpeg is not installed or could not be found.")
            )
            return True

        track = state.current
        if track is None:
            await destination.send(
                embed=ui.error("Didn't have any songs in playlist/queue to play.")
            )
            return True

        # Another invocation for this guild is already resolving a stream.
        # Guilds never block each other; only this one guild's audio output.
        if state.starting:
            return False

        state.starting = True
        stream = None
        unavailable = False
        try:
            stream = await self.youtube.resolve_stream(track.url)
        except ExtractionError as exc:
            log.info("stream unavailable for %s: %s", track.url, exc)
            unavailable = True
        except Exception:
            log.exception("failed to resolve stream for %s", track.url)
            await destination.send(embed=ui.generic_error())
            return True
        finally:
            state.starting = False

        # Deliberately handled *after* the finally above. _advance re-enters
        # _play_current, which bails out while `starting` is set - doing this
        # inside the try left the flag on and stopped a playlist dead at its
        # first unavailable track.
        if unavailable:
            await destination.send(
                embed=ui.error(
                    f"{ui.track_link(track)} is not available, skip to next song."
                )
            )
            await self._advance(channel, state, forced=forced, expect=track)
            return True

        # Resolving took a network round trip, during which another command
        # may have skipped, cleared or disconnected. Re-validate before
        # committing - there is no await between here and voice.play(), so
        # this check and the play form one atomic step.
        if not state.connected:
            await destination.send(embed=ui.not_in_voice())
            return True
        if state.playing or state.current is not track:
            log.debug("playback for %s superseded while resolving", track.url)
            return False

        try:
            source = discord.FFmpegPCMAudio(
                stream.stream_url,
                executable=self.ffmpeg_path,
                before_options=_before_options(stream),
                options=FFMPEG_OPTIONS,
                stderr=_FFmpegLog(track.url),
            )
            # Wrapping before play() means voice.source is the transformer, so
            # ?volume can adjust it live.
            state.playback_started = time.monotonic()
            state.voice.play(
                discord.PCMVolumeTransformer(source, volume=state.volume),
                after=lambda err: self._on_track_end(err, channel, state, track),
            )
        except Exception:
            log.exception("failed to start playback for %s", track.url)
            await destination.send(embed=ui.generic_error())
            return True

        state.cancel_idle_disconnect()

        # Resolve the following track now, while this one plays, so the
        # handover is instant instead of a silent 1-2s extraction.
        if len(state.queue) > 1:
            self.youtube.prefetch_stream(state.queue[1].url)

        user = await self._fetch_requester(channel, track)
        await destination.send(
            embed=ui.now_playing(
                title=stream.title,
                url=track.url,
                duration=stream.duration or track.duration,
                thumbnail=stream.thumbnail,
                user=user,
            )
        )
        return True

    async def _fetch_requester(
        self, channel: discord.abc.Messageable, track: Track
    ) -> discord.abc.User:
        """Resolve the requesting member, falling back to the cache."""
        guild = getattr(channel, "guild", None)
        if guild is not None:
            member = guild.get_member(track.requester_id)
            if member is not None:
                return member
            try:
                return await guild.fetch_member(track.requester_id)
            except discord.HTTPException:
                log.debug("could not fetch member %s", track.requester_id)
        return self.bot.user

    def _on_track_end(
        self,
        error: Optional[Exception],
        channel: discord.abc.Messageable,
        state: GuildState,
        track: Track,
    ) -> None:
        """Voice ``after`` callback. Runs on a non-async thread."""
        if error is not None:
            log.error("playback error in guild %s: %s", state.guild_id, error)

        played = time.monotonic() - state.playback_started
        if not self._ended_early(state, track, played):
            coro = self._advance(channel, state, forced=False, expect=track)
        elif state.retried_url == track.url:
            # Already given a second chance on a fresh URL. Say so rather than
            # letting a third song silently fly past.
            coro = self._give_up(channel, state, track, played)
        else:
            state.retried_url = track.url
            coro = self._retry_current(channel, state, track, played)
        asyncio.run_coroutine_threadsafe(coro, self.bot.loop)

    @staticmethod
    def _ended_early(state: GuildState, track: Track, played: float) -> bool:
        """Did this track end because ffmpeg could not read it?

        ffmpeg exits 0 when the stream URL answers 403, so the after-callback
        cannot tell "finished" from "never started" - the symptom being a song
        announced as now-playing that produces silence and disappears. Comparing
        elapsed audio against the known duration is what separates them.

        A user-initiated stop is excluded: ?skip and ?pause end a track early on
        purpose, and neither is a failure.
        """
        if state.skip_requested or state.suppress_advance:
            return False
        if state.current is not track or not state.connected:
            return False
        expected = max(0, track.duration)
        if expected <= PLAYBACK_FAILURE_SECONDS:
            return False  # too short to judge against
        return played < max(PLAYBACK_FAILURE_SECONDS, expected * MIN_PLAYED_FRACTION)

    async def _give_up(
        self,
        channel: discord.abc.Messageable,
        state: GuildState,
        track: Track,
        played: float,
    ) -> None:
        log.warning(
            "%s failed again after %.1fs of audio; skipping", track.url, played
        )
        await channel.send(
            embed=ui.error(
                f"Couldn't play the audio for {ui.track_link(track)}, "
                "skip to next song."
            )
        )
        await self._advance(channel, state, forced=False, expect=track)

    async def _retry_current(
        self,
        channel: discord.abc.Messageable,
        state: GuildState,
        track: Track,
        played: float,
    ) -> None:
        """Restart the head of the queue on a freshly resolved stream URL.

        The cached URL is dropped first: replaying the link ffmpeg just failed
        on would only fail again.
        """
        log.warning(
            "%s produced %.1fs of audio but is %ss long - refreshing stream URL",
            track.url,
            played,
            track.duration,
        )
        self.youtube.invalidate_stream(track.url)
        try:
            await self._play_current(channel, state, forced=False)
        except Exception:
            log.exception("retry failed for %s", track.url)
            await self._advance(channel, state, forced=False, expect=track)

    async def _advance(
        self,
        channel: discord.abc.Messageable,
        state: GuildState,
        *,
        forced: bool,
        expect: Optional[Track] = None,
    ) -> None:
        """Move to the next track, or wind down when the queue is exhausted.

        ``expect`` is the track the caller believes is playing. If the head of
        the queue has already moved on, another advance beat us to it and this
        one is dropped - otherwise two users pressing ?skip together, or a skip
        racing a track ending naturally, would jump two songs instead of one.
        """
        # ?skip stops the voice client, which fires the after-callback. The
        # command itself calls us with forced=True, so ignore the callback.
        if state.skip_requested and not forced:
            return
        if state.suppress_advance:
            return
        if not state.queue:
            return
        if expect is not None and state.current is not expect:
            log.debug("advance for %r superseded in guild %s", expect.title, state.guild_id)
            return

        try:
            if len(state.queue) > 1:
                state.queue.pop(0)
                state.skip_requested = False
                state.retried_url = None
                await self._play_current(channel, state, forced=False)
            else:
                state.queue.clear()
                state.skip_requested = False
                state.retried_url = None
                state.schedule_idle_disconnect()
                await channel.send(embed=ui.all_played())
        except Exception:
            log.exception("failed to advance queue in guild %s", state.guild_id)

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="play", description="Play audio/music in voice channel")
    async def play(self, ctx: commands.Context) -> None:
        state = self.state.get(ctx.guild.id)

        if state.playing:
            await ctx.send(embed=ui.notice("Is playing right now."))
            return
        if state.paused:
            await ctx.send(
                embed=ui.notice(
                    "The song is paused, use **`?resume`** command to resume playing."
                )
            )
            return

        # Answer the instant-failure cases before signalling "working on it" -
        # there is no point deferring an interaction we can reject right now.
        if not state.connected:
            await ctx.send(embed=ui.not_in_voice())
            return
        if not state.queue:
            await ctx.send(
                embed=ui.error("Didn't have any songs in playlist/queue to play.")
            )
            return

        state.skip_requested = False
        state.suppress_advance = False
        # Reply through ctx, not ctx.channel: for a slash command this defers
        # the interaction, and only ctx.send resolves it.
        async with ui.thinking(ctx):
            responded = await self._play_current(ctx, state, forced=False)

        if not responded:
            # A deferred interaction must never be left unanswered.
            if state.playing:
                await ctx.send(embed=ui.notice("Is playing right now."))
            else:
                await ctx.send(
                    embed=ui.notice("Another command is already starting playback.")
                )

    @commands.hybrid_command(name="add", description="Add music by using youtube link/URL")
    @app_commands.describe(url="Youtube link/Url")
    # One extraction per user at a time, so a single person cannot occupy
    # several extractor threads (or the channel's message budget) at once.
    @commands.max_concurrency(1, per=commands.BucketType.user, wait=False)
    @commands.cooldown(ADD_RATE, ADD_PER, commands.BucketType.user)
    async def add(self, ctx: commands.Context, *, url: str) -> None:
        state = self.state.get(ctx.guild.id)

        async with ui.thinking(ctx):
            try:
                result = await self.youtube.fetch(url)
            except ExtractionError as exc:
                log.info("add failed for %r: %s", url, exc)
                await ctx.send(embed=ui.invalid_link())
                return
            except Exception:
                log.exception("unexpected error adding %r", url)
                await ctx.send(embed=ui.generic_error())
                return

            tracks = [
                Track(
                    url=entry.url,
                    title=entry.title,
                    duration=entry.duration,
                    requester_id=ctx.author.id,
                    requester_name=ctx.author.name,
                )
                for entry in result.entries
            ]
            state.queue.extend(tracks)
            await ctx.send(
                embed=ui.added(
                    tracks[0],
                    extra_count=len(tracks) - 1,
                    unavailable=result.unavailable,
                )
            )

    @add.autocomplete("url")
    async def add_autocomplete(
        self, interaction: Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        """Suggest the video/playlist title for a pasted link.

        Must return within Discord's 3s window or the user sees "The
        application did not respond", so this never waits on a slow lookup:
        :meth:`YouTubeService.preview` has its own deadline and falls back to
        offering the bare URL, which is still a submittable choice.
        """
        query = current.strip()
        if not query:
            return []

        value = normalize_url(query)[:_CHOICE_LIMIT]
        try:
            label = await self.youtube.preview(query)
        except Exception:
            log.exception("autocomplete failed for %r", query)
            return []

        if label is None:
            # Still being typed: stay quiet. Complete but unresolved (slow
            # lookup): offer the link itself so it is still selectable.
            if is_complete_query(value):
                return [app_commands.Choice(name=value, value=value)]
            return []

        return [app_commands.Choice(name=label[:_CHOICE_LIMIT], value=value)]

    @commands.hybrid_command(name="queue", description="Display the queued songs.")
    @commands.cooldown(QUEUE_RATE, QUEUE_PER, commands.BucketType.user)
    async def queue(self, ctx: commands.Context) -> None:
        state = self.state.get(ctx.guild.id)
        if not state.queue:
            await ctx.send(embed=ui.empty_queue())
            return
        await ctx.send(
            embed=ui.queue_page(state.queue, 1, status=self._status_marker(state))
        )

    @commands.hybrid_group(fallback="page", description="Fetch the queue page")
    @app_commands.describe(number="The number of the page")
    @commands.cooldown(QUEUE_RATE, QUEUE_PER, commands.BucketType.user)
    async def queueto(self, ctx: commands.Context, number: int) -> None:
        state = self.state.get(ctx.guild.id)
        if not state.queue:
            await ctx.send(embed=ui.error("Doesn't have any song in the queue."))
            return

        pages = ui.total_pages(len(state.queue), QUEUE_PAGE_SIZE)
        if not 1 <= number <= pages:
            await ctx.send(embed=ui.error("The queue page does not exist."))
            return

        await ctx.send(
            embed=ui.queue_page(state.queue, number, status=self._status_marker(state))
        )

    @commands.hybrid_group(
        fallback="queue", description="Clear the music/song in the queue"
    )
    async def clear(self, ctx: commands.Context) -> None:
        state = self.state.get(ctx.guild.id)
        if not state.queue:
            await ctx.send(embed=ui.empty_queue())
            return

        if state.connected and (state.playing or state.paused):
            # Keep the track that is currently on air.
            del state.queue[1:]
        else:
            state.queue.clear()

        await ctx.send(embed=ui.success("Playlist/queue have been cleared."))

    @commands.hybrid_command(name="skip", description="Skip to the next songs")
    async def skip(self, ctx: commands.Context) -> None:
        state = self.state.get(ctx.guild.id)
        if not state.queue:
            await ctx.send(embed=ui.empty_queue())
            return
        if not state.connected:
            await ctx.send(embed=ui.not_in_voice())
            return

        await self._skip_to_head(ctx, state)

    @commands.hybrid_command(name="skipto", description="Skip to chosen songs")
    @app_commands.describe(number="The number of the song in the queue")
    async def skipto(self, ctx: commands.Context, number: int) -> None:
        state = self.state.get(ctx.guild.id)
        if not state.queue:
            await ctx.send(embed=ui.empty_queue())
            return
        if not 1 <= number <= len(state.queue):
            await ctx.send(embed=ui.error("The number of the song does not exist."))
            return
        if not state.connected:
            await ctx.send(embed=ui.not_in_voice())
            return

        # Drop everything before the target; _advance pops the final one.
        del state.queue[: max(0, number - 2)]
        await self._skip_to_head(ctx, state)

    async def _skip_to_head(self, ctx: commands.Context, state: GuildState) -> None:
        # Pin the track we are skipping past, so two users pressing ?skip at
        # the same moment advance one song between them, not two.
        skipping = state.current
        state.skip_requested = True
        state.suppress_advance = False
        state.voice.stop()
        await ctx.send(
            embed=Embed(
                colour=discord.Colour.dark_gold(),
                description="Skip to the next songs.",
            )
        )
        await self._advance(ctx.channel, state, forced=True, expect=skipping)

    @commands.hybrid_command(
        name="pause", description="Pause current playing audio in voice channel"
    )
    async def pause(self, ctx: commands.Context) -> None:
        state = self.state.get(ctx.guild.id)
        if not state.connected:
            await ctx.send(embed=ui.not_in_voice())
            return

        if state.playing:
            state.voice.pause()
            state.suppress_advance = True
            state.schedule_idle_disconnect()
            await ctx.send(embed=ui.success(":white_check_mark: Song have been paused."))
        elif state.paused:
            await ctx.send(embed=ui.success("The song had already paused."))
        else:
            await ctx.send(embed=ui.error("There's no music playing."))

    @commands.hybrid_command(
        name="resume", description="Resume the paused audio in voice channel"
    )
    async def resume(self, ctx: commands.Context) -> None:
        state = self.state.get(ctx.guild.id)
        if not state.connected:
            await ctx.send(embed=ui.not_in_voice())
            return

        if state.playing:
            await ctx.send(embed=ui.notice("Is playing right now."))
        elif state.paused:
            state.voice.resume()
            state.suppress_advance = False
            state.cancel_idle_disconnect()
            await ctx.send(embed=ui.notice("Resuming the paused song."))
        else:
            await ctx.send(embed=ui.error("There's no music playing."))

    @commands.hybrid_command(name="volume", description="Set the volume for the player")
    @app_commands.describe(number="0 to 100")
    async def volume(self, ctx: commands.Context, number: int) -> None:
        if not 0 <= number <= 100:
            await ctx.send(embed=ui.error("Please choose 0 to 100 to set the volume."))
            return

        state = self.state.get(ctx.guild.id)
        state.volume = number / 100

        source = state.voice.source if state.connected else None
        if isinstance(source, discord.PCMVolumeTransformer):
            source.volume = state.volume

        await ctx.send(embed=ui.success(f"Volume have set to **`{number}%`**."))

    @commands.hybrid_command(
        name="stop",
        description="Stop playing, leave voice channel and clear the playlist/queue",
    )
    async def stop(self, ctx: commands.Context) -> None:
        state = self.state.get(ctx.guild.id)
        state.suppress_advance = True
        await state.disconnect()
        state.reset()
        await ctx.send(
            embed=ui.success(
                ":white_check_mark: Stop playing, leaving voice channel and "
                "queue have been cleared."
            )
        )

    # ------------------------------------------------------------------
    # helpers and error handling
    # ------------------------------------------------------------------

    @staticmethod
    def _status_marker(state: GuildState) -> str:
        if state.playing:
            return "(Playing) "
        if state.paused:
            return "(Paused)"
        return ""

    @staticmethod
    async def _handle_throttle(ctx: commands.Context, error: Exception) -> bool:
        """Answer cooldown / concurrency rejections. Returns True if handled."""
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(
                embed=ui.notice(
                    f"Slow down a moment - try again in "
                    f"**`{error.retry_after:.1f}s`**."
                )
            )
            return True
        if isinstance(error, commands.MaxConcurrencyReached):
            await ctx.send(
                embed=ui.notice("Your previous **`add`** is still being processed.")
            )
            return True
        return False

    async def _handle_argument_error(
        self, ctx: commands.Context, error: Exception, argument: str
    ) -> None:
        if await self._handle_throttle(ctx, error):
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(
                embed=ui.error(
                    f"Missing required **`{argument}/argument`** in the command."
                )
            )
        elif isinstance(error, (commands.BadArgument, commands.ConversionError)):
            await ctx.send(
                embed=ui.error(
                    f"Please use number for the required "
                    f"**`{argument}/argument`** in the command."
                )
            )
        else:
            log.error("command error in %s: %s", ctx.command, error)
            await ctx.send(embed=ui.generic_error())

    @add.error
    async def add_error(self, ctx: commands.Context, error: Exception) -> None:
        if await self._handle_throttle(ctx, error):
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(
                embed=ui.error("Missing required **`url/argument`** in the command.")
            )
        else:
            log.error("add command error: %s", error)

    @queue.error
    async def queue_error(self, ctx: commands.Context, error: Exception) -> None:
        if not await self._handle_throttle(ctx, error):
            log.error("queue command error: %s", error)

    @queueto.error
    async def queueto_error(self, ctx: commands.Context, error: Exception) -> None:
        await self._handle_argument_error(ctx, error, "number")

    @skipto.error
    async def skipto_error(self, ctx: commands.Context, error: Exception) -> None:
        await self._handle_argument_error(ctx, error, "number")

    @volume.error
    async def volume_error(self, ctx: commands.Context, error: Exception) -> None:
        await self._handle_argument_error(ctx, error, "number")

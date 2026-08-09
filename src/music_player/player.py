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
from music_player.audio import BufferedAudioSource
from music_player.controls import PlayerControls, QueuePages
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
                    f"**Couldn't play {ui.track_link(track)}** — it's "
                    "unavailable. Moving to the next song."
                )
            )
            await self._advance(channel, state, forced=forced, expect=track)
            return True

        # Start ffmpeg and let it fill the jitter buffer *before* the final
        # re-validation below, so that check and voice.play() remain a single
        # atomic step. `starting` stays set across the wait: the guild is
        # committed to this track from here on.
        state.starting = True
        try:
            source = BufferedAudioSource(
                discord.FFmpegPCMAudio(
                    stream.stream_url,
                    executable=self.ffmpeg_path,
                    before_options=_before_options(stream),
                    options=FFMPEG_OPTIONS,
                    stderr=_FFmpegLog(track.url),
                ),
                label=track.url,
            )
            await source.wait_until_ready()
        except Exception:
            log.exception("failed to start ffmpeg for %s", track.url)
            await destination.send(embed=ui.generic_error())
            return True
        finally:
            state.starting = False

        # Resolving and buffering took time, during which another command may
        # have skipped, cleared or disconnected. Re-validate before committing -
        # there is no await between here and voice.play(), so this check and the
        # play form one atomic step.
        if not state.connected:
            source.cleanup()
            await destination.send(embed=ui.not_in_voice())
            return True
        if state.playing or state.current is not track:
            source.cleanup()
            log.debug("playback for %s superseded while resolving", track.url)
            return False

        try:
            # Wrapping before play() means voice.source is the transformer, so
            # ?volume can adjust it live. The buffer sits underneath it, so a
            # volume change applies to the next frame out rather than to audio
            # queued seconds ago.
            state.mark_started()
            state.voice.play(
                discord.PCMVolumeTransformer(source, volume=state.volume),
                after=lambda err: self._on_track_end(err, channel, state, track),
            )
        except Exception:
            source.cleanup()
            log.exception("failed to start playback for %s", track.url)
            await destination.send(embed=ui.generic_error())
            return True

        state.cancel_idle_disconnect()

        # Resolve the following track now, while this one plays, so the
        # handover is instant instead of a silent 1-2s extraction.
        if len(state.queue) > 1:
            self.youtube.prefetch_stream(state.queue[1].url)

        # The previous track's buttons refer to a song that is no longer on
        # air; grey them out before posting the ones that replace them.
        await state.retire_controls()

        user = await self._fetch_requester(channel, track)
        snapshot = self._snapshot(state, stream, track, user)
        view = PlayerControls(self, state, snapshot)
        try:
            view.message = await destination.send(
                embed=ui.now_playing(snapshot), view=view
            )
            state.controls = view
        except discord.HTTPException:
            # The song is playing either way - a missing announcement must not
            # take the audio down with it.
            log.warning("could not post the now playing message", exc_info=True)
        return True

    @staticmethod
    def _snapshot(
        state: GuildState,
        stream: StreamInfo,
        track: Track,
        user: Optional[discord.abc.User],
    ) -> ui.NowPlaying:
        """Freeze everything the Now Playing embed needs into one value."""
        return ui.NowPlaying(
            title=stream.title,
            url=track.url,
            duration=stream.duration or track.duration,
            thumbnail=stream.thumbnail,
            requester=user,
            volume=state.volume,
            # The playing track is always the head of the queue.
            position=1,
            total=len(state.queue),
            up_next=state.queue[1] if len(state.queue) > 1 else None,
            remaining=ui.total_duration(state.queue),
        )

    async def _fetch_requester(
        self, channel: discord.abc.Messageable, track: Track
    ) -> Optional[discord.abc.User]:
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
                f"**Still couldn't play {ui.track_link(track)}** after a retry. "
                "Moving to the next song."
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
                # Nothing is playing, so the controls under the last Now
                # Playing message have nothing left to control.
                await state.retire_controls()
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
            await ctx.send(embed=ui.notice("▶  **Already playing.**"))
            return
        if state.paused:
            await ctx.send(
                embed=ui.notice(
                    "⏸  **That song is paused.**\nUse **`?resume`** to pick it "
                    "back up."
                )
            )
            return

        # Answer the instant-failure cases before signalling "working on it" -
        # there is no point deferring an interaction we can reject right now.
        if not state.connected:
            await ctx.send(embed=ui.not_in_voice())
            return
        if not state.queue:
            await ctx.send(embed=ui.empty_queue())
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
                await ctx.send(embed=ui.notice("▶  **Already playing.**"))
            else:
                await ctx.send(
                    embed=ui.notice("⏳  **Starting up** — another request got here first.")
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

            # Where these landed, and how long until the first is heard. A
            # countdown is only offered while audio is actually advancing -
            # a paused or idle queue would make it a guess.
            first_index = len(state.queue) - len(tracks)
            starts_in = None
            if state.playing:
                ahead = ui.total_duration(state.queue[:first_index])
                starts_in = max(0, ahead - int(state.elapsed))

            await ctx.send(
                embed=ui.added(
                    tracks[0],
                    extra_count=len(tracks) - 1,
                    unavailable=result.unavailable,
                    playlist_title=result.playlist_title,
                    playlist_url=normalize_url(url) if result.is_playlist else None,
                    total_seconds=ui.total_duration(tracks),
                    position=first_index + 1,
                    starts_in=starts_in,
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

    @commands.hybrid_command(name="queue", description="Show the songs waiting to play")
    @commands.cooldown(QUEUE_RATE, QUEUE_PER, commands.BucketType.user)
    async def queue(self, ctx: commands.Context) -> None:
        state = self.state.get(ctx.guild.id)
        if not state.queue:
            await ctx.send(embed=ui.empty_queue())
            return
        view = QueuePages(self, state, user_id=ctx.author.id)
        view.message = await ctx.send(embed=view.render(), view=view)

    @commands.hybrid_group(fallback="page", description="Jump to a page of the queue")
    @app_commands.describe(number="The number of the page")
    @commands.cooldown(QUEUE_RATE, QUEUE_PER, commands.BucketType.user)
    async def queueto(self, ctx: commands.Context, number: int) -> None:
        state = self.state.get(ctx.guild.id)
        if not state.queue:
            await ctx.send(embed=ui.empty_queue())
            return

        pages = ui.total_pages(len(state.queue), QUEUE_PAGE_SIZE)
        if not 1 <= number <= pages:
            await ctx.send(embed=ui.no_such_page(number, pages))
            return

        view = QueuePages(self, state, user_id=ctx.author.id, page=number)
        view.message = await ctx.send(embed=view.render(), view=view)

    @commands.hybrid_group(fallback="queue", description="Empty the queue")
    async def clear(self, ctx: commands.Context) -> None:
        state = self.state.get(ctx.guild.id)
        if not state.queue:
            await ctx.send(embed=ui.empty_queue())
            return

        kept_playing = state.connected and (state.playing or state.paused)
        if kept_playing:
            # Keep the track that is currently on air.
            del state.queue[1:]
        else:
            state.queue.clear()

        await ctx.send(embed=ui.cleared(kept_playing))
        await self._repaint(state)

    @commands.hybrid_command(name="skip", description="Skip to the next song")
    async def skip(self, ctx: commands.Context) -> None:
        state = self.state.get(ctx.guild.id)
        if not state.queue:
            await ctx.send(embed=ui.empty_queue())
            return
        if not state.connected:
            await ctx.send(embed=ui.not_in_voice())
            return

        await ctx.send(embed=ui.skipped(state.current))
        await self.perform_skip(ctx.channel, state)

    @commands.hybrid_command(name="skipto", description="Jump to a song further down")
    @app_commands.describe(number="The number of the song in the queue")
    async def skipto(self, ctx: commands.Context, number: int) -> None:
        state = self.state.get(ctx.guild.id)
        if not state.queue:
            await ctx.send(embed=ui.empty_queue())
            return
        if not 1 <= number <= len(state.queue):
            await ctx.send(embed=ui.no_such_song(number, len(state.queue)))
            return
        if not state.connected:
            await ctx.send(embed=ui.not_in_voice())
            return

        target = state.queue[number - 1]
        # Drop everything before the target; _advance pops the final one.
        del state.queue[: max(0, number - 2)]
        await ctx.send(embed=ui.jumping_to(target))
        await self.perform_skip(ctx.channel, state)

    @commands.hybrid_command(name="pause", description="Pause the current song")
    async def pause(self, ctx: commands.Context) -> None:
        state = self.state.get(ctx.guild.id)
        if not state.connected:
            await ctx.send(embed=ui.not_in_voice())
            return

        if self.apply_pause(state):
            await ctx.send(embed=ui.paused())
            await self._repaint(state)
        elif state.paused:
            await ctx.send(embed=ui.notice("⏸  **Already paused.**"))
        else:
            await ctx.send(embed=ui.nothing_playing())

    @commands.hybrid_command(name="resume", description="Resume the paused song")
    async def resume(self, ctx: commands.Context) -> None:
        state = self.state.get(ctx.guild.id)
        if not state.connected:
            await ctx.send(embed=ui.not_in_voice())
            return

        if self.apply_resume(state):
            await ctx.send(embed=ui.resumed())
            await self._repaint(state)
        elif state.playing:
            await ctx.send(embed=ui.notice("▶  **Already playing.**"))
        else:
            await ctx.send(embed=ui.nothing_playing())

    @commands.hybrid_command(name="nowplaying", description="Show what's playing now")
    async def nowplaying(self, ctx: commands.Context) -> None:
        """The progress bar's real home.

        The Now Playing announcement is a snapshot from the moment a song
        started; this is the one that answers "how much of this is left".
        """
        state = self.state.get(ctx.guild.id)
        embed = self._current_embed(state)
        if embed is None:
            await ctx.send(embed=ui.nothing_playing())
            return
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="volume", description="Set the playback volume")
    @app_commands.describe(number="0 to 100")
    async def volume(self, ctx: commands.Context, number: int) -> None:
        if not 0 <= number <= 100:
            await ctx.send(
                embed=ui.error(
                    "**Volume goes from 0 to 100.**\nTry **`?volume 25`**."
                )
            )
            return

        state = self.state.get(ctx.guild.id)
        state.volume = number / 100

        source = state.voice.source if state.connected else None
        if isinstance(source, discord.PCMVolumeTransformer):
            source.volume = state.volume

        await ctx.send(embed=ui.volume_set(number))
        await self._repaint(state)

    @commands.hybrid_command(
        name="stop", description="Stop, clear the queue and leave the channel"
    )
    async def stop(self, ctx: commands.Context) -> None:
        state = self.state.get(ctx.guild.id)
        state.suppress_advance = True
        await state.retire_controls()
        await state.disconnect()
        state.reset()
        await ctx.send(embed=ui.stopped())

    # ------------------------------------------------------------------
    # shared actions - the commands above and the buttons in ``controls``
    # both route through these, so a press and a command cannot drift apart
    # ------------------------------------------------------------------

    def apply_pause(self, state: GuildState) -> bool:
        """Pause playback. Returns False when there was nothing to pause."""
        if not state.playing:
            return False
        state.voice.pause()
        state.mark_paused()
        state.suppress_advance = True
        state.schedule_idle_disconnect()
        return True

    def apply_resume(self, state: GuildState) -> bool:
        """Resume playback. Returns False when nothing was paused."""
        if not state.paused:
            return False
        state.voice.resume()
        state.mark_resumed()
        state.suppress_advance = False
        state.cancel_idle_disconnect()
        return True

    async def perform_skip(
        self, channel: discord.abc.Messageable, state: GuildState
    ) -> None:
        """Advance past the head of the queue and start whatever follows."""
        # Pin the track we are skipping past, so two users skipping at the same
        # moment advance one song between them, not two.
        skipping = state.current
        state.skip_requested = True
        state.suppress_advance = False
        await state.retire_controls()
        if state.connected:
            state.voice.stop()
        await self._advance(channel, state, forced=True, expect=skipping)

    @staticmethod
    def status_marker(state: GuildState) -> str:
        """The marker shown against the head of the queue."""
        if state.playing:
            return ui.PLAYING_MARKER
        if state.paused:
            return ui.PAUSED_MARKER
        return ""

    # ------------------------------------------------------------------
    # helpers and error handling
    # ------------------------------------------------------------------

    @staticmethod
    async def _repaint(state: GuildState) -> None:
        """Keep the live Now Playing message in step with a command.

        Pausing from a button and pausing with ``?pause`` must leave the
        channel looking identical, so the command path repaints the embed the
        button path would have edited.
        """
        view = state.controls
        if view is not None:
            await view.repaint()

    @staticmethod
    def _current_embed(state: GuildState) -> Optional[Embed]:
        """Render the current track, or ``None`` if nothing is on air."""
        if not (state.playing or state.paused) or state.current is None:
            return None

        view = state.controls
        if view is not None and not view.is_finished():
            # The live view holds the resolved title and artwork.
            return view.render()

        # No view (restarted, or the controls timed out): fall back to what the
        # queue itself knows, which is enough for a progress bar. Artwork comes
        # off the link rather than a re-resolve.
        track = state.current
        return ui.now_playing(
            ui.NowPlaying(
                title=track.title,
                url=track.url,
                duration=track.duration,
                thumbnail=ui.artwork(track.url),
                requester=None,
                volume=state.volume,
                position=1,
                total=len(state.queue),
                up_next=state.queue[1] if len(state.queue) > 1 else None,
                remaining=ui.total_duration(state.queue),
                elapsed=state.elapsed,
                paused=state.paused,
            )
        )

    @staticmethod
    async def _handle_throttle(ctx: commands.Context, error: Exception) -> bool:
        """Answer cooldown / concurrency rejections. Returns True if handled."""
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(
                embed=ui.notice(
                    f"⏳  **Slow down a moment** — try again in "
                    f"**{error.retry_after:.1f}s**."
                )
            )
            return True
        if isinstance(error, commands.MaxConcurrencyReached):
            await ctx.send(
                embed=ui.notice(
                    "⏳  **Your last `?add` is still being processed.**\n"
                    "Nothing's lost — give it a second."
                )
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
                    f"**This command needs a {argument}.**\n"
                    f"For example: **`?{ctx.invoked_with} 3`**"
                )
            )
        elif isinstance(error, (commands.BadArgument, commands.ConversionError)):
            await ctx.send(
                embed=ui.error(
                    f"**The {argument} has to be a whole number.**\n"
                    f"For example: **`?{ctx.invoked_with} 3`**"
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
                embed=ui.error(
                    "**This command needs a YouTube link.**\n"
                    "For example: **`?add https://youtu.be/dQw4w9WgXcQ`**"
                )
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

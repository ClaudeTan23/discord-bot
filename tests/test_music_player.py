"""Unit tests for the pure logic in the music player.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Several tests deliberately trigger failure paths; keep their logging out of
# the test output.
logging.disable(logging.CRITICAL)

import discord  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

from music_player import ui  # noqa: E402
from music_player.state import GuildState, MusicState, Track  # noqa: E402
from music_player.ytdl import (  # noqa: E402
    FetchResult,
    TrackInfo,
    _to_track,
    is_complete_query,
    is_youtube_url,
    normalize_url,
)

VIDEO = "dQw4w9WgXcQ"
LIST = "PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI"
WATCH = f"https://www.youtube.com/watch?v={VIDEO}"


class TestNormalizeUrl(unittest.TestCase):
    def test_plain_watch_url_is_unchanged(self):
        self.assertEqual(normalize_url(WATCH), WATCH)

    def test_whitespace_is_trimmed(self):
        self.assertEqual(normalize_url(f"   {WATCH}  "), WATCH)

    def test_angle_brackets_are_stripped_for_https(self):
        self.assertEqual(normalize_url(f"<{WATCH}>"), WATCH)

    def test_angle_brackets_are_stripped_for_http(self):
        """Regression: the old two-if/else pair discarded the http result."""
        self.assertEqual(normalize_url(f"<http://youtu.be/{VIDEO}>"), WATCH)

    def test_list_param_is_dropped_from_watch_url(self):
        self.assertEqual(normalize_url(f"{WATCH}&list={LIST}"), WATCH)

    def test_list_param_is_dropped_over_http(self):
        """Regression: the old code only split on the literal https host."""
        self.assertEqual(
            normalize_url(f"http://www.youtube.com/watch?v={VIDEO}&list={LIST}"), WATCH
        )

    def test_short_link_becomes_watch_url(self):
        self.assertEqual(normalize_url(f"https://youtu.be/{VIDEO}"), WATCH)

    def test_short_link_with_list_drops_the_list(self):
        self.assertEqual(normalize_url(f"https://youtu.be/{VIDEO}?list={LIST}"), WATCH)

    def test_music_subdomain_is_normalised(self):
        self.assertEqual(
            normalize_url(f"https://music.youtube.com/watch?v={VIDEO}&list={LIST}"),
            WATCH,
        )

    def test_bare_playlist_url_is_preserved(self):
        """A playlist link must still queue the whole playlist."""
        url = f"https://www.youtube.com/playlist?list={LIST}"
        self.assertEqual(normalize_url(url), url)

    def test_shorts_url_is_preserved(self):
        url = "https://www.youtube.com/shorts/tPEE9ZwTmy0"
        self.assertEqual(normalize_url(url), url)

    def test_non_url_text_is_passed_through(self):
        self.assertEqual(normalize_url("never gonna give you up"), "never gonna give you up")

    def test_non_youtube_url_is_untouched(self):
        url = "https://example.com/watch?v=abc&list=xyz"
        self.assertEqual(normalize_url(url), url)

    def test_empty_input(self):
        self.assertEqual(normalize_url("   "), "")


class TestToTrack(unittest.TestCase):
    def test_valid_entry(self):
        track = _to_track({"title": "T", "url": WATCH, "duration": 213})
        self.assertEqual(track, TrackInfo(url=WATCH, title="T", duration=213))

    def test_float_duration_is_truncated(self):
        self.assertEqual(_to_track({"title": "T", "url": WATCH, "duration": 212.7}).duration, 212)

    def test_string_duration_is_parsed(self):
        self.assertEqual(_to_track({"title": "T", "url": WATCH, "duration": "213"}).duration, 213)

    def test_missing_duration_is_rejected(self):
        """Private/deleted videos report no duration and must be skipped."""
        self.assertIsNone(_to_track({"title": "T", "url": WATCH, "duration": None}))

    def test_na_duration_is_rejected(self):
        self.assertIsNone(_to_track({"title": "T", "url": WATCH, "duration": "NA"}))

    def test_zero_duration_is_rejected(self):
        self.assertIsNone(_to_track({"title": "T", "url": WATCH, "duration": 0}))

    def test_missing_title_is_rejected(self):
        self.assertIsNone(_to_track({"url": WATCH, "duration": 10}))

    def test_none_entry_is_rejected(self):
        self.assertIsNone(_to_track(None))

    def test_webpage_url_wins_over_url(self):
        track = _to_track({"title": "T", "url": "flat", "webpage_url": WATCH, "duration": 5})
        self.assertEqual(track.url, WATCH)


class TestFormatDuration(unittest.TestCase):
    def test_seconds_are_zero_padded(self):
        self.assertEqual(ui.format_duration(5), "0:05")

    def test_minutes_and_seconds(self):
        self.assertEqual(ui.format_duration(213), "3:33")

    def test_exact_minute(self):
        self.assertEqual(ui.format_duration(120), "2:00")

    def test_hours_are_rendered(self):
        """The old formatter showed 9350s as '155:50'."""
        self.assertEqual(ui.format_duration(9350), "2:35:50")

    def test_exact_hour(self):
        self.assertEqual(ui.format_duration(3600), "1:00:00")

    def test_zero_and_negative(self):
        self.assertEqual(ui.format_duration(0), "0:00")
        self.assertEqual(ui.format_duration(-5), "0:00")


class TestPagination(unittest.TestCase):
    def test_total_pages(self):
        self.assertEqual(ui.total_pages(0), 1)
        self.assertEqual(ui.total_pages(1), 1)
        self.assertEqual(ui.total_pages(10), 1)
        self.assertEqual(ui.total_pages(11), 2)
        self.assertEqual(ui.total_pages(183), 19)

    def _tracks(self, n):
        return [
            Track(url=f"https://y/{i}", title=f"Song {i}", duration=60 + i,
                  requester_id=42, requester_name="tester")
            for i in range(n)
        ]

    def test_first_page_lists_ten(self):
        embed = ui.queue_page(self._tracks(183), 1)
        self.assertEqual(len(embed.description.splitlines()), 10)
        self.assertEqual(embed.footer.text, "Page 1/19")

    def test_last_page_lists_remainder(self):
        embed = ui.queue_page(self._tracks(183), 19)
        self.assertEqual(len(embed.description.splitlines()), 3)
        self.assertEqual(embed.footer.text, "Page 19/19")

    def test_page_numbering_is_absolute(self):
        embed = ui.queue_page(self._tracks(183), 2)
        self.assertTrue(embed.description.startswith("11. "))

    def test_status_marker_only_on_first_track(self):
        embed = ui.queue_page(self._tracks(20), 1, status="(Playing) ")
        lines = embed.description.splitlines()
        self.assertIn("(Playing) ", lines[0])
        self.assertNotIn("(Playing) ", lines[1])

    def test_status_marker_absent_on_later_pages(self):
        embed = ui.queue_page(self._tracks(20), 2, status="(Playing) ")
        self.assertNotIn("(Playing) ", embed.description)

    def test_requester_is_mentioned(self):
        embed = ui.queue_page(self._tracks(1), 1)
        self.assertIn("<@42>", embed.description)


class TestTitleRendering(unittest.TestCase):
    """Titles must appear exactly as YouTube reports them.

    Regression: escaping markdown put visible backslashes in the queue, because
    Discord does not unescape inside a [label](url) link.
    """

    @staticmethod
    def _link(title):
        return ui.track_link(Track(WATCH, title, 10, 1, "u"))

    def test_square_brackets_are_not_escaped(self):
        title = "スパークル [original ver.] -Your name. Music Video edition-"
        rendered = self._link(title)
        self.assertNotIn("\\", rendered)
        self.assertIn(title, rendered)

    def test_pipe_is_not_escaped(self):
        rendered = self._link("FIGHTER: Sher Khul Gaye | Vishal-Sheykhar")
        self.assertNotIn("\\", rendered)

    def test_official_video_tag_is_intact(self):
        rendered = self._link("Finesse2Tymes - Crazy [Official Music Video]")
        self.assertIn("[Official Music Video]", rendered)
        self.assertNotIn("\\", rendered)

    def test_queue_listing_has_no_backslashes(self):
        tracks = [
            Track(WATCH, "Lil Uzi Vert - Red Moon [Official Music Video]", 60, 1, "u"),
            Track(WATCH, "Song | Official", 60, 1, "u"),
        ]
        self.assertNotIn("\\", ui.queue_page(tracks, 1).description)

    def test_added_embed_has_no_backslashes(self):
        track = Track(WATCH, "Crazy [Official Music Video]", 60, 1, "u")
        self.assertNotIn("\\", ui.added(track).description)


class TestAddedEmbed(unittest.TestCase):
    def _track(self):
        return Track(url=WATCH, title="Song", duration=10, requester_id=1, requester_name="u")

    def test_single_track_wording(self):
        self.assertIn("Added [Song](<%s>) in to playlist." % WATCH,
                      ui.added(self._track()).description)

    def test_playlist_wording(self):
        self.assertIn("and 5 songs in to playlist.",
                      ui.added(self._track(), extra_count=5).description)


class TestGuildState(unittest.TestCase):
    def test_defaults(self):
        state = GuildState(1)
        self.assertFalse(state.connected)
        self.assertFalse(state.playing)
        self.assertFalse(state.paused)
        self.assertIsNone(state.current)
        self.assertEqual(state.volume, 0.10)

    def test_properties_are_safe_without_a_voice_client(self):
        """The old ChannelValidation raised KeyError in this situation."""
        state = GuildState(1)
        for attr in ("connected", "playing", "paused"):
            self.assertFalse(getattr(state, attr))

    def test_store_returns_the_same_object(self):
        store = MusicState()
        self.assertIs(store.get(7), store.get(7))
        self.assertIsNot(store.get(7), store.get(8))

    def test_reset_clears_queue_and_flags(self):
        state = GuildState(1)
        state.queue.append(Track(WATCH, "t", 1, 1, "u"))
        state.skip_requested = True
        state.suppress_advance = True
        state.reset()
        self.assertEqual(state.queue, [])
        self.assertFalse(state.skip_requested)
        self.assertFalse(state.suppress_advance)

    def test_idle_disconnect_can_be_cancelled(self):
        async def scenario():
            state = GuildState(1)
            state.schedule_idle_disconnect(delay=30)
            self.assertIsNotNone(state._idle_task)
            state.cancel_idle_disconnect()
            self.assertIsNone(state._idle_task)

        asyncio.run(scenario())

    def test_rescheduling_replaces_the_previous_timer(self):
        async def scenario():
            state = GuildState(1)
            state.schedule_idle_disconnect(delay=30)
            first = state._idle_task
            state.schedule_idle_disconnect(delay=30)
            await asyncio.sleep(0)
            self.assertTrue(first.cancelled() or first.done())
            self.assertIsNot(first, state._idle_task)
            state.cancel_idle_disconnect()

        asyncio.run(scenario())


class TestFetchResult(unittest.TestCase):
    def test_single_video_is_not_a_playlist(self):
        result = FetchResult(entries=[TrackInfo(WATCH, "t", 1)])
        self.assertFalse(result.is_playlist)

    def test_playlist_is_flagged(self):
        result = FetchResult(entries=[TrackInfo(WATCH, "t", 1)], playlist_title="Mix")
        self.assertTrue(result.is_playlist)


class TestSkiptoArithmetic(unittest.TestCase):
    """?skipto trims the queue, then _advance pops one more."""

    @staticmethod
    def simulate(queue_len: int, number: int) -> int:
        queue = list(range(1, queue_len + 1))
        del queue[: max(0, number - 2)]
        if len(queue) > 1:  # what _advance does
            queue.pop(0)
        return queue[0]

    def test_skipto_lands_on_the_requested_song(self):
        for number in range(2, 11):
            with self.subTest(number=number):
                self.assertEqual(self.simulate(20, number), number)

    def test_skipto_last_song(self):
        self.assertEqual(self.simulate(20, 20), 20)


class TestUrlGating(unittest.TestCase):
    """Autocomplete must reject half-typed input without a network call."""

    def test_complete_urls_are_accepted(self):
        for url in (
            WATCH,
            f"https://www.youtube.com/playlist?list={LIST}",
            "https://www.youtube.com/shorts/tPEE9ZwTmy0",
            f"https://music.youtube.com/watch?v={VIDEO}",
        ):
            with self.subTest(url=url):
                self.assertTrue(is_youtube_url(url))
                self.assertTrue(is_complete_query(url))

    def test_partial_typing_is_rejected(self):
        for text in (
            "h",
            "https:",
            "https://www.yout",
            "https://www.youtube.com/",
            "https://www.youtube.com/watch",
            "not a url",
            "",
        ):
            with self.subTest(text=text):
                self.assertFalse(is_complete_query(text))

    def test_half_typed_video_id_is_rejected(self):
        """A YouTube id is 11 chars; anything shorter is mid-keystroke."""
        self.assertFalse(is_complete_query("https://www.youtube.com/watch?v=dQw4w9Wg"))
        self.assertTrue(is_complete_query(f"https://www.youtube.com/watch?v={VIDEO}"))

    def test_non_youtube_hosts_are_rejected(self):
        self.assertFalse(is_youtube_url("https://example.com/watch?v=dQw4w9WgXcQ"))
        self.assertFalse(is_youtube_url("https://notyoutube.com/watch?v=dQw4w9WgXcQ"))

    def test_subdomains_are_accepted(self):
        self.assertTrue(is_youtube_url(f"https://m.youtube.com/watch?v={VIDEO}"))


class TestPreview(unittest.IsolatedAsyncioTestCase):
    """Behaviour that keeps autocomplete inside Discord's 3s window."""

    def setUp(self):
        from music_player.ytdl import YouTubeService

        self.calls = []
        self.original = YouTubeService._extract

    def tearDown(self):
        from music_player.ytdl import YouTubeService

        YouTubeService._extract = self.original

    def _patch(self, delay=0.0, result=None):
        import time as _time
        from music_player.ytdl import YouTubeService

        calls = self.calls

        def fake(url, opts):
            calls.append((url, opts))
            if delay:
                _time.sleep(delay)
            return result if result is not None else {"title": "Fake Song"}

        YouTubeService._extract = staticmethod(fake)

    async def test_partial_input_never_touches_the_network(self):
        from music_player.ytdl import YouTubeService

        self._patch()
        yt = YouTubeService()
        for text in ("h", "https://www.you", "https://www.youtube.com/watch?v=dQw4"):
            self.assertIsNone(await yt.preview(text))
        self.assertEqual(self.calls, [], "no extraction should have run")

    async def test_concurrent_keystrokes_share_one_extraction(self):
        from music_player.ytdl import YouTubeService

        self._patch(delay=0.2)
        yt = YouTubeService()
        results = await asyncio.gather(*[yt.preview(WATCH) for _ in range(10)])
        self.assertEqual(len(self.calls), 1, "10 keystrokes must not mean 10 lookups")
        self.assertTrue(all(r == "Fake Song" for r in results))

    async def test_repeat_lookups_are_cached(self):
        from music_player.ytdl import YouTubeService

        self._patch()
        yt = YouTubeService()
        for _ in range(5):
            await yt.preview(WATCH)
        self.assertEqual(len(self.calls), 1)

    async def test_timeout_returns_none_but_keeps_working(self):
        """A slow lookup must not stall the response past Discord's window."""
        from music_player.ytdl import YouTubeService

        self._patch(delay=0.4)
        yt = YouTubeService()

        self.assertIsNone(await yt.preview(WATCH, timeout=0.05))
        await asyncio.sleep(0.6)
        # The shielded task finished in the background and warmed the cache.
        self.assertEqual(await yt.preview(WATCH, timeout=0.05), "Fake Song")
        self.assertEqual(len(self.calls), 1)

    async def test_playlist_preview_stops_after_first_entry(self):
        from music_player.ytdl import YouTubeService

        self._patch(result={"title": "Popular Music Videos"})
        yt = YouTubeService()
        label = await yt.preview(f"https://www.youtube.com/playlist?list={LIST}")
        self.assertEqual(label, "Popular Music Videos")
        _, opts = self.calls[0]
        self.assertEqual(opts.get("playlist_items"), "1")

    async def test_extraction_failure_is_swallowed(self):
        from music_player.ytdl import YouTubeService

        self._patch(result={})
        yt = YouTubeService()
        self.assertIsNone(await yt.preview(WATCH))


class FakeChannel:
    """Stands in for a discord.TextChannel."""

    def __init__(self) -> None:
        self.sent: list = []
        self.guild = None
        self.typing_calls = 0

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return object()

    async def typing(self):
        self.typing_calls += 1


class FakeContext:
    """Stands in for commands.Context (which exposes .channel)."""

    def __init__(self, channel: FakeChannel) -> None:
        self.channel = channel
        self.sent: list = []
        self.guild = None
        self.interaction = None  # prefix-command context

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return object()

    def typing(self):
        class _Defer:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *exc):
                return False

        return _Defer()


class FakeVoice:
    def __init__(self) -> None:
        self.playing = False
        self.paused = False
        self.source = None
        self.after = None
        self.play_calls = 0
        self.channel = type("Ch", (), {"id": 999})()

    def is_connected(self):
        return True

    def is_playing(self):
        return self.playing

    def is_paused(self):
        return self.paused

    def play(self, source, after=None):
        # Real discord.py raises ClientException if already playing.
        if self.playing:
            raise RuntimeError("Already playing audio")
        self.play_calls += 1
        self.source = source
        self.after = after
        self.playing = True

    def pause(self):
        if self.playing:
            self.playing = False
            self.paused = True

    def resume(self):
        if self.paused:
            self.paused = False
            self.playing = True

    def stop(self):
        self.playing = False
        self.paused = False

    async def disconnect(self, *, force=False):
        self.playing = False
        self.paused = False

    async def move_to(self, channel):
        self.channel = channel


class FakeYouTube:
    def __init__(self):
        self.prefetched: list = []
        self.invalidated: list = []

    async def resolve_stream(self, url):
        from music_player.ytdl import StreamInfo

        return StreamInfo(stream_url="https://stream", title="Song",
                          duration=10, thumbnail=None)

    def prefetch_stream(self, url):
        self.prefetched.append(url)

    def invalidate_stream(self, url):
        self.invalidated.append(url)


class TestPlayResponds(unittest.IsolatedAsyncioTestCase):
    """A slash command that defers must be answered through the Context.

    ``ctx.typing()`` defers the interaction; only ``ctx.send`` resolves it.
    Replying to ``ctx.channel`` posts a normal message and leaves the command
    stuck showing "Bot is thinking...".
    """

    def _player(self):
        from unittest.mock import MagicMock
        from music_player.player import Player

        bot = MagicMock()
        bot.user = MagicMock()
        player = Player(bot, MusicState(), FakeYouTube())
        player.ffmpeg_path = "ffmpeg"
        return player

    def _state_with_track(self):
        state = GuildState(1)
        state.voice = FakeVoice()
        state.queue.append(Track(WATCH, "Song", 10, 42, "tester"))
        return state

    async def test_reply_goes_to_context_not_channel(self):
        from unittest.mock import MagicMock, patch
        import music_player.player as mp

        player = self._player()
        state = self._state_with_track()
        channel = FakeChannel()
        ctx = FakeContext(channel)

        with patch.object(mp.discord, "FFmpegPCMAudio", MagicMock()), \
             patch.object(mp.discord, "PCMVolumeTransformer", MagicMock()):
            responded = await player._play_current(ctx, state, forced=False)

        self.assertTrue(responded)
        self.assertEqual(len(ctx.sent), 1, "reply must resolve the interaction")
        self.assertEqual(len(channel.sent), 0, "must not bypass the interaction")

    async def test_returns_false_when_already_playing(self):
        player = self._player()
        state = self._state_with_track()
        state.voice.playing = True
        ctx = FakeContext(FakeChannel())

        responded = await player._play_current(ctx, state, forced=False)
        self.assertFalse(responded)
        self.assertEqual(ctx.sent, [])

    async def test_play_command_always_answers(self):
        """Every branch of ?play must produce exactly one reply on ctx."""
        from unittest.mock import MagicMock, patch
        import music_player.player as mp

        scenarios = {}

        disconnected = GuildState(1)
        scenarios["not connected"] = disconnected

        empty = GuildState(1)
        empty.voice = FakeVoice()
        scenarios["connected, empty queue"] = empty

        busy = self._state_with_track()
        busy.voice.playing = True
        scenarios["already playing"] = busy

        ready = self._state_with_track()
        scenarios["ready to play"] = ready

        for label, state in scenarios.items():
            with self.subTest(label):
                player = self._player()
                player.state._guilds[1] = state
                ctx = FakeContext(FakeChannel())
                ctx.guild = type("G", (), {"id": 1})()

                with patch.object(mp.discord, "FFmpegPCMAudio", MagicMock()), \
                     patch.object(mp.discord, "PCMVolumeTransformer", MagicMock()):
                    await player.play.callback(player, ctx)

                self.assertEqual(
                    len(ctx.sent), 1, f"{label}: expected exactly one reply"
                )
                self.assertEqual(len(ctx.channel.sent), 0, f"{label}: bypassed ctx")

    async def test_followup_tracks_post_to_the_channel(self):
        """Track 2 onward cannot reuse the interaction, so it posts normally."""
        player = self._player()
        state = self._state_with_track()
        state.queue.append(Track(WATCH, "Song 2", 10, 42, "tester"))
        channel = FakeChannel()

        from unittest.mock import MagicMock, patch
        import music_player.player as mp

        with patch.object(mp.discord, "FFmpegPCMAudio", MagicMock()), \
             patch.object(mp.discord, "PCMVolumeTransformer", MagicMock()):
            await player._advance(channel, state, forced=True)

        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(len(state.queue), 1)

    async def test_exhausted_queue_announces_on_the_channel(self):
        player = self._player()
        state = self._state_with_track()
        channel = FakeChannel()

        await player._advance(channel, state, forced=True)

        self.assertEqual(state.queue, [])
        self.assertEqual(len(channel.sent), 1)
        state.cancel_idle_disconnect()


class TestThrottleMessages(unittest.IsolatedAsyncioTestCase):
    """One user must not silently absorb a channel's message budget."""

    async def _handle(self, error):
        from discord.ext import commands
        from music_player.player import Player

        ctx = FakeContext(FakeChannel())
        handled = await Player._handle_throttle(ctx, error)
        return handled, ctx

    async def test_cooldown_tells_the_user_how_long_to_wait(self):
        from discord.ext import commands

        error = commands.CommandOnCooldown(MagicMock(), 2.5, MagicMock())
        handled, ctx = await self._handle(error)

        self.assertTrue(handled)
        self.assertEqual(len(ctx.sent), 1)
        self.assertIn("2.5s", ctx.sent[0]["embed"].description)

    async def test_max_concurrency_is_explained(self):
        from discord.ext import commands

        error = commands.MaxConcurrencyReached(1, commands.BucketType.user)
        handled, ctx = await self._handle(error)

        self.assertTrue(handled)
        self.assertIn("still being processed", ctx.sent[0]["embed"].description)

    async def test_unrelated_errors_are_left_alone(self):
        handled, ctx = await self._handle(ValueError("something else"))
        self.assertFalse(handled)
        self.assertEqual(ctx.sent, [])

    def test_expensive_commands_are_throttled(self):
        """Guard against the decorators being dropped in a future edit."""
        import asyncio as _asyncio
        from music_player.player import Player

        async def build():
            bot = MagicMock()
            bot.user = MagicMock()
            cog = Player(bot, MusicState(), FakeYouTube())
            return {c.name: c for c in cog.get_commands()}

        cmds = _asyncio.run(build())

        add = cmds["add"]
        self.assertIsNotNone(add._max_concurrency, "?add needs per-user concurrency")
        self.assertIsNotNone(add._buckets._cooldown, "?add needs a cooldown")
        self.assertIsNotNone(cmds["queue"]._buckets._cooldown)


class TestSharedWork(unittest.IsolatedAsyncioTestCase):
    """N users wanting the same thing must cost one extraction, not N."""

    def setUp(self):
        from music_player.ytdl import YouTubeService

        self.calls = []
        self.original = YouTubeService._extract

    def tearDown(self):
        from music_player.ytdl import YouTubeService

        YouTubeService._extract = self.original

    def _patch(self, delay=0.0, ttl=21600):
        import time as _time
        from music_player.ytdl import YouTubeService

        calls = self.calls

        def fake(url, opts):
            calls.append(url)
            if delay:
                _time.sleep(delay)
            return {
                "title": "S",
                "url": f"https://gv/x?expire={int(_time.time() + ttl)}",
                "duration": 10,
                "thumbnail": None,
            }

        YouTubeService._extract = staticmethod(fake)

    def _service(self):
        from music_player.ytdl import YouTubeService

        return YouTubeService()

    async def test_concurrent_fetches_share_one_extraction(self):
        self._patch(delay=0.1)
        yt = self._service()
        await asyncio.gather(*[yt.fetch(WATCH) for _ in range(8)])
        self.assertEqual(len(self.calls), 1)

    async def test_concurrent_stream_resolves_share_one_extraction(self):
        """Several guilds queueing the same popular song."""
        self._patch(delay=0.1)
        yt = self._service()
        await asyncio.gather(*[yt.resolve_stream(WATCH) for _ in range(6)])
        self.assertEqual(len(self.calls), 1)

    async def test_stream_url_is_cached(self):
        self._patch()
        yt = self._service()
        await yt.resolve_stream(WATCH)
        await yt.resolve_stream(WATCH)
        self.assertEqual(len(self.calls), 1)

    async def test_near_expiry_stream_is_not_reused(self):
        """A link that dies in 60s must never start a new track."""
        self._patch(ttl=60)
        yt = self._service()
        await yt.resolve_stream(WATCH)
        await yt.resolve_stream(WATCH)
        self.assertEqual(len(self.calls), 2)

    async def test_prefetch_makes_the_next_track_instant(self):
        self._patch(delay=0.15)
        yt = self._service()

        yt.prefetch_stream(WATCH)
        await asyncio.sleep(0.3)  # the current song is playing

        start = time.perf_counter()
        await yt.resolve_stream(WATCH)
        elapsed = time.perf_counter() - start

        self.assertLess(elapsed, 0.01, "handover should be instant")
        self.assertEqual(len(self.calls), 1)

    async def test_prefetch_failure_is_silent(self):
        from music_player.ytdl import YouTubeService

        def boom(url, opts):
            raise RuntimeError("network down")

        YouTubeService._extract = staticmethod(boom)
        yt = self._service()
        yt.prefetch_stream(WATCH)
        await asyncio.sleep(0.05)  # must not raise or warn

    def test_expiry_is_parsed_from_the_url(self):
        from music_player.ytdl import _stream_expiry

        self.assertEqual(_stream_expiry("https://gv/x?expire=1785680360"), 1785680360.0)

    def test_expiry_falls_back_when_absent(self):
        from music_player.ytdl import _stream_expiry

        self.assertGreater(_stream_expiry("https://gv/x"), time.time())


class TestThinking(unittest.IsolatedAsyncioTestCase):
    """The 'working on it' hint must never sit in front of the response."""

    class _Channel:
        def __init__(self, latency=0.05):
            self.latency = latency
            self.started = 0
            self.completed = 0

        async def typing(self):
            self.started += 1
            await asyncio.sleep(self.latency)
            self.completed += 1

    class _PrefixCtx:
        interaction = None

        def __init__(self, channel):
            self.channel = channel

    class _SlashCtx:
        def __init__(self):
            self.interaction = object()
            self.deferred = False
            self.channel = None

        def typing(ctx_self):
            async def defer():
                ctx_self.deferred = True

            class _Awaitable:
                def __await__(self):
                    return defer().__await__()

            return _Awaitable()

    async def test_prefix_command_does_not_wait_on_the_typing_call(self):
        channel = self._Channel(latency=0.05)
        ctx = self._PrefixCtx(channel)

        start = time.perf_counter()
        async with ui.thinking(ctx):
            pass
        elapsed = time.perf_counter() - start

        self.assertLess(elapsed, 0.02, "typing REST call blocked the command body")

    async def test_instant_command_aborts_the_typing_request(self):
        channel = self._Channel(latency=0.05)
        ctx = self._PrefixCtx(channel)

        async with ui.thinking(ctx):
            pass
        await asyncio.sleep(0.08)

        self.assertEqual(channel.completed, 0, "request should be cancelled in flight")

    async def test_slow_command_still_shows_the_indicator(self):
        channel = self._Channel(latency=0.01)
        ctx = self._PrefixCtx(channel)

        async with ui.thinking(ctx):
            await asyncio.sleep(0.05)

        self.assertGreaterEqual(channel.completed, 1)

    async def test_slash_command_is_deferred(self):
        """An interaction must be acknowledged inside Discord's 3s window."""
        ctx = self._SlashCtx()
        async with ui.thinking(ctx):
            pass
        self.assertTrue(ctx.deferred)

    async def test_typing_failure_does_not_break_the_command(self):
        class Broken:
            async def typing(self):
                raise discord.HTTPException(MagicMock(status=500), "boom")

        class Ctx:
            interaction = None
            channel = Broken()

        async with ui.thinking(Ctx()):
            result = "work still ran"
        await asyncio.sleep(0.01)
        self.assertEqual(result, "work still ran")


class SlowYouTube(FakeYouTube):
    """Simulates network latency so races have a window to occur in."""

    def __init__(self, delay=0.2):
        self.delay = delay

    async def resolve_stream(self, url):
        await asyncio.sleep(self.delay)
        return await super().resolve_stream(url)


class TestConcurrency(unittest.IsolatedAsyncioTestCase):
    """Guilds run in parallel; a single guild's audio output stays serialised."""

    def _player(self, youtube=None):
        from unittest.mock import MagicMock
        from music_player.player import Player

        bot = MagicMock()
        bot.user = MagicMock()
        player = Player(bot, MusicState(), youtube or SlowYouTube())
        player.ffmpeg_path = "ffmpeg"
        return player

    @staticmethod
    def _state(guild_id=1, songs=1):
        state = GuildState(guild_id)
        state.voice = FakeVoice()
        for i in range(songs):
            state.queue.append(Track(f"{WATCH}&i={i}", f"Song {i}", 10, 42, "t"))
        return state

    @staticmethod
    def _patched():
        from unittest.mock import MagicMock, patch
        import music_player.player as mp

        return (
            patch.object(mp.discord, "FFmpegPCMAudio", MagicMock()),
            patch.object(mp.discord, "PCMVolumeTransformer", MagicMock()),
        )

    async def test_two_users_playing_at_once_start_one_track(self):
        """Both callers pass the is-playing check before the stream resolves."""
        player = self._player()
        state = self._state()
        c1, c2 = FakeContext(FakeChannel()), FakeContext(FakeChannel())

        p1, p2 = self._patched()
        with p1, p2:
            await asyncio.gather(
                player._play_current(c1, state, forced=False),
                player._play_current(c2, state, forced=False),
            )

        self.assertEqual(state.voice.play_calls, 1, "must not double-play")
        self.assertEqual(
            len(c1.sent) + len(c2.sent), 1, "only one 'now playing' announcement"
        )

    async def test_separate_guilds_do_not_block_each_other(self):
        player = self._player(SlowYouTube(delay=0.2))
        states = [self._state(guild_id=g) for g in range(1, 9)]
        ctxs = [FakeContext(FakeChannel()) for _ in states]

        p1, p2 = self._patched()
        start = time.perf_counter()
        with p1, p2:
            await asyncio.gather(
                *[
                    player._play_current(c, s, forced=False)
                    for c, s in zip(ctxs, states)
                ]
            )
        elapsed = time.perf_counter() - start

        self.assertTrue(all(s.voice.play_calls == 1 for s in states))
        # Sequential would be 8 * 0.2 = 1.6s; parallel is bounded by one call.
        self.assertLess(elapsed, 0.8, f"guilds serialised: took {elapsed:.2f}s")

    async def test_unavailable_track_skips_to_the_next_one(self):
        """Regression: a playlist stopped dead at its first dead video.

        The recursive _advance ran inside the try block, so `starting` was
        still set when _play_current re-entered and it bailed out silently.
        """
        from music_player.ytdl import ExtractionError, StreamInfo

        class PartlyDead(FakeYouTube):
            async def resolve_stream(self, url):
                await asyncio.sleep(0.01)
                if "&i=1" in url:
                    raise ExtractionError("Video unavailable")
                return StreamInfo("https://s", "Song", 10, None)

        player = self._player(PartlyDead())
        state = self._state(songs=5)
        state.queue.pop(0)  # "Song 0" finished; the dead "Song 1" is now head
        channel = FakeChannel()

        p1, p2 = self._patched()
        with p1, p2:
            await player._play_current(channel, state, forced=False)

        self.assertEqual(state.voice.play_calls, 1, "playback must continue")
        self.assertEqual(state.current.title, "Song 2")
        self.assertFalse(state.starting)

    async def test_consecutive_dead_tracks_are_all_skipped(self):
        from music_player.ytdl import ExtractionError, StreamInfo

        class MostlyDead(FakeYouTube):
            async def resolve_stream(self, url):
                await asyncio.sleep(0.01)
                if any(f"&i={i}" in url for i in (0, 1, 2)):
                    raise ExtractionError("Video unavailable")
                return StreamInfo("https://s", "Song", 10, None)

        player = self._player(MostlyDead())
        state = self._state(songs=6)
        channel = FakeChannel()

        p1, p2 = self._patched()
        with p1, p2:
            await player._play_current(channel, state, forced=False)

        self.assertEqual(state.voice.play_calls, 1)
        self.assertEqual(state.current.title, "Song 3", "must skip all three")
        self.assertFalse(state.starting)

    async def test_starting_flag_is_released_on_failure(self):
        """A failed resolve must not wedge the guild permanently."""

        class Boom(FakeYouTube):
            async def resolve_stream(self, url):
                raise RuntimeError("network down")

        player = self._player(Boom())
        state = self._state()
        ctx = FakeContext(FakeChannel())

        p1, p2 = self._patched()
        with p1, p2:
            await player._play_current(ctx, state, forced=False)

        self.assertFalse(state.starting, "flag must be cleared in finally")

    async def test_playback_aborts_if_queue_changed_while_resolving(self):
        """?clear or ?skip during the resolve must not play a stale track."""
        player = self._player(SlowYouTube(delay=0.2))
        state = self._state(songs=2)
        ctx = FakeContext(FakeChannel())

        p1, p2 = self._patched()
        with p1, p2:
            task = asyncio.create_task(player._play_current(ctx, state, forced=False))
            await asyncio.sleep(0.05)
            state.queue.clear()  # user ran ?stop / ?clear mid-resolve
            responded = await task

        self.assertFalse(responded)
        self.assertEqual(state.voice.play_calls, 0, "stale track must not play")

    async def test_simultaneous_skips_advance_one_song(self):
        """Two users pressing ?skip together must not jump two tracks."""
        player = self._player()
        state = self._state(songs=5)
        head = state.current
        channel = FakeChannel()

        p1, p2 = self._patched()
        with p1, p2:
            await asyncio.gather(
                *[
                    player._advance(channel, state, forced=True, expect=head)
                    for _ in range(5)
                ]
            )

        self.assertEqual(state.current.title, "Song 1")
        self.assertEqual(len(state.queue), 4)

    async def test_skip_racing_a_natural_track_end(self):
        """?skip and the after-callback both fire for the same finished track."""
        player = self._player()
        state = self._state(songs=5)
        head = state.current
        channel = FakeChannel()

        p1, p2 = self._patched()
        with p1, p2:
            await asyncio.gather(
                player._advance(channel, state, forced=True, expect=head),
                player._advance(channel, state, forced=False, expect=head),
            )

        self.assertEqual(len(state.queue), 4, "one advance, not two")

    async def test_sequential_skips_still_advance_each_time(self):
        """The guard must not break ordinary repeated skipping."""
        player = self._player()
        state = self._state(songs=5)
        channel = FakeChannel()

        p1, p2 = self._patched()
        with p1, p2:
            for _ in range(3):
                await player._advance(
                    channel, state, forced=True, expect=state.current
                )

        self.assertEqual(len(state.queue), 2)

    async def test_concurrent_adds_keep_every_track(self):
        """Queue mutation from several users must not lose entries."""
        state = GuildState(1)

        async def add(n):
            await asyncio.sleep(0)
            state.queue.extend(
                Track(f"{WATCH}#{n}-{i}", f"S{n}-{i}", 10, n, "u") for i in range(5)
            )

        await asyncio.gather(*[add(n) for n in range(10)])
        self.assertEqual(len(state.queue), 50)
        self.assertEqual(len({t.url for t in state.queue}), 50)


class TestSilentSkip(unittest.IsolatedAsyncioTestCase):
    """A track announced as now-playing that produces no audio.

    ffmpeg exits 0 when googlevideo answers a stream URL with 403, so the voice
    after-callback saw an ordinary completion and advanced. The song was
    announced, played silence, and vanished with nothing in the log.
    """

    def _player(self, youtube=None):
        from unittest.mock import MagicMock
        from music_player.player import Player

        bot = MagicMock()
        bot.user = MagicMock()
        bot.loop = asyncio.get_running_loop()
        player = Player(bot, MusicState(), youtube or FakeYouTube())
        player.ffmpeg_path = "ffmpeg"
        return player

    @staticmethod
    def _state(songs=3, duration=240):
        state = GuildState(1)
        state.voice = FakeVoice()
        for i in range(songs):
            state.queue.append(
                Track(f"{WATCH}&i={i}", f"Song {i}", duration, 42, "t")
            )
        return state

    @staticmethod
    def _patched():
        from unittest.mock import MagicMock, patch
        import music_player.player as mp

        return (
            patch.object(mp.discord, "FFmpegPCMAudio", MagicMock()),
            patch.object(mp.discord, "PCMVolumeTransformer", MagicMock()),
        )

    # -- detection ------------------------------------------------------

    def test_instant_end_of_a_long_track_is_a_failure(self):
        from music_player.player import Player

        state = self._state()
        self.assertTrue(Player._ended_early(state, state.current, 0.2))

    def test_a_finished_track_is_not_a_failure(self):
        from music_player.player import Player

        state = self._state()
        self.assertFalse(Player._ended_early(state, state.current, 240.0))

    def test_a_user_skip_is_not_a_failure(self):
        """?skip ends a track early on purpose; retrying would fight the user."""
        from music_player.player import Player

        state = self._state()
        state.skip_requested = True
        self.assertFalse(Player._ended_early(state, state.current, 0.2))

    def test_pause_is_not_a_failure(self):
        from music_player.player import Player

        state = self._state()
        state.suppress_advance = True
        self.assertFalse(Player._ended_early(state, state.current, 0.2))

    def test_very_short_tracks_are_not_judged(self):
        """A 3s clip legitimately ends in about 3s."""
        from music_player.player import Player

        state = self._state(duration=3)
        self.assertFalse(Player._ended_early(state, state.current, 0.2))

    # -- recovery -------------------------------------------------------

    async def test_a_silent_track_is_retried_on_a_fresh_url(self):
        player = self._player()
        state = self._state()
        channel = FakeChannel()
        head = state.current

        p1, p2 = self._patched()
        with p1, p2:
            state.playback_started = time.monotonic()  # "played" ~0s
            player._on_track_end(None, channel, state, head)
            await asyncio.sleep(0.05)

        self.assertEqual(player.youtube.invalidated, [head.url],
                         "the dead URL must not be replayed from cache")
        self.assertIs(state.current, head, "the track must not be skipped")
        self.assertEqual(state.voice.play_calls, 1, "it must be restarted")

    async def test_a_second_failure_gives_up_and_says_so(self):
        player = self._player()
        state = self._state()
        channel = FakeChannel()
        head = state.current
        state.retried_url = head.url  # the retry already happened

        p1, p2 = self._patched()
        with p1, p2:
            state.playback_started = time.monotonic()
            player._on_track_end(None, channel, state, head)
            await asyncio.sleep(0.05)

        self.assertIsNot(state.current, head, "must move on after two attempts")
        self.assertTrue(channel.sent, "the user must be told, not left guessing")

    async def test_a_track_that_played_advances_normally(self):
        player = self._player()
        state = self._state()
        channel = FakeChannel()
        head = state.current

        p1, p2 = self._patched()
        with p1, p2:
            state.playback_started = time.monotonic() - 240
            player._on_track_end(None, channel, state, head)
            await asyncio.sleep(0.05)

        self.assertIsNot(state.current, head)
        self.assertEqual(player.youtube.invalidated, [], "nothing was wrong")

    async def test_the_retry_marker_clears_between_tracks(self):
        """Otherwise one bad song would spend the next song's retry."""
        player = self._player()
        state = self._state()
        state.retried_url = state.current.url
        channel = FakeChannel()

        p1, p2 = self._patched()
        with p1, p2:
            await player._advance(channel, state, forced=True)

        self.assertIsNone(state.retried_url)


class TestUnavailableCount(unittest.TestCase):
    """A playlist's dead entries are dropped; the user must hear about it."""

    def test_fetch_result_counts_dropped_entries(self):
        from music_player.ytdl import FetchResult

        self.assertEqual(FetchResult(entries=[]).unavailable, 0)
        self.assertEqual(
            FetchResult(entries=[], playlist_title="p", unavailable=39).unavailable, 39
        )

    def test_added_embed_reports_unavailable(self):
        from music_player import ui

        track = Track(WATCH, "Song", 10, 42, "t")
        body = ui.added(track, extra_count=159, unavailable=39).description
        self.assertIn("159", body)
        self.assertIn("39", body)

    def test_added_embed_is_unchanged_when_nothing_was_dropped(self):
        from music_player import ui

        track = Track(WATCH, "Song", 10, 42, "t")
        self.assertNotIn("unavailable", ui.added(track, extra_count=5).description)


if __name__ == "__main__":
    unittest.main(verbosity=2)

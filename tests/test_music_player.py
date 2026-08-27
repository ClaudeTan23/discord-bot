"""Unit tests for the pure logic in the music player.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Importing app configures logging for real, and config reads LOG_DIR at import
# time - so this has to be set before any music_player import below. Without it
# the suite writes into the project's own logs/ tree.
os.environ.setdefault(
    "LOG_DIR", tempfile.mkdtemp(prefix="music-player-test-logs-")
)

# Several tests deliberately trigger failure paths; keep their logging out of
# the test output.
logging.disable(logging.CRITICAL)

import discord  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

from music_player.ui import embeds as ui  # noqa: E402
from music_player.audio import (  # noqa: E402
    FRAME_SIZE,
    BufferedAudioSource,
)
from music_player.state import GuildState, MusicState, Track  # noqa: E402
from music_player.services.youtube import (  # noqa: E402
    ExtractionError,
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
        self.assertTrue(embed.footer.text.startswith("Page 1/19"))

    def test_last_page_lists_remainder(self):
        embed = ui.queue_page(self._tracks(183), 19)
        self.assertEqual(len(embed.description.splitlines()), 3)
        self.assertTrue(embed.footer.text.startswith("Page 19/19"))

    def test_page_numbering_is_absolute(self):
        embed = ui.queue_page(self._tracks(183), 2)
        self.assertTrue(embed.description.startswith("`11.`"))

    def test_footer_summarises_the_whole_queue(self):
        """The page footer answers "how much is in here" without paging."""
        embed = ui.queue_page(self._tracks(183), 1)
        self.assertIn("183 songs", embed.footer.text)
        self.assertIn("total", embed.footer.text)

    def test_single_song_footer_is_not_pluralised(self):
        self.assertIn("1 song ", ui.queue_page(self._tracks(1), 1).footer.text)

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

    def test_single_track_shows_the_title_and_links_out(self):
        embed = ui.added(self._track())
        self.assertEqual(embed.author.name, "Added to the queue")
        self.assertEqual(embed.title, "Song")
        self.assertEqual(embed.url, WATCH)

    def test_playlist_counts_every_song_including_the_first(self):
        """"and 5 more" is ambiguous; "6 songs" is not."""
        embed = ui.added(self._track(), extra_count=5)
        self.assertEqual(embed.author.name, "Added 6 songs to the queue")

    def test_duration_is_shown(self):
        self.assertIn("0:10", ui.added(self._track()).description)


class TestAddedPlaylist(unittest.TestCase):
    """A playlist is named by its own title, not by whichever song is first."""

    PLIST = "https://www.youtube.com/playlist?list=PLabc"

    def _track(self):
        return Track(WATCH, "First Song", 213, 1, "u")

    def _embed(self, **kw):
        base = dict(
            playlist_title="Rick Astley - The Best Of",
            playlist_url=self.PLIST,
            total_seconds=33154,
        )
        base.update(kw)
        return ui.added(self._track(), 159, **base)

    def test_playlist_title_is_the_headline(self):
        embed = self._embed()
        self.assertEqual(embed.title, "Rick Astley - The Best Of")
        self.assertEqual(embed.url, self.PLIST)

    def test_total_running_time_is_shown(self):
        """160 songs is meaningless until you know if it's 40 min or 9 hours."""
        self.assertIn("9 hr 12 min", self._embed().description)

    def test_song_count_is_shown_with_the_duration(self):
        self.assertIn("**160 songs**", self._embed().description)

    def test_the_first_song_is_still_linked(self):
        field = next(f for f in self._embed().fields if f.name == "First up")
        self.assertIn(f"](<{WATCH}>)", field.value)
        self.assertIn("3:33", field.value)

    def test_an_untitled_playlist_falls_back_to_the_first_song(self):
        embed = self._embed(playlist_title="", playlist_url=None)
        self.assertEqual(embed.title, "First Song")
        self.assertEqual(embed.url, WATCH)

    def test_a_single_track_has_no_first_up_field(self):
        self.assertEqual(ui.added(self._track()).fields, [])


class TestAddedLandingNote(unittest.TestCase):
    """"Added" alone leaves open the question of when it will be heard."""

    def _track(self):
        return Track(WATCH, "Song", 213, 1, "u")

    def _footer(self, **kw):
        return ui.added(self._track(), **kw).footer.text

    def test_position_and_countdown_while_playing(self):
        self.assertEqual(
            self._footer(position=5, starts_in=740),
            "#5 in queue · about 12 min away",
        )

    def test_no_countdown_when_nothing_is_playing(self):
        """An idle queue isn't advancing, so a countdown would be a guess."""
        self.assertEqual(self._footer(position=5), "#5 in queue")

    def test_the_first_song_added_to_an_idle_bot_says_how_to_start(self):
        self.assertIn("?play", self._footer(position=1))

    def test_next_in_line_says_so_rather_than_zero_minutes(self):
        self.assertEqual(
            self._footer(position=2, starts_in=0), "#2 in queue · plays next"
        )

    def test_no_footer_when_position_is_unknown(self):
        self.assertIsNone(ui.added(self._track()).footer.text)


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
        from music_player.services.youtube import YouTubeService

        self.calls = []
        self.original = YouTubeService._extract

    def tearDown(self):
        from music_player.services.youtube import YouTubeService

        YouTubeService._extract = self.original

    def _patch(self, delay=0.0, result=None):
        import time as _time
        from music_player.services.youtube import YouTubeService

        calls = self.calls

        def fake(url, opts):
            calls.append((url, opts))
            if delay:
                _time.sleep(delay)
            return result if result is not None else {"title": "Fake Song"}

        YouTubeService._extract = staticmethod(fake)

    async def test_partial_input_never_touches_the_network(self):
        from music_player.services.youtube import YouTubeService

        self._patch()
        yt = YouTubeService()
        for text in ("h", "https://www.you", "https://www.youtube.com/watch?v=dQw4"):
            self.assertIsNone(await yt.preview(text))
        self.assertEqual(self.calls, [], "no extraction should have run")

    async def test_concurrent_keystrokes_share_one_extraction(self):
        from music_player.services.youtube import YouTubeService

        self._patch(delay=0.2)
        yt = YouTubeService()
        results = await asyncio.gather(*[yt.preview(WATCH) for _ in range(10)])
        self.assertEqual(len(self.calls), 1, "10 keystrokes must not mean 10 lookups")
        self.assertTrue(all(r == "Fake Song" for r in results))

    async def test_repeat_lookups_are_cached(self):
        from music_player.services.youtube import YouTubeService

        self._patch()
        yt = YouTubeService()
        for _ in range(5):
            await yt.preview(WATCH)
        self.assertEqual(len(self.calls), 1)

    async def test_timeout_returns_none_but_keeps_working(self):
        """A slow lookup must not stall the response past Discord's window."""
        from music_player.services.youtube import YouTubeService

        self._patch(delay=0.4)
        yt = YouTubeService()

        self.assertIsNone(await yt.preview(WATCH, timeout=0.05))
        await asyncio.sleep(0.6)
        # The shielded task finished in the background and warmed the cache.
        self.assertEqual(await yt.preview(WATCH, timeout=0.05), "Fake Song")
        self.assertEqual(len(self.calls), 1)

    async def test_playlist_preview_stops_after_first_entry(self):
        from music_player.services.youtube import YouTubeService

        self._patch(result={"title": "Popular Music Videos"})
        yt = YouTubeService()
        label = await yt.preview(f"https://www.youtube.com/playlist?list={LIST}")
        self.assertEqual(label, "Popular Music Videos")
        _, opts = self.calls[0]
        self.assertEqual(opts.get("playlist_items"), "1")

    async def test_extraction_failure_is_swallowed(self):
        from music_player.services.youtube import YouTubeService

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
        self.channel = type("Ch", (), {"id": 999, "name": "General"})()

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
        from music_player.services.youtube import StreamInfo

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
        from music_player.cogs.player import Player

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
        import music_player.cogs.player as mp

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
        import music_player.cogs.player as mp

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
        import music_player.cogs.player as mp

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
        from music_player.cogs.player import Player

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
        from music_player.cogs.player import Player

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
        from music_player.services.youtube import YouTubeService

        self.calls = []
        self.original = YouTubeService._extract

    def tearDown(self):
        from music_player.services.youtube import YouTubeService

        YouTubeService._extract = self.original

    def _patch(self, delay=0.0, ttl=21600):
        import time as _time
        from music_player.services.youtube import YouTubeService

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
        from music_player.services.youtube import YouTubeService

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
        from music_player.services.youtube import YouTubeService

        def boom(url, opts):
            raise RuntimeError("network down")

        YouTubeService._extract = staticmethod(boom)
        yt = self._service()
        yt.prefetch_stream(WATCH)
        await asyncio.sleep(0.05)  # must not raise or warn

    def test_expiry_is_parsed_from_the_url(self):
        from music_player.services.youtube import _stream_expiry

        self.assertEqual(_stream_expiry("https://gv/x?expire=1785680360"), 1785680360.0)

    def test_expiry_falls_back_when_absent(self):
        from music_player.services.youtube import _stream_expiry

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
        from music_player.cogs.player import Player

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
        import music_player.cogs.player as mp

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
        from music_player.services.youtube import ExtractionError, StreamInfo

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
        from music_player.services.youtube import ExtractionError, StreamInfo

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
        from music_player.cogs.player import Player

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
        import music_player.cogs.player as mp

        return (
            patch.object(mp.discord, "FFmpegPCMAudio", MagicMock()),
            patch.object(mp.discord, "PCMVolumeTransformer", MagicMock()),
        )

    # -- detection ------------------------------------------------------

    def test_instant_end_of_a_long_track_is_a_failure(self):
        from music_player.cogs.player import Player

        state = self._state()
        self.assertTrue(Player._ended_early(state, state.current, 0.2))

    def test_a_finished_track_is_not_a_failure(self):
        from music_player.cogs.player import Player

        state = self._state()
        self.assertFalse(Player._ended_early(state, state.current, 240.0))

    def test_a_user_skip_is_not_a_failure(self):
        """?skip ends a track early on purpose; retrying would fight the user."""
        from music_player.cogs.player import Player

        state = self._state()
        state.skip_requested = True
        self.assertFalse(Player._ended_early(state, state.current, 0.2))

    def test_pause_is_not_a_failure(self):
        from music_player.cogs.player import Player

        state = self._state()
        state.suppress_advance = True
        self.assertFalse(Player._ended_early(state, state.current, 0.2))

    def test_very_short_tracks_are_not_judged(self):
        """A 3s clip legitimately ends in about 3s."""
        from music_player.cogs.player import Player

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
        from music_player.services.youtube import FetchResult

        self.assertEqual(FetchResult(entries=[]).unavailable, 0)
        self.assertEqual(
            FetchResult(entries=[], playlist_title="p", unavailable=39).unavailable, 39
        )

    def test_added_embed_reports_unavailable(self):
        from music_player.ui import embeds as ui

        track = Track(WATCH, "Song", 10, 42, "t")
        embed = ui.added(track, extra_count=159, unavailable=39)
        self.assertIn("160", embed.author.name)
        # Discord subtext: present, but not competing with what was added.
        self.assertIn("-# Skipped 39 unavailable videos", embed.description)

    def test_a_single_dropped_video_is_not_pluralised(self):
        from music_player.ui import embeds as ui

        track = Track(WATCH, "Song", 10, 42, "t")
        body = ui.added(track, extra_count=5, unavailable=1).description
        self.assertIn("1 unavailable video —", body)

    def test_added_embed_is_unchanged_when_nothing_was_dropped(self):
        from music_player.ui import embeds as ui

        track = Track(WATCH, "Song", 10, 42, "t")
        self.assertNotIn("-#", ui.added(track, extra_count=5).description)


class _FakeSource(discord.AudioSource):
    """A PCM source whose reads can be stalled on demand."""

    def __init__(self, frames: int, *, stall: float = 0.0, stall_at: int = -1):
        self._remaining = frames
        self._stall = stall
        self._stall_at = stall_at
        self._served = 0
        self.cleaned = False

    def is_opus(self) -> bool:
        return False

    def read(self) -> bytes:
        if self._remaining <= 0:
            return b""
        if self._served == self._stall_at:
            time.sleep(self._stall)
        self._remaining -= 1
        self._served += 1
        return bytes([self._served % 251]) * FRAME_SIZE

    def cleanup(self) -> None:
        self.cleaned = True


class TestBufferedAudioSource(unittest.IsolatedAsyncioTestCase):
    """The buffer exists so discord.py's player never reads late.

    Its loop skips the 20ms sleep for every deadline that has already passed,
    so a blocked read is paid back as a burst of packets - audio that speeds up
    and stutters. read() must therefore always return immediately.
    """

    async def test_frames_pass_through_in_order(self):
        source = _FakeSource(10)
        buf = BufferedAudioSource(source, buffer_seconds=1, prefill_seconds=0.1)
        await buf.wait_until_ready(timeout=2)

        read = []
        while (frame := buf.read()):
            read.append(frame)

        self.assertEqual(len(read), 10)
        self.assertEqual(read, [bytes([i % 251]) * FRAME_SIZE for i in range(1, 11)])
        buf.cleanup()
        self.assertTrue(source.cleaned)

    async def test_read_never_blocks_while_the_source_stalls(self):
        # One frame buffered, then a stall far longer than a 20ms deadline.
        source = _FakeSource(4, stall=0.4, stall_at=1)
        buf = BufferedAudioSource(source, buffer_seconds=1, prefill_seconds=0.02)
        await buf.wait_until_ready(timeout=2)

        started = time.monotonic()
        for _ in range(5):
            self.assertEqual(len(buf.read()), FRAME_SIZE)
        elapsed = time.monotonic() - started

        # Five reads across a 400ms stall, none of which waited for it.
        self.assertLess(elapsed, 0.1)
        self.assertGreater(buf.underruns, 0)
        buf.cleanup()

    async def test_underrun_is_silence_not_a_dropped_frame(self):
        source = _FakeSource(2, stall=0.3, stall_at=1)
        buf = BufferedAudioSource(source, buffer_seconds=1, prefill_seconds=0.02)
        await buf.wait_until_ready(timeout=2)

        first = buf.read()
        padding = buf.read()
        self.assertEqual(padding, b"\x00" * FRAME_SIZE)

        # The stalled frame is still delivered afterwards - delayed, not lost.
        # Read on the player's own 20ms cadence rather than spinning, so the
        # starvation cut-off is not reached in a few microseconds.
        frame = b"\x00" * FRAME_SIZE
        for _ in range(50):
            frame = buf.read()
            if frame != b"\x00" * FRAME_SIZE:
                break
            time.sleep(0.02)

        self.assertEqual(len(frame), FRAME_SIZE)
        self.assertNotEqual(frame, first)
        buf.cleanup()

    async def test_exhausted_source_ends_the_track(self):
        buf = BufferedAudioSource(_FakeSource(2), buffer_seconds=1, prefill_seconds=1)
        # Prefill can never be reached, but the source ending must still release
        # the wait rather than burning the whole timeout.
        started = time.monotonic()
        await buf.wait_until_ready(timeout=5)
        self.assertLess(time.monotonic() - started, 2)

        self.assertEqual(len(buf.read()), FRAME_SIZE)
        self.assertEqual(len(buf.read()), FRAME_SIZE)
        self.assertEqual(buf.read(), b"")
        buf.cleanup()

    async def test_cleanup_releases_a_producer_blocked_on_a_full_buffer(self):
        # Capacity is one second; the source has far more than that to give.
        source = _FakeSource(10_000)
        buf = BufferedAudioSource(source, buffer_seconds=1, prefill_seconds=0.1)
        await buf.wait_until_ready(timeout=2)

        buf.cleanup()
        self.assertTrue(source.cleaned)
        self.assertFalse(buf._thread.is_alive())

    def test_is_opus_is_forwarded(self):
        # PCMVolumeTransformer refuses an opus source, so this must not lie.
        buf = BufferedAudioSource(_FakeSource(0))
        self.assertFalse(buf.is_opus())
        discord.PCMVolumeTransformer(buf, volume=0.5)
        buf.cleanup()


class TestHelpManual(unittest.TestCase):
    """?help must reflect edits to help.txt without restarting the bot."""

    def setUp(self):
        import tempfile

        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "help.txt"

    def _touch(self, text: str) -> None:
        # Timestamps have coarse resolution on some filesystems, so change the
        # size too - the stamp is (mtime, size) precisely for this reason.
        self.path.write_text(text, encoding="utf-8")

    def test_reloads_after_the_file_changes(self):
        from music_player.ui.manual import HelpManual

        self._touch("first version")
        manual = HelpManual(self.path)
        self.assertEqual(manual.text(), "first version")

        self._touch("second version, longer")
        self.assertEqual(manual.text(), "second version, longer")

    def test_unchanged_file_is_not_read_again(self):
        from music_player.ui.manual import HelpManual

        self._touch("stable text")
        manual = HelpManual(self.path)

        calls = []
        original = Path.read_text

        def counted(self_, *a, **kw):
            calls.append(self_)
            return original(self_, *a, **kw)

        with patch.object(Path, "read_text", counted):
            for _ in range(5):
                self.assertEqual(manual.text(), "stable text")
        self.assertEqual(calls, [], "a stat() should be enough when nothing changed")

    def test_missing_file_falls_back_and_recovers(self):
        from music_player.ui.manual import HelpManual

        manual = HelpManual(self.path)  # never created
        self.assertEqual(manual.text(), HelpManual.FALLBACK)

        self._touch("now it exists")
        self.assertEqual(manual.text(), "now it exists")

    def test_blank_save_keeps_the_previous_text(self):
        from music_player.ui.manual import HelpManual

        self._touch("real content")
        manual = HelpManual(self.path)

        self._touch("   \n  ")  # caught mid-edit
        self.assertEqual(manual.text(), "real content")

    def test_every_shipped_section_fits_in_an_embed(self):
        """The 4096-char ceiling is per *page*, not per file.

        The manual is browsable, so each section is its own embed and only the
        landing page carries the intro as well. Asserting the whole file fits
        one embed was the pre-browsing constraint; it now fails the moment the
        manual grows past what any single page would ever show.
        """
        from music_player.config import HELP_FILE
        from music_player.ui.manual import build_embed, parse

        text = HELP_FILE.read_text(encoding="utf-8")
        self.assertTrue(text.strip())

        intro, sections = parse(text)
        for index, section in enumerate(sections):
            rendered = build_embed(section, intro=intro if index == 0 else "")
            self.assertLessEqual(
                len(rendered.description),
                4096,
                f"the {section.name!r} page is past what Discord will show",
            )
            # build_embed clips rather than raising, so a page at exactly the
            # limit has already lost its tail.
            self.assertFalse(
                rendered.description.endswith("…"),
                f"the {section.name!r} page was truncated",
            )

    def test_the_shipped_manual_has_exactly_the_pages_we_expect(self):
        """A bold-only line becomes a dropdown entry, which is easy to do by
        accident: a sub-heading like ``**Every command:**`` silently splits its
        section in two, and every size check still passes. Pinning the list is
        what catches that.
        """
        from music_player.config import HELP_FILE
        from music_player.ui.manual import parse

        from music_player.ui.manual import split_icon

        _intro, sections = parse(HELP_FILE.read_text(encoding="utf-8"))
        self.assertEqual(
            [split_icon(section.name)[1] for section in sections],
            [
                "Getting started",
                "Playing music",
                "Queue",
                "Playlists",
                "Buttons",
                "Notes",
            ],
        )

    def test_every_shipped_page_carries_an_icon(self):
        """The dropdown shows them beside the label, so a bare page looks
        broken next to the rest."""
        from music_player.config import HELP_FILE
        from music_player.ui.manual import parse, split_icon

        _intro, sections = parse(HELP_FILE.read_text(encoding="utf-8"))
        for section in sections:
            icon, label = split_icon(section.name)
            self.assertIsNotNone(icon, f"{section.name!r} has no icon")
            self.assertTrue(label, f"{section.name!r} is only an icon")

    def test_the_landing_page_points_at_playlists(self):
        """?help opens on the first section; a feature never named there is a
        feature most people never find."""
        from music_player.config import HELP_FILE
        from music_player.ui.manual import parse

        intro, sections = parse(HELP_FILE.read_text(encoding="utf-8"))
        landing = intro + sections[0].body
        self.assertIn("?playlist", landing)

    def test_sections_are_reparsed_on_reload(self):
        from music_player.ui.manual import HelpManual

        self._touch("intro\n\n**One**\nbody one")
        manual = HelpManual(self.path)
        self.assertEqual([s.name for s in manual.sections()], ["One"])

        self._touch("intro\n\n**One**\nbody one\n\n**Two**\nbody two")
        self.assertEqual([s.name for s in manual.sections()], ["One", "Two"])


class TestHelpParsing(unittest.TestCase):
    """help.txt drives the dropdown, so its headings must parse predictably."""

    def test_intro_and_sections_are_separated(self):
        from music_player.ui.manual import parse

        intro, sections = parse(
            "Read me first.\n\n**Playback**\n- ?play\n\n**Queue**\n- ?queue\n"
        )
        self.assertEqual(intro, "Read me first.")
        self.assertEqual([s.name for s in sections], ["Playback", "Queue"])
        self.assertEqual(sections[0].body, "- ?play")

    def test_a_line_that_merely_starts_bold_is_content(self):
        """`**?help** - display this list` is an entry, not a new category."""
        from music_player.ui.manual import parse

        _, sections = parse("**Start**\n**`?help`** — Display this list.\n")
        self.assertEqual([s.name for s in sections], ["Start"])
        self.assertIn("?help", sections[0].body)

    def test_a_file_without_headings_renders_whole(self):
        from music_player.ui.manual import parse

        intro, sections = parse("just a flat list of commands")
        self.assertEqual(intro, "")
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].body, "just a flat list of commands")

    def test_empty_sections_are_dropped(self):
        from music_player.ui.manual import parse

        _, sections = parse("**Empty**\n\n**Real**\ncontent")
        self.assertEqual([s.name for s in sections], ["Real"])

    def test_shipped_manual_parses_into_categories(self):
        from music_player.config import HELP_FILE
        from music_player.ui.manual import build_embed, parse

        intro, sections = parse(HELP_FILE.read_text(encoding="utf-8"))
        self.assertTrue(intro, "the lead paragraph should stay out of the sections")
        self.assertGreaterEqual(len(sections), 2)
        self.assertLessEqual(len(sections), 25, "Discord allows 25 select options")
        for section in sections:
            self.assertLessEqual(len(section.name), 100, section.name)
            self.assertLessEqual(len(build_embed(section).description), 4096)


class TestHelpView(unittest.IsolatedAsyncioTestCase):
    """The dropdown must answer only its owner and fail visibly, not silently."""

    MANUAL = "Lead line.\n\n**One**\nbody one\n\n**Two**\nbody two"

    def _manual(self, text=None):
        import tempfile

        from music_player.ui.manual import HelpManual

        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = Path(d.name) / "help.txt"
        path.write_text(text or self.MANUAL, encoding="utf-8")
        return HelpManual(path)

    async def test_one_option_per_section(self):
        from music_player.ui.manual import HelpView

        view = HelpView(self._manual(), user_id=7)
        select = view.children[0]
        self.assertEqual([o.label for o in select.options], ["One", "Two"])

    async def test_landing_page_carries_the_intro(self):
        from music_player.ui.manual import HelpView

        view = HelpView(self._manual(), user_id=7)
        self.assertEqual(view.landing.title, "One")
        self.assertIn("Lead line.", view.landing.description)
        self.assertIn("body one", view.landing.description)

    async def test_a_single_section_gets_no_dropdown(self):
        from music_player.ui.manual import HelpView

        view = HelpView(self._manual("flat text, no headings"), user_id=7)
        self.assertEqual(view.children, [], "nothing to choose between")

    async def test_choosing_swaps_the_page(self):
        from unittest.mock import AsyncMock

        from music_player.ui.manual import HelpView

        view = HelpView(self._manual(), user_id=7)
        # Select.values reads a ContextVar set during a real interaction and
        # falls back to _values; the fallback is what a unit test can drive.
        view.children[0]._values = ["1"]
        interaction = MagicMock()
        interaction.response.edit_message = AsyncMock()

        await view.children[0].callback(interaction)

        embed = interaction.response.edit_message.await_args.kwargs["embed"]
        self.assertEqual(embed.title, "Two")
        self.assertEqual(embed.description, "body two")
        self.assertNotIn("Lead line.", embed.description)

    async def test_another_user_is_turned_away(self):
        from unittest.mock import AsyncMock

        from music_player.ui.manual import HelpView

        view = HelpView(self._manual(), user_id=7)
        interaction = MagicMock()
        interaction.user.id = 999
        interaction.response.send_message = AsyncMock()

        self.assertFalse(await view.interaction_check(interaction))
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_owner_is_let_through(self):
        from music_player.ui.manual import HelpView

        view = HelpView(self._manual(), user_id=7)
        interaction = MagicMock()
        interaction.user.id = 7
        self.assertTrue(await view.interaction_check(interaction))

    async def test_timeout_disables_the_menu(self):
        from unittest.mock import AsyncMock

        from music_player.ui.manual import HelpView

        view = HelpView(self._manual(), user_id=7)
        view.message = MagicMock()
        view.message.edit = AsyncMock()

        await view.on_timeout()

        self.assertTrue(view.children[0].disabled)
        view.message.edit.assert_awaited_once()

    async def test_timeout_without_a_message_is_harmless(self):
        from music_player.ui.manual import HelpView

        view = HelpView(self._manual(), user_id=7)
        await view.on_timeout()  # must not raise
        self.assertTrue(view.children[0].disabled)


class TestTitleClipping(unittest.TestCase):
    """Queue rows must stay one line without emitting broken markdown."""

    def test_short_titles_are_untouched(self):
        self.assertEqual(ui.clip("Darude - Sandstorm"), "Darude - Sandstorm")

    def test_long_titles_get_an_ellipsis(self):
        clipped = ui.clip("x" * 200, 20)
        self.assertTrue(clipped.endswith("…"))

    def test_the_result_never_exceeds_the_limit(self):
        """The ellipsis counts. Discord rejects a 257-character embed title."""
        for limit in (5, 20, 45, 100, 256):
            self.assertLessEqual(len(ui.clip("x" * 900, limit)), limit, limit)
            self.assertLessEqual(len(ui.clip("[" * 900, limit)), limit, limit)

    def test_pathological_input_still_fits_discords_embed_limits(self):
        track = Track(WATCH, "T" * 900, 200, 1, "u")
        embed = ui.added(track, 5, 3, playlist_title="P" * 900, total_seconds=100)
        self.assertLessEqual(len(embed.title), 256)
        self.assertLess(len(embed.description), 4096)

        queue = ui.queue_page([track] * 10, 1, status=ui.PLAYING_MARKER)
        self.assertLess(len(queue.description), 4096)

    def test_a_cut_inside_brackets_backs_out_of_them(self):
        """"Take On Me [Remas…" would break the [label](url) it sits inside."""
        clipped = ui.clip("a-ha - Take On Me (Official Video) [Remastered in 4K]", 45)
        self.assertEqual(clipped.count("["), clipped.count("]"))
        self.assertEqual(clipped.count("("), clipped.count(")"))

    def test_a_cut_inside_parentheses_backs_out_of_them(self):
        clipped = ui.clip("Some Song (Official Music Video Extended)", 30)
        self.assertEqual(clipped.count("("), clipped.count(")"))

    def test_one_long_bracket_run_still_produces_a_label(self):
        """Backing out must never leave an empty link label."""
        clipped = ui.clip("[" + "y" * 200, 20)
        self.assertTrue(clipped.strip("…"))

    def test_clipped_links_still_point_at_the_whole_video(self):
        track = Track(WATCH, "z" * 200, 60, 1, "u")
        rendered = ui.track_link(track, limit=20)
        self.assertIn(f"](<{WATCH}>)", rendered)
        self.assertLess(len(rendered), 200)

    def test_track_link_is_verbatim_without_a_limit(self):
        title = "Finesse2Tymes - Crazy [Official Music Video]"
        self.assertIn(title, ui.track_link(Track(WATCH, title, 10, 1, "u")))


class TestEmbedsFitDiscordsLimits(unittest.TestCase):
    """Discord rejects an over-long embed outright with a 400.

    The message never arrives and the traceback only shows up in the log
    afterwards, so these are silent failures in production. Every field the
    bot fills from a title it did not write is checked here.
    """

    HUGE = "M" * 4000
    MARKDOWN = "[**`~~x~~`**](http://x) " * 200

    def _track(self, title):
        return Track("https://youtu.be/dQw4w9WgXcQ", title, 213, 1, "u")

    def test_now_playing_clips_a_monstrous_title(self):
        """It was the one builder passing the title through unclipped."""
        rendered = ui.now_playing(
            ui.NowPlaying(
                title=self.HUGE, url="https://y/1", duration=213, thumbnail=None,
                requester=None, volume=0.1, position=1, total=1, up_next=None,
                remaining=213,
            )
        )
        self.assertLessEqual(len(rendered.title), 256)

    def test_skip_confirmations_bound_their_description(self):
        """track_link() without a limit uses the title verbatim - fine inside
        a bounded row, fatal when it *is* the description."""
        track = self._track(self.MARKDOWN)
        for rendered in (ui.skipped(track), ui.jumping_to(track)):
            self.assertLessEqual(len(rendered.description), 4096)

    def test_the_whole_embed_stays_under_the_total(self):
        track = self._track(self.HUGE)
        rendered = ui.now_playing(
            ui.NowPlaying(
                title=self.HUGE, url=track.url, duration=4000,
                thumbnail=None, requester=None, volume=1.0, position=1,
                total=50, up_next=track, remaining=200000, elapsed=1.0,
                source=self.HUGE,
            )
        )
        self.assertLessEqual(len(rendered), 6000)


class TestClipKeepsMarkdownBalanced(unittest.TestCase):
    """A clipped title goes inside ``[label](url)``.

    One backup pass was not enough: a title can carry several unmatched
    openers, and backing out of one pair can remove the closer that was
    balancing the other.
    """

    @staticmethod
    def _imbalance(text, opener, closer):
        return max(0, text.count(opener) - text.count(closer))

    def _assert_balanced(self, text, limit):
        out = ui.clip(text, limit)
        for opener, closer in (("[", "]"), ("(", ")")):
            self.assertLessEqual(
                self._imbalance(out, opener, closer),
                self._imbalance(text, opener, closer),
                f"clip({text!r}, {limit}) -> {out!r}",
            )

    def test_several_unmatched_openers_are_all_backed_out_of(self):
        self._assert_balanced("[a[bcdefghijkl", 10)

    def test_fixing_one_pair_does_not_break_the_other(self):
        """Cutting back past ( took away the ] balancing an earlier [."""
        self._assert_balanced("[a](bcdefghij", 10)

    def test_a_title_that_is_nothing_but_openers(self):
        self._assert_balanced("[[[[[[[[[[[[abc", 10)
        self._assert_balanced("((((((((((((abc", 10)

    def test_a_realistic_title_cut_mid_bracket(self):
        self._assert_balanced("Song Name (Official Video) [4K Remaster]", 20)

    def test_it_still_never_exceeds_the_limit(self):
        for text in ("[a[bcdefghijkl", "[a](bcdefghij", "[[[[[abc", "plain"):
            for limit in (2, 10, 45, 256):
                self.assertLessEqual(len(ui.clip(text, limit)), limit)


class TestVideoId(unittest.TestCase):
    """Artwork is derived from the id, so a wrong id means a broken image."""

    def test_watch_url(self):
        from music_player.services.youtube import video_id
        self.assertEqual(video_id(WATCH), VIDEO)

    def test_short_link(self):
        from music_player.services.youtube import video_id
        self.assertEqual(video_id(f"https://youtu.be/{VIDEO}"), VIDEO)

    def test_shorts_and_embed_paths(self):
        from music_player.services.youtube import video_id
        for path in ("shorts", "embed", "live", "v"):
            self.assertEqual(
                video_id(f"https://www.youtube.com/{path}/{VIDEO}"), VIDEO, path
            )

    def test_music_subdomain(self):
        from music_player.services.youtube import video_id
        self.assertEqual(video_id(f"https://music.youtube.com/watch?v={VIDEO}"), VIDEO)

    def test_playlist_ride_along_is_ignored(self):
        from music_player.services.youtube import video_id
        self.assertEqual(video_id(f"{WATCH}&list={LIST}"), VIDEO)

    def test_a_wrong_length_id_is_rejected(self):
        """Better no image than a request for a video that doesn't exist."""
        from music_player.services.youtube import video_id
        self.assertIsNone(video_id("https://www.youtube.com/watch?v=short"))

    def test_non_youtube_hosts_are_rejected(self):
        from music_player.services.youtube import video_id
        self.assertIsNone(video_id(f"https://example.com/watch?v={VIDEO}"))
        self.assertIsNone(video_id(f"https://notyoutube.com/watch?v={VIDEO}"))

    def test_bare_playlist_url_has_no_video(self):
        from music_player.services.youtube import video_id
        self.assertIsNone(video_id(f"https://www.youtube.com/playlist?list={LIST}"))


class TestArtwork(unittest.TestCase):
    def test_youtube_links_get_a_thumbnail(self):
        self.assertEqual(
            ui.artwork(WATCH),
            f"https://img.youtube.com/vi/{VIDEO}/mqdefault.jpg",
        )

    def test_unrecognised_links_get_none(self):
        self.assertIsNone(ui.artwork("https://example.com/song"))

    def test_queue_shows_art_for_the_live_track(self):
        tracks = [Track(WATCH, f"Song {i}", 60, 1, "u") for i in range(20)]
        embed = ui.queue_page(tracks, 1, status=ui.PLAYING_MARKER)
        self.assertIn(VIDEO, embed.thumbnail.url)

    def test_later_pages_carry_no_art(self):
        """A thumbnail on page 4 claims something about a song not on screen."""
        tracks = [Track(WATCH, f"Song {i}", 60, 1, "u") for i in range(20)]
        self.assertIsNone(ui.queue_page(tracks, 2, status=ui.PLAYING_MARKER).thumbnail.url)

    def test_a_stopped_queue_carries_no_art(self):
        tracks = [Track(WATCH, f"Song {i}", 60, 1, "u") for i in range(5)]
        self.assertIsNone(ui.queue_page(tracks, 1).thumbnail.url)

    def test_rows_are_clipped_harder_when_art_narrows_the_column(self):
        long_title = "Tears For Fears - Everybody Wants To Rule The World"
        tracks = [Track(WATCH, long_title, 60, 1, "u") for _ in range(5)]

        with_art = ui.queue_page(tracks, 1, status=ui.PLAYING_MARKER).description
        without = ui.queue_page(tracks, 1).description

        self.assertIn("…", with_art)
        # Same titles, but the art page must not produce longer rows.
        self.assertLess(
            max(len(line) for line in with_art.splitlines()),
            max(len(line) for line in without.splitlines()),
        )

    def test_non_youtube_queue_still_renders(self):
        tracks = [Track(f"https://y/{i}", f"Song {i}", 60, 1, "u") for i in range(3)]
        embed = ui.queue_page(tracks, 1, status=ui.PLAYING_MARKER)
        self.assertIsNone(embed.thumbnail.url)
        self.assertIn("Now playing", embed.description)


class TestTracksAreClickable(unittest.TestCase):
    """Anywhere a song is named, its title is the link to it."""

    def _track(self):
        return Track(WATCH, "Song", 10, 1, "u")

    def test_skip_links_the_song(self):
        self.assertIn(f"](<{WATCH}>)", ui.skipped(self._track()).description)

    def test_skipto_links_the_song(self):
        self.assertIn(f"](<{WATCH}>)", ui.jumping_to(self._track()).description)

    def test_skip_without_a_track_does_not_break(self):
        rendered = ui.skipped(None)
        self.assertEqual(rendered.author.name, "Skipped")
        self.assertIn("Nothing was playing", rendered.description)

    def test_queue_rows_link_every_song(self):
        tracks = [Track(f"https://y/{i}", f"Song {i}", 60, 1, "u") for i in range(3)]
        body = ui.queue_page(tracks, 1).description
        for i in range(3):
            self.assertIn(f"](<https://y/{i}>)", body)

    def test_now_playing_title_carries_its_url(self):
        embed = ui.now_playing(ui.NowPlaying(
            title="Song", url=WATCH, duration=200, thumbnail=None, requester=None,
            volume=0.1, position=1, total=1, up_next=None, remaining=200,
        ))
        self.assertEqual(embed.url, WATCH)


class TestQueueLayout(unittest.TestCase):
    def _tracks(self, n):
        return [
            Track(f"https://y/{i}", f"Song {i}", 60 + i, 42, "tester")
            for i in range(n)
        ]

    def test_playing_track_is_lifted_out_of_the_numbered_list(self):
        body = ui.queue_page(self._tracks(20), 1, status=ui.PLAYING_MARKER).description
        self.assertIn("**Now playing**", body)
        self.assertIn("> **", body)  # blockquote, Discord's own vertical rule
        self.assertIn("**Up next**", body)

    def test_paused_queue_says_paused(self):
        body = ui.queue_page(self._tracks(20), 1, status=ui.PAUSED_MARKER).description
        self.assertIn("**Paused**", body)
        self.assertNotIn("**Now playing**", body)

    def test_up_next_numbering_matches_skipto(self):
        """The list numbers are the ones ?skipto takes, so 1 is the live song."""
        body = ui.queue_page(self._tracks(20), 1, status=ui.PLAYING_MARKER).description
        after = body.split("**Up next**")[1]
        self.assertTrue(after.strip().startswith("` 2.`"))

    def test_a_stopped_queue_is_just_a_list(self):
        """No live track means no two sections to tell apart, so no heading."""
        body = ui.queue_page(self._tracks(5), 1).description
        self.assertNotIn("Up next", body)
        self.assertNotIn("Now playing", body)
        self.assertTrue(body.startswith("` 1.`"))

    def test_later_pages_carry_no_heading(self):
        body = ui.queue_page(self._tracks(20), 2, status=ui.PLAYING_MARKER).description
        self.assertNotIn("Up next", body)
        self.assertTrue(body.startswith("`11.`"))

    def test_a_lone_playing_track_has_no_up_next(self):
        body = ui.queue_page(self._tracks(1), 1, status=ui.PLAYING_MARKER).description
        self.assertIn("**Now playing**", body)
        self.assertNotIn("Up next", body)

    def test_the_description_stays_inside_discords_limit(self):
        """Ten rows of pathological titles must still fit in one embed."""
        tracks = [Track("https://y/" + "u" * 90, "T" * 300, 60, 42, "x")
                  for _ in range(10)]
        self.assertLess(len(ui.queue_page(tracks, 1).description), 4096)


class TestAddCommandWiring(unittest.IsolatedAsyncioTestCase):
    """The ?add command must hand ui.added the right position and countdown."""

    def _ctx(self):
        channel = FakeChannel()
        ctx = FakeContext(channel)
        ctx.guild = type("G", (), {"id": 1})()
        ctx.author = type("A", (), {"id": 42, "name": "tester"})()
        return ctx

    def _player(self, entries, playlist_title=None, unavailable=0):
        from music_player.cogs.player import Player
        from music_player.services.youtube import FetchResult, TrackInfo

        youtube = FakeYouTube()

        async def fetch(url):
            return FetchResult(
                entries=[TrackInfo(f"https://y/{i}", f"Song {i}", d)
                         for i, d in enumerate(entries)],
                playlist_title=playlist_title,
                unavailable=unavailable,
            )

        youtube.fetch = fetch
        return Player(MagicMock(), MusicState(), youtube)

    async def _add(self, player, ctx, state):
        player.state._guilds[1] = state
        await player.add.callback(player, ctx, url="https://y/x")
        return ctx.sent[0]["embed"]

    async def test_first_song_into_an_empty_idle_queue(self):
        player = self._player([200])
        state = GuildState(1)
        embed = await self._add(player, self._ctx(), state)
        self.assertIn("?play", embed.footer.text)

    async def test_position_counts_songs_already_queued(self):
        player = self._player([200])
        state = GuildState(1)
        state.queue.extend(
            Track(f"https://y/o{i}", f"Old {i}", 60, 1, "u") for i in range(4)
        )
        embed = await self._add(player, self._ctx(), state)
        self.assertEqual(embed.footer.text, "#5 in queue")

    async def test_countdown_subtracts_what_has_already_played(self):
        """90s into a 100s track with one 200s song behind it: 210s to go."""
        player = self._player([50])
        state = GuildState(1)
        state.voice = FakeVoice()
        state.voice.playing = True
        state.queue.extend([
            Track("https://y/a", "Playing", 100, 1, "u"),
            Track("https://y/b", "Waiting", 200, 1, "u"),
        ])
        state.mark_started()
        state.playback_started -= 90

        embed = await self._add(player, self._ctx(), state)

        self.assertIn("#3 in queue", embed.footer.text)
        self.assertIn("3 min", embed.footer.text)  # 300 - 90 = 210s

    async def test_playlist_reports_its_own_total(self):
        player = self._player([200, 300, 400], playlist_title="Road Trip")
        state = GuildState(1)
        embed = await self._add(player, self._ctx(), state)

        self.assertEqual(embed.title, "Road Trip")
        self.assertIn("**3 songs**", embed.description)
        self.assertIn("15 min", embed.description)  # 900s total

    async def test_queue_actually_receives_every_track(self):
        player = self._player([200, 300, 400], playlist_title="Road Trip")
        state = GuildState(1)
        await self._add(player, self._ctx(), state)
        self.assertEqual(len(state.queue), 3)


class TestHumanDuration(unittest.TestCase):
    """"41 min" is for reading; "3:33" is for comparing against a clock."""

    def test_under_a_minute_is_seconds(self):
        self.assertEqual(ui.format_human(0), "0 sec")
        self.assertEqual(ui.format_human(59), "59 sec")

    def test_minutes(self):
        self.assertEqual(ui.format_human(60), "1 min")
        self.assertEqual(ui.format_human(2500), "41 min")

    def test_hours_drop_the_minutes_when_exact(self):
        self.assertEqual(ui.format_human(3600), "1 hr")
        self.assertEqual(ui.format_human(7200), "2 hr")

    def test_hours_and_minutes(self):
        self.assertEqual(ui.format_human(3900), "1 hr 5 min")

    def test_negative_is_clamped(self):
        self.assertEqual(ui.format_human(-10), "0 sec")


class TestProgressBar(unittest.TestCase):
    def test_start_of_track_is_empty(self):
        bar = ui.progress_bar(0, 200)
        self.assertNotIn("▰", bar)
        self.assertIn("0:00 / 3:20", bar)

    def test_halfway_fills_half(self):
        bar = ui.progress_bar(100, 200, width=10)
        self.assertEqual(bar.count("▰"), 5)
        self.assertEqual(bar.count("▱"), 5)

    def test_end_of_track_is_full(self):
        self.assertEqual(ui.progress_bar(200, 200, width=10).count("▱"), 0)

    def test_overrun_does_not_exceed_the_bar(self):
        """Elapsed can pass the reported duration; the bar must not overflow."""
        bar = ui.progress_bar(9999, 200, width=10)
        self.assertEqual(bar.count("▰"), 10)
        self.assertEqual(bar.count("▱"), 0)

    def test_unknown_duration_shows_elapsed_only(self):
        """A bar with no end to measure against would be inventing a position."""
        bar = ui.progress_bar(65, 0)
        self.assertNotIn("▰", bar)
        self.assertNotIn("▱", bar)
        self.assertIn("1:05", bar)


class TestPlaybackLine(unittest.TestCase):
    """An embed is a snapshot; only the <t:...:R> timestamp stays true.

    Discord renders relative timestamps client-side and keeps them ticking, so
    the finish time is correct minutes after the message was sent - without
    the bot editing anything.
    """

    NOW = 1_700_000_000

    def test_a_just_started_track_shows_an_empty_bar(self):
        """Drawn from the first frame, the way a music player draws one.

        It used to be withheld until there was progress, so a message
        scrolled past later would not show an empty gauge. Repainting on
        every pause and freezing what is no longer maintained covers that
        now, and 0:00 against the length is the plainer statement.
        """
        line = ui.playback_line(0, 213)
        self.assertIn(ui._BAR_EMPTY * 14, line)
        self.assertIn("0:00 / 3:33", line)
        self.assertNotIn(ui._BAR_FILLED, line)

    def test_a_track_in_progress_shows_the_bar(self):
        line = ui.playback_line(83, 213, now=self.NOW)
        self.assertIn("▰", line)
        self.assertIn("1:23 / 3:33", line)

    def test_the_finish_time_is_a_live_discord_timestamp(self):
        line = ui.playback_line(0, 213, now=self.NOW)
        self.assertIn(f"Ends <t:{self.NOW + 213}:R>", line)

    def test_the_finish_time_accounts_for_what_has_played(self):
        line = ui.playback_line(83, 213, now=self.NOW)
        self.assertIn(f"<t:{self.NOW + 130}:R>", line)

    def test_a_paused_track_gets_no_countdown(self):
        """It would keep counting down against audio that isn't playing."""
        line = ui.playback_line(83, 213, paused=True, now=self.NOW)
        self.assertNotIn("<t:", line)
        self.assertIn("1:23 / 3:33", line)

    def test_unknown_duration_gets_neither_bar_nor_countdown(self):
        line = ui.playback_line(95, 0, now=self.NOW)
        self.assertNotIn("<t:", line)
        self.assertNotIn("▰", line)
        self.assertIn("1:35", line)

    def test_an_overrunning_track_never_ends_in_the_past(self):
        line = ui.playback_line(9999, 213, now=self.NOW)
        self.assertIn(f"<t:{self.NOW}:R>", line)

    def test_the_embed_uses_it(self):
        embed = ui.now_playing(ui.NowPlaying(
            title="Song", url=WATCH, duration=213, thumbnail=None, requester=None,
            volume=0.1, position=1, total=1, up_next=None, remaining=213,
        ))
        self.assertIn("Ends <t:", embed.description)

    def test_the_paused_embed_does_not(self):
        embed = ui.now_playing(ui.NowPlaying(
            title="Song", url=WATCH, duration=213, thumbnail=None, requester=None,
            volume=0.1, position=1, total=1, up_next=None, remaining=213,
            elapsed=83, paused=True,
        ))
        self.assertNotIn("<t:", embed.description)


class TestNowPlayingEmbed(unittest.TestCase):
    def _snapshot(self, **overrides):
        base = dict(
            title="Song",
            url=WATCH,
            duration=200,
            thumbnail=None,
            requester=None,
            volume=0.25,
            position=1,
            total=1,
            up_next=None,
            remaining=200,
        )
        base.update(overrides)
        return ui.NowPlaying(**base)

    def test_playing_and_paused_are_told_apart(self):
        playing = ui.now_playing(self._snapshot())
        paused = ui.now_playing(self._snapshot(paused=True))

        self.assertEqual(playing.author.name, "Now playing")
        self.assertEqual(paused.author.name, "Paused")
        self.assertNotEqual(playing.colour, paused.colour)

    def test_volume_is_shown_as_a_percentage(self):
        embed = ui.now_playing(self._snapshot(volume=0.25))
        volumes = [f.value for f in embed.fields if f.name == "Volume"]
        self.assertEqual(volumes, ["25%"])

    def test_up_next_is_shown_when_there_is_one(self):
        nxt = Track(WATCH, "Next Song", 90, 7, "u")
        embed = ui.now_playing(self._snapshot(up_next=nxt, total=2))
        field = next(f for f in embed.fields if f.name == "Up next")
        self.assertIn("Next Song", field.value)
        self.assertIn("1:30", field.value)

    def test_up_next_is_omitted_for_the_last_song(self):
        embed = ui.now_playing(self._snapshot())
        self.assertNotIn("Up next", [f.name for f in embed.fields])

    def test_footer_gives_position_and_time_left(self):
        embed = ui.now_playing(self._snapshot(total=12, remaining=2500))
        self.assertIn("1 of 12", embed.footer.text)
        self.assertIn("41 min left", embed.footer.text)

    def test_lone_song_footer_omits_time_left(self):
        """"41 min left" against a single song just restates its duration."""
        embed = ui.now_playing(self._snapshot(total=1))
        self.assertIn("1 of 1", embed.footer.text)
        self.assertNotIn("left", embed.footer.text)


class TestTheCountdownNeverLies(unittest.IsolatedAsyncioTestCase):
    """"Ends in 2 minutes" is rendered by the Discord client, not by us.

    It counts down to an absolute instant with no idea the audio was paused,
    so it is only true while something is repainting the message. These pin
    the two halves of that: it corrects itself while the view is alive, and
    it is taken away the moment the view stops.
    """

    def _rig(self):
        from music_player.ui.views import PlayerControls

        state = GuildState(1)
        state.voice = FakeVoice()
        state.voice.playing = True
        state.mark_started()
        snapshot = ui.NowPlaying(
            title="A Song", url="https://y/1", duration=213, thumbnail=None,
            requester=None, volume=0.1, position=1, total=12, up_next=None,
            remaining=2500,
        )
        view = PlayerControls(MagicMock(), state, snapshot)
        view.message = AsyncMock()
        return state, view

    @staticmethod
    def _age(state, seconds):
        """Wall time passing: every absolute stamp gets that much older."""
        state.playback_started -= seconds
        if state.paused_at is not None:
            state.paused_at -= seconds

    @staticmethod
    def _stamp(embed):
        found = re.search(r"<t:(\d+):R>", embed.description or "")
        return int(found.group(1)) if found else None

    async def test_a_pause_takes_the_countdown_away(self):
        state, view = self._rig()
        self._age(state, 30)
        self.assertIsNotNone(self._stamp(view.render()))

        state.voice.pause()
        state.mark_paused()
        self.assertIsNone(self._stamp(view.render()))

    async def test_resuming_recomputes_it_from_where_the_song_really_is(self):
        state, view = self._rig()
        self._age(state, 30)
        state.voice.pause()
        state.mark_paused()
        self._age(state, 120)  # two minutes go by, paused
        state.voice.resume()
        state.mark_resumed()

        # 30s of a 213s song has been heard, so 183s remain - the two minutes
        # spent paused must not have eaten into it.
        remaining = self._stamp(view.render()) - int(time.time())
        self.assertAlmostEqual(remaining, 183, delta=2)
        self.assertAlmostEqual(state.elapsed, 30, delta=1)

    async def test_the_pause_button_records_who_pressed_it(self):
        """The buttons edit the card instead of replying, so this is the only
        place the channel can learn who stopped the music."""
        from music_player.cogs.player import Player

        state, view = self._rig()
        view.cog = Player(MagicMock(), MusicState(), FakeYouTube())
        self._age(state, 30)

        class _Response:
            async def edit_message(self, **kwargs):
                pass

        interaction = MagicMock()
        interaction.user = FakeUser(1, "tan")
        interaction.response = _Response()

        await view.toggle.callback(interaction)
        self.addCleanup(state.cancel_idle_disconnect)
        self.assertEqual(view.render().author.name, "Paused by tan")

        await view.toggle.callback(interaction)
        self.assertEqual(view.render().author.name, "Now playing")

    async def test_a_retired_message_stops_counting(self):
        """Nothing will repaint it again, so it must stop claiming to be live."""
        state, view = self._rig()
        self._age(state, 30)
        await view.retire()

        posted = view.message.edit.await_args.kwargs["embed"]
        self.assertIsNone(self._stamp(posted))
        self.assertIn("no longer updating", posted.footer.text)

    async def test_a_timed_out_message_stops_counting(self):
        state, view = self._rig()
        self._age(state, 30)
        await view.on_timeout()

        posted = view.message.edit.await_args.kwargs["embed"]
        self.assertIsNone(self._stamp(posted))

    async def test_freezing_still_greys_the_buttons(self):
        """The embed change must not have cost the thing fade() already did."""
        _state, view = self._rig()
        await view.retire()
        self.assertTrue(all(child.disabled for child in view.children))
        self.assertTrue(view.is_finished())

    async def test_the_progress_bar_survives_freezing(self):
        """Only the part that would keep moving is removed."""
        state, view = self._rig()
        self._age(state, 30)
        await view.retire()
        posted = view.message.edit.await_args.kwargs["embed"]
        self.assertIn("0:30 / 3:33", posted.description)


class TestPauseAndResume(unittest.TestCase):
    """Pausing starts a countdown to leaving, so it has to say so."""

    TRACK = Track(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "A Song", 213, 1, "tan"
    )

    def test_pausing_names_the_song_and_where_it_stopped(self):
        rendered = ui.paused(self.TRACK, 75.0)
        self.assertEqual(rendered.title, "A Song")
        self.assertEqual(rendered.url, self.TRACK.url)
        self.assertIn("1:15 / 3:33", rendered.description)

    def test_pausing_says_how_to_pick_it_back_up(self):
        self.assertIn("?resume", ui.paused(self.TRACK, 75.0).description)

    def test_pausing_warns_that_the_bot_will_leave(self):
        """apply_pause schedules the idle disconnect; an unwarned listener
        comes back to an empty channel and assumes it crashed."""
        self.assertIn("leave the channel", ui.paused(self.TRACK, 75.0).footer.text)

    def test_a_bot_that_never_leaves_makes_no_promise_about_it(self):
        self.assertIsNone(ui.paused(self.TRACK, 75.0, leaves_in=0).footer.text)

    def test_a_paused_track_gets_no_countdown(self):
        """The finish time would keep running against audio that is stopped."""
        self.assertNotIn("Ends <t:", ui.paused(self.TRACK, 75.0).description)

    def test_resuming_brings_the_finish_time_back(self):
        rendered = ui.resumed(self.TRACK, 75.0)
        self.assertEqual(rendered.title, "A Song")
        self.assertIn("Ends <t:", rendered.description)

    def test_pausing_at_the_very_start_still_shows_the_length(self):
        line = ui.paused(self.TRACK, 0.0).description
        self.assertIn("0:00 / 3:33", line)

    def test_both_carry_the_songs_cover(self):
        for rendered in (ui.paused(self.TRACK, 75.0), ui.resumed(self.TRACK, 75.0)):
            self.assertIn("dQw4w9WgXcQ", rendered.thumbnail.url)

    def test_a_link_with_no_derivable_cover_still_renders(self):
        """artwork() only knows YouTube ids; anything else gets no picture."""
        track = Track("https://example.com/audio", "Something Else", 200, 1, "u")
        for rendered in (ui.paused(track, 40.0), ui.resumed(track, 40.0)):
            self.assertIsNone(rendered.thumbnail.url)
            self.assertEqual(rendered.title, "Something Else")

    def test_both_name_whoever_did_it(self):
        who = FakeUser(1, "tan")
        self.assertEqual(ui.paused(self.TRACK, 75.0, by=who).author.name, "Paused by tan")
        self.assertEqual(
            ui.resumed(self.TRACK, 75.0, by=who).author.name, "Resumed by tan"
        )

    def test_the_eyebrow_carries_their_avatar(self):
        who = FakeUser(1, "tan")
        self.assertIsNotNone(ui.paused(self.TRACK, 75.0, by=who).author.icon_url)

    def test_the_label_falls_back_when_nobody_is_recorded(self):
        self.assertEqual(ui.paused(self.TRACK, 75.0).author.name, "Paused")
        self.assertIsNone(ui.paused(self.TRACK, 75.0).author.icon_url)

    def test_both_still_render_without_a_track(self):
        self.assertIn("Paused", ui.paused().description)
        self.assertIn("Resumed", ui.resumed().description)


class TestSkipSaysWhoDidIt(unittest.IsolatedAsyncioTestCase):
    """A song vanishing is the thing a channel most wants attributed."""

    TRACK = Track("https://youtu.be/dQw4w9WgXcQ", "A Song", 213, 1, "sam")

    def test_the_command_names_the_skipper(self):
        rendered = ui.skipped(self.TRACK, by=FakeUser(1, "tan"))
        self.assertEqual(rendered.author.name, "Skipped by tan")
        self.assertIsNotNone(rendered.author.icon_url)

    def test_skipto_reads_as_a_skip_too(self):
        rendered = ui.jumping_to(self.TRACK, by=FakeUser(1, "tan"))
        self.assertEqual(rendered.author.name, "Skipped ahead by tan")

    def test_the_song_is_still_linked(self):
        rendered = ui.skipped(self.TRACK, by=FakeUser(1, "tan"))
        self.assertIn(f"(<{self.TRACK.url}>)", rendered.description)

    def test_it_falls_back_when_nobody_is_recorded(self):
        self.assertEqual(ui.skipped(self.TRACK).author.name, "Skipped")

    def test_the_skipped_song_gets_no_artwork(self):
        """The next song's Now Playing card follows immediately; the wrong
        cover directly above the right one would just confuse."""
        self.assertIsNone(ui.skipped(self.TRACK, by=FakeUser()).thumbnail.url)

    async def test_the_skip_button_names_whoever_pressed_it(self):
        from music_player.ui.views import PlayerControls
        from music_player.cogs.player import Player

        state = GuildState(1)
        state.voice = FakeVoice()
        state.voice.playing = True
        state.queue.append(self.TRACK)
        state.mark_started()

        snapshot = ui.NowPlaying(
            title="A Song", url="https://y/1", duration=213, thumbnail=None,
            requester=None, volume=0.1, position=1, total=1, up_next=None,
            remaining=213,
        )
        view = PlayerControls(
            Player(MagicMock(), MusicState(), FakeYouTube()), state, snapshot
        )
        view.message = AsyncMock()

        interaction = MagicMock()
        interaction.user = FakeUser(1, "tan")
        interaction.channel = FakeChannel()
        interaction.response = AsyncMock()
        interaction.followup = AsyncMock()

        await view.skip.callback(interaction)
        self.addCleanup(state.cancel_idle_disconnect)

        posted = interaction.followup.send.await_args.kwargs["embed"]
        self.assertEqual(posted.author.name, "Skipped by tan")


class TestVolumeMeter(unittest.TestCase):
    def test_meter_tracks_the_number(self):
        self.assertEqual(ui.volume_set(0).description.count("▰"), 0)
        self.assertEqual(ui.volume_set(50).description.count("▰"), 5)
        self.assertEqual(ui.volume_set(100).description.count("▰"), 10)

    def test_meter_is_always_ten_cells(self):
        for percent in (0, 7, 33, 50, 99, 100):
            body = ui.volume_set(percent).description
            self.assertEqual(body.count("▰") + body.count("▱"), 10, percent)


class TestDeadEndsOfferAWayOut(unittest.TestCase):
    """Every message that says "no" must also say what to do instead."""

    def test_empty_queue_names_the_command_that_fills_it(self):
        self.assertIn("?add", ui.empty_queue().description)

    def test_not_in_voice_names_the_join_command(self):
        self.assertIn("/join", ui.not_in_voice().description)

    def test_nothing_playing_names_the_play_command(self):
        self.assertIn("?play", ui.nothing_playing().description)

    def test_bad_page_states_the_real_range(self):
        self.assertIn("19", ui.no_such_page(40, 19).description)

    def test_bad_song_number_states_the_queue_length(self):
        body = ui.no_such_song(40, 12).description
        self.assertIn("12", body)
        self.assertIn("?queue", body)


class TestElapsedTracking(unittest.TestCase):
    """The progress bar must show time heard, not time since the song began."""

    def test_elapsed_is_zero_before_playback(self):
        self.assertEqual(GuildState(1).elapsed, 0.0)

    def test_elapsed_advances_with_the_clock(self):
        state = GuildState(1)
        state.mark_started()
        state.playback_started -= 30  # pretend 30s of audio has gone by
        self.assertAlmostEqual(state.elapsed, 30, delta=1)

    def test_elapsed_freezes_while_paused(self):
        state = GuildState(1)
        state.mark_started()
        state.playback_started -= 30
        state.mark_paused()
        frozen = state.elapsed
        time.sleep(0.05)
        self.assertEqual(state.elapsed, frozen)

    def test_paused_time_is_not_counted_as_played(self):
        state = GuildState(1)
        state.mark_started()
        # Wall clock says the track began 90s ago, but it was paused 60s ago -
        # that is, 30s in - and has sat paused since.
        state.playback_started -= 90
        state.mark_paused()
        state.paused_at -= 60
        state.mark_resumed()
        # 30s heard, 60s paused: the bar must read 30s, not 90s.
        self.assertAlmostEqual(state.elapsed, 30, delta=1)

    def test_restarting_clears_the_pause_ledger(self):
        state = GuildState(1)
        state.mark_started()
        state.mark_paused()
        state.paused_at -= 60
        state.mark_resumed()
        state.mark_started()
        self.assertEqual(state.paused_total, 0.0)
        self.assertIsNone(state.paused_at)
        self.assertAlmostEqual(state.elapsed, 0, delta=1)


class TestQueuePagesView(unittest.IsolatedAsyncioTestCase):
    def _view(self, count, page=1):
        from music_player.ui.views import QueuePages
        from music_player.cogs.player import Player

        player = Player(MagicMock(), MusicState(), FakeYouTube())
        state = GuildState(1)
        state.queue.extend(
            Track(f"https://y/{i}", f"Song {i}", 60, 42, "t") for i in range(count)
        )
        return QueuePages(player, state, user_id=7, page=page), state

    async def test_arrows_are_disabled_at_the_ends(self):
        view, _ = self._view(35)  # 4 pages
        self.assertTrue(view.previous.disabled)
        self.assertFalse(view.next.disabled)

        view.page = 4
        view.sync()
        self.assertFalse(view.previous.disabled)
        self.assertTrue(view.next.disabled)

    async def test_single_page_disables_both_arrows(self):
        view, _ = self._view(3)
        self.assertTrue(view.previous.disabled)
        self.assertTrue(view.next.disabled)

    async def test_indicator_reads_the_current_page(self):
        view, _ = self._view(35, page=3)
        self.assertEqual(view.indicator.label, "3 / 4")

    async def test_page_is_clamped_to_the_live_queue(self):
        """Songs finish while a page is open; page 4 of 4 can become page 4 of 1."""
        view, state = self._view(35, page=4)
        del state.queue[5:]
        view.sync()
        self.assertEqual(view.page, 1)
        self.assertEqual(view.indicator.label, "1 / 1")

    async def test_render_marks_the_playing_track(self):
        view, state = self._view(35)
        state.voice = FakeVoice()
        state.voice.playing = True
        self.assertIn(ui.PLAYING_MARKER, view.render().description)

    async def test_other_users_cannot_turn_the_page(self):
        view, _ = self._view(35)
        interaction = MagicMock()
        interaction.user.id = 999
        interaction.response.send_message = AsyncMock()

        self.assertFalse(await view.interaction_check(interaction))
        interaction.response.send_message.assert_awaited_once()

    async def test_the_owner_can_turn_the_page(self):
        view, _ = self._view(35)
        interaction = MagicMock()
        interaction.user.id = 7
        self.assertTrue(await view.interaction_check(interaction))


class TestPlayerControlsView(unittest.IsolatedAsyncioTestCase):
    def _view(self):
        from music_player.ui.views import PlayerControls
        from music_player.cogs.player import Player

        player = Player(MagicMock(), MusicState(), FakeYouTube())
        state = GuildState(1)
        state.voice = FakeVoice()
        state.queue.append(Track(WATCH, "Song", 200, 42, "t"))
        snapshot = ui.NowPlaying(
            title="Song", url=WATCH, duration=200, thumbnail=None, requester=None,
            volume=0.1, position=1, total=1, up_next=None, remaining=200,
        )
        return PlayerControls(player, state, snapshot), state, player

    async def test_toggle_offers_pause_while_playing(self):
        view, state, _ = self._view()
        state.voice.playing = True
        view.sync()
        self.assertEqual(view.toggle.label, "Pause")

    async def test_toggle_offers_resume_while_paused(self):
        view, state, _ = self._view()
        state.voice.playing = False
        state.voice.paused = True
        view.sync()
        self.assertEqual(view.toggle.label, "Resume")

    async def test_render_reflects_live_state(self):
        view, state, player = self._view()
        state.voice.playing = True
        state.mark_started()
        state.volume = 0.4

        player.apply_pause(state)
        embed = view.render()

        self.assertEqual(embed.author.name, "Paused")
        self.assertIn("40%", [f.value for f in embed.fields if f.name == "Volume"])

    async def test_non_listeners_are_turned_away(self):
        view, state, _ = self._view()
        interaction = MagicMock()
        interaction.user.voice = None
        interaction.response.send_message = AsyncMock()

        self.assertFalse(await view.interaction_check(interaction))
        interaction.response.send_message.assert_awaited_once()

    async def test_listeners_in_the_channel_are_allowed(self):
        view, state, _ = self._view()
        interaction = MagicMock()
        interaction.user.voice.channel = state.voice.channel
        self.assertTrue(await view.interaction_check(interaction))

    async def test_retiring_greys_the_buttons_out(self):
        view, _, _ = self._view()
        view.message = MagicMock()
        view.message.edit = AsyncMock()

        await view.retire()

        self.assertTrue(all(child.disabled for child in view.children))
        view.message.edit.assert_awaited_once()

    async def test_retiring_without_a_message_is_harmless(self):
        view, _, _ = self._view()
        await view.retire()  # must not raise
        self.assertTrue(view.is_finished())


class TestSharedActions(unittest.IsolatedAsyncioTestCase):
    """A button press and a command must not be able to drift apart."""

    def _player_state(self):
        from music_player.cogs.player import Player

        player = Player(MagicMock(), MusicState(), FakeYouTube())
        state = GuildState(1)
        state.voice = FakeVoice()
        state.voice.playing = True
        state.queue.append(Track(WATCH, "Song", 200, 42, "t"))
        state.mark_started()
        return player, state

    async def test_pause_then_resume_round_trips(self):
        player, state = self._player_state()

        self.assertTrue(player.apply_pause(state))
        self.assertTrue(state.paused)
        self.assertTrue(state.suppress_advance)

        self.assertTrue(player.apply_resume(state))
        self.assertTrue(state.playing)
        self.assertFalse(state.suppress_advance)
        state.cancel_idle_disconnect()

    async def test_pausing_twice_is_a_no_op(self):
        player, state = self._player_state()
        self.assertTrue(player.apply_pause(state))
        self.assertFalse(player.apply_pause(state))
        state.cancel_idle_disconnect()

    async def test_resuming_what_is_not_paused_is_a_no_op(self):
        player, state = self._player_state()
        self.assertFalse(player.apply_resume(state))

    async def test_pause_schedules_the_idle_timer_and_resume_cancels_it(self):
        player, state = self._player_state()

        player.apply_pause(state)
        self.assertIsNotNone(state._idle_task)

        player.apply_resume(state)
        self.assertIsNone(state._idle_task)

    async def test_status_marker_follows_playback(self):
        from music_player.cogs.player import Player

        state = GuildState(1)
        self.assertEqual(Player.status_marker(state), "")

        state.voice = FakeVoice()
        state.voice.playing = True
        self.assertEqual(Player.status_marker(state), ui.PLAYING_MARKER)

        state.voice.playing = False
        state.voice.paused = True
        self.assertEqual(Player.status_marker(state), ui.PAUSED_MARKER)


class _LogTestCase(unittest.TestCase):
    """Base for log tests: a scratch tree and a calendar we control."""

    def setUp(self):
        import tempfile
        from music_player import logs

        self.logs = logs
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "logs"
        self.addCleanup(self._tmp.cleanup)

    def at(self, y, m, d):
        """Pretend today is this date, for the duration of the block."""
        import datetime as real_dt
        return patch.object(self.logs, "_today", lambda: real_dt.date(y, m, d))

    def read(self, y, m, d, stem="bot"):
        path = self.root / f"{y:04d}" / f"{m:02d}" / f"{d:02d}" / f"{stem}.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""


class TestDatedFolderHandler(_LogTestCase):
    def _record(self, message="hello"):
        return logging.LogRecord(
            "test", logging.INFO, __file__, 1, message, None, None
        )

    def test_writes_into_a_year_month_day_folder(self):
        with self.at(2026, 8, 9):
            handler = self.logs.DatedFolderHandler(self.root, "bot")
            handler.emit(self._record("first line"))
            handler.close()
        self.assertIn("first line", self.read(2026, 8, 9))

    def test_midnight_moves_to_the_next_days_folder(self):
        """The failure a long-running bot actually hits."""
        with self.at(2026, 8, 9):
            handler = self.logs.DatedFolderHandler(self.root, "bot")
            handler.emit(self._record("tuesday"))
        with self.at(2026, 8, 10):
            handler.emit(self._record("wednesday"))
            handler.close()

        self.assertIn("tuesday", self.read(2026, 8, 9))
        self.assertNotIn("wednesday", self.read(2026, 8, 9))
        self.assertIn("wednesday", self.read(2026, 8, 10))

    def test_a_new_month_and_year_nest_correctly(self):
        with self.at(2026, 12, 31):
            handler = self.logs.DatedFolderHandler(self.root, "bot")
            handler.emit(self._record("old year"))
        with self.at(2027, 1, 1):
            handler.emit(self._record("new year"))
            handler.close()
        self.assertIn("new year", self.read(2027, 1, 1))

    def test_a_huge_day_continues_in_a_numbered_part(self):
        """One runaway error loop must not become one unopenable file."""
        with self.at(2026, 8, 9):
            handler = self.logs.DatedFolderHandler(self.root, "bot", max_bytes=200)
            for i in range(40):
                handler.emit(self._record(f"line {i} " + "x" * 40))
            handler.close()

        day = self.root / "2026" / "08" / "09"
        parts = sorted(p.name for p in day.glob("bot*.log"))
        self.assertIn("bot.log", parts)
        self.assertIn("bot.2.log", parts)

    def test_parts_reset_when_the_day_turns_over(self):
        with self.at(2026, 8, 9):
            handler = self.logs.DatedFolderHandler(self.root, "bot", max_bytes=120)
            for i in range(20):
                handler.emit(self._record("x" * 40))
        with self.at(2026, 8, 10):
            handler.emit(self._record("fresh day"))
            handler.close()
        # The new day starts at bot.log, not wherever the old one left off.
        self.assertIn("fresh day", self.read(2026, 8, 10))

    def test_a_title_the_console_cannot_encode_is_still_written(self):
        """The bug that made this module necessary."""
        with self.at(2026, 8, 9):
            handler = self.logs.DatedFolderHandler(self.root, "bot")
            handler.emit(self._record("スパークル [original ver.] 🎵"))
            handler.close()
        self.assertIn("スパークル", self.read(2026, 8, 9))


class TestLogRetention(_LogTestCase):
    def _make_day(self, y, m, d):
        folder = self.root / f"{y:04d}" / f"{m:02d}" / f"{d:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "bot.log").write_text("x", encoding="utf-8")
        return folder

    def test_old_days_are_removed_and_recent_ones_kept(self):
        old = self._make_day(2026, 7, 1)
        recent = self._make_day(2026, 8, 8)

        with self.at(2026, 8, 9):
            removed = self.logs.prune(self.root, keep_days=14)

        self.assertEqual(removed, 1)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_empty_year_and_month_shells_are_tidied_away(self):
        self._make_day(2026, 7, 1)
        with self.at(2026, 8, 9):
            self.logs.prune(self.root, keep_days=14)
        self.assertFalse((self.root / "2026" / "07").exists())

    def test_zero_keeps_everything(self):
        old = self._make_day(2020, 1, 1)
        with self.at(2026, 8, 9):
            self.assertEqual(self.logs.prune(self.root, keep_days=0), 0)
        self.assertTrue(old.exists())

    def test_unrelated_folders_are_left_alone(self):
        stray = self.root / "notes" / "for" / "later"
        stray.mkdir(parents=True)
        (stray / "keep.txt").write_text("mine", encoding="utf-8")
        with self.at(2026, 8, 9):
            self.logs.prune(self.root, keep_days=1)
        self.assertTrue((stray / "keep.txt").exists())

    def test_a_missing_root_is_not_an_error(self):
        self.assertEqual(self.logs.prune(self.root / "nope", keep_days=7), 0)


class TestTracingCannotBreakTheCommand(unittest.IsolatedAsyncioTestCase):
    """Observability must never take down the thing it observes."""

    async def test_a_hostile_argument_does_not_stop_the_command(self):
        """str() on a command argument raising must cost a log line, not the run."""
        import app

        class Exploding:
            def __str__(self):
                raise RuntimeError("nope")
            __repr__ = __str__

        ctx = MagicMock()
        ctx.command.qualified_name = "add"
        ctx.kwargs = {"url": Exploding()}

        described = app._describe(ctx)          # must not raise
        self.assertIn("add", described)

    async def test_the_traced_block_still_runs_when_binding_fails(self):
        from music_player import logs

        ran = []
        with patch.object(logs, "bind", side_effect=RuntimeError("boom")):
            with logs.traced("thing"):
                ran.append(True)
        self.assertEqual(ran, [True])

    async def test_the_traced_block_still_runs_when_logging_is_broken(self):
        from music_player import logs

        ran = []
        with patch.object(logs.log, "info", side_effect=RuntimeError("disk full")):
            with logs.traced("thing"):
                ran.append(True)
        self.assertEqual(ran, [True])

    async def test_the_callers_own_exception_still_propagates(self):
        """Guarding the trace must not swallow real failures."""
        from music_player import logs

        with self.assertRaises(ValueError):
            with logs.traced("thing"):
                raise ValueError("the actual bug")

    async def test_context_is_released_even_when_the_body_raises(self):
        from music_player import logs

        record = logging.LogRecord("t", logging.INFO, __file__, 1, "m", None, None)
        try:
            with logs.traced("thing", guild="Cool Server"):
                raise ValueError
        except ValueError:
            pass
        logs._ContextFilter().filter(record)
        self.assertEqual(record.ctx, "")


class TestFfmpegIsLogged(unittest.TestCase):
    """ffmpeg is where playback actually fails, so none of it may be muted."""

    def setUp(self):
        # The suite silences logging globally; these tests are about whether a
        # record is emitted at all, so they need it back for their duration.
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.CRITICAL)

    def test_ffmpeg_stderr_is_routed_into_logging(self):
        from music_player.cogs.player import _FFmpegLog

        sink = _FFmpegLog("https://youtube.com/watch?v=x")
        with self.assertLogs("music_player.cogs.player", level="WARNING") as caught:
            sink.write(b"[tcp @ 0x1] Failed to resolve hostname rr3---sn-x\n")
        self.assertIn("Failed to resolve hostname", caught.output[0])

    def test_the_sink_has_no_fileno(self):
        """That absence is how discord.py decides to pipe stderr to us at all."""
        from music_player.cogs.player import _FFmpegLog

        self.assertFalse(hasattr(_FFmpegLog("u"), "fileno"))

    def test_blank_ffmpeg_output_is_not_logged(self):
        from music_player.cogs.player import _FFmpegLog

        logger = logging.getLogger("music_player.cogs.player")
        with patch.object(logger, "warning") as warned:
            _FFmpegLog("u").write(b"   \n")
        warned.assert_not_called()

    def test_discord_player_is_exempt_from_the_gateway_muting(self):
        """It carries the ffmpeg command line and, crucially, the exit code.

        _ended_early exists because ffmpeg returns 0 after a 403; this logger
        is the only place the real return code shows up.
        """
        from music_player import logs

        # configure() has already run for the suite, so assert the outcome.
        logs.configure()
        self.assertLessEqual(
            logging.getLogger("discord.player").level, logging.INFO
        )
        self.assertLessEqual(
            logging.getLogger("discord.voice_client").level, logging.INFO
        )


class TestLogRedaction(unittest.TestCase):
    """A log file must always be safe to paste into a bug report."""

    def _render(self, message, *, secrets=()):
        from music_player import logs

        formatter = logs._SafeFormatter("%(message)s", secrets=secrets)
        record = logging.LogRecord("t", logging.INFO, __file__, 1, message, None, None)
        record.ctx = ""
        return formatter.format(record)

    def test_the_bot_token_never_reaches_the_output(self):
        token = "MTIzNDU2Nzg5.SUPERSECRET.abcdefg"
        out = self._render(f"login with {token}", secrets=(token,))
        self.assertNotIn(token, out)
        self.assertIn("***redacted***", out)

    def test_signed_stream_parameters_are_scrubbed(self):
        out = self._render(
            "https://x.googlevideo.com/vp?expire=1&signature=DEADBEEF&pot=SECRET"
        )
        self.assertNotIn("DEADBEEF", out)
        self.assertNotIn("SECRET", out)
        # The rest of the URL survives - it is what makes the log useful.
        self.assertIn("googlevideo.com", out)
        self.assertIn("expire=1", out)

    def test_cookie_headers_are_scrubbed(self):
        out = self._render("-headers Cookie: SID=abc123; HSID=xyz")
        self.assertNotIn("abc123", out)

    def test_an_empty_secret_does_not_redact_everything(self):
        out = self._render("perfectly ordinary line", secrets=("",))
        self.assertEqual(out, "perfectly ordinary line")


class TestLogContext(unittest.TestCase):
    def _render(self):
        from music_player import logs

        record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)
        logs._ContextFilter().filter(record)
        return record.ctx

    def test_no_context_renders_nothing(self):
        """Background tasks shouldn't each pay for a row of empty fields."""
        self.assertEqual(self._render(), "")

    def test_bound_fields_appear(self):
        from music_player import logs

        with logs.context(guild="Cool Server", user="tan"):
            rendered = self._render()
        self.assertIn("guild=Cool Server", rendered)
        self.assertIn("user=tan", rendered)

    def test_context_is_released_afterwards(self):
        from music_player import logs

        with logs.context(guild="Cool Server"):
            pass
        self.assertEqual(self._render(), "")

    def test_nested_binds_merge(self):
        from music_player import logs

        with logs.context(guild="Cool Server"):
            with logs.context(cmd="play"):
                rendered = self._render()
        self.assertIn("guild=Cool Server", rendered)
        self.assertIn("cmd=play", rendered)

    def test_none_values_are_dropped(self):
        from music_player import logs

        with logs.context(guild="Cool Server", user=None):
            self.assertNotIn("user=", self._render())

    async def _tagged(self, name, seen):
        from music_player import logs

        with logs.context(cmd=name):
            await asyncio.sleep(0)
            seen[name] = self._render()

    def test_concurrent_commands_do_not_leak_into_each_other(self):
        """Each invocation is its own task, so contexts must stay separate."""
        seen = {}

        async def run():
            await asyncio.gather(
                self._tagged("play", seen), self._tagged("skip", seen)
            )

        asyncio.run(run())
        self.assertIn("cmd=play", seen["play"])
        self.assertNotIn("skip", seen["play"])
        self.assertIn("cmd=skip", seen["skip"])
        self.assertNotIn("play", seen["skip"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---------------------------------------------------------------------------
# Saved playlists
# ---------------------------------------------------------------------------

import json  # noqa: E402
import sqlite3  # noqa: E402

from discord.ext import commands  # noqa: E402

from music_player.config import (  # noqa: E402
    MAX_PLAYLIST_TRACKS,
    MAX_PLAYLISTS_PER_GUILD,
)
from music_player.services.library import (  # noqa: E402
    InvalidName,
    NoSuchPlaylist,
    NoSuchSong,
    Playlist,
    PlaylistExists,
    PlaylistFull,
    PlaylistLibrary,
    SavedTrack,
    StorageError,
    TooManyPlaylists,
    fold,
    normalise_name,
)

#: The guild every cog test acts in. Playlists are keyed by guild id, so a bare
#: int in these tests is always a server, never a person.
GUILD = 1
OTHER_GUILD = 2


class FakePermissions:
    def __init__(self, manage_guild: bool = False) -> None:
        self.manage_guild = manage_guild


class FakeGuild:
    def __init__(self, gid: int = GUILD, name: str = "Test Server") -> None:
        self.id = gid
        self.name = name
        self.icon = None


class FakeUser:
    """Stands in for a discord.Member / discord.User."""

    def __init__(
        self, uid: int = 42, name: str = "tester", *, manage_guild: bool = False
    ) -> None:
        self.id = uid
        self.name = name
        self.display_name = name
        self.avatar = None
        self.guild_permissions = FakePermissions(manage_guild)


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list = []
        self.edits: list = []
        self.deferred = False

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)

    async def defer(self, **kwargs):
        self.deferred = True


class FakeInteraction:
    """Stands in for a component interaction on a view."""

    def __init__(self, user: FakeUser, channel, guild_id=GUILD) -> None:
        self.user = user
        self.channel = channel
        self.guild = FakeGuild(guild_id) if guild_id else None
        self.response = FakeResponse()


def _saved(count: int, *, seconds: int = 60) -> list:
    return [
        SavedTrack(f"https://youtu.be/{i:011d}", f"Song {i}", seconds)
        for i in range(count)
    ]


def _fresh_db() -> Path:
    return Path(tempfile.mkdtemp(prefix="playlist-test-")) / "sub" / "playlists.db"


class TestPlaylistNames(unittest.TestCase):
    """Names are shown as typed but matched loosely, so the two must agree."""

    def test_surrounding_and_repeated_whitespace_is_collapsed(self):
        self.assertEqual(normalise_name("  Late   Night  "), "Late Night")

    def test_a_newline_cannot_survive_into_an_embed(self):
        self.assertEqual(normalise_name("Late\nNight"), "Late Night")

    def test_an_empty_name_is_refused(self):
        with self.assertRaises(InvalidName) as caught:
            normalise_name("   ")
        self.assertEqual(caught.exception.reason, "empty")

    def test_an_overlong_name_is_refused(self):
        with self.assertRaises(InvalidName) as caught:
            normalise_name("x" * 500)
        self.assertEqual(caught.exception.reason, "long")

    def test_matching_ignores_case(self):
        self.assertEqual(fold("Late Night"), fold("LATE night"))

    def test_the_two_agree_on_collapsed_whitespace(self):
        """?playlist play "late  night" has to find "Late Night"."""
        self.assertEqual(fold(normalise_name("late  night")), fold("Late Night"))


class TestPlaylistLibrary(unittest.IsolatedAsyncioTestCase):
    """What a server may and may not do to its own playlists."""

    def setUp(self):
        self.path = _fresh_db()
        self.library = PlaylistLibrary(self.path)

    def tearDown(self):
        self.library.close()

    async def test_a_new_library_is_empty(self):
        self.assertEqual(await self.library.summaries(GUILD), [])

    async def test_create_then_find_is_case_insensitive(self):
        await self.library.create(GUILD, "Late Night")
        self.assertIsNotNone(await self.library.find(GUILD, "LATE NIGHT"))

    async def test_the_name_is_kept_as_it_was_typed(self):
        await self.library.create(GUILD, "Late Night")
        found = await self.library.find(GUILD, "late night")
        self.assertEqual(found.name, "Late Night")

    async def test_playlists_belong_to_one_server_only(self):
        """A playlist made in server A must not exist in server B."""
        await self.library.create(GUILD, "Ours")
        self.assertIsNone(await self.library.find(OTHER_GUILD, "Ours"))
        self.assertEqual(await self.library.summaries(OTHER_GUILD), [])

    async def test_two_servers_may_use_the_same_name(self):
        await self.library.create(GUILD, "Mix")
        await self.library.create(OTHER_GUILD, "Mix")
        await self.library.extend(GUILD, "Mix", _saved(2))
        self.assertEqual(len((await self.library.find(GUILD, "Mix")).tracks), 2)
        self.assertEqual(len((await self.library.find(OTHER_GUILD, "Mix")).tracks), 0)

    async def test_a_duplicate_name_is_refused(self):
        await self.library.create(GUILD, "Mix")
        with self.assertRaises(PlaylistExists):
            await self.library.create(GUILD, "  mix  ")

    async def test_the_duplicate_check_beats_the_limit_check(self):
        """At the cap, "pick another name" is still the useful answer."""
        for index in range(MAX_PLAYLISTS_PER_GUILD):
            await self.library.create(GUILD, f"P{index}")
        with self.assertRaises(PlaylistExists):
            await self.library.create(GUILD, "p0")

    async def test_the_playlist_limit_is_enforced(self):
        for index in range(MAX_PLAYLISTS_PER_GUILD):
            await self.library.create(GUILD, f"P{index}")
        with self.assertRaises(TooManyPlaylists):
            await self.library.create(GUILD, "one too many")

    async def test_the_limit_is_per_server(self):
        for index in range(MAX_PLAYLISTS_PER_GUILD):
            await self.library.create(GUILD, f"P{index}")
        await self.library.create(OTHER_GUILD, "Plenty of room")  # must not raise

    async def test_a_refused_create_leaves_no_trace(self):
        with self.assertRaises(InvalidName):
            await self.library.create(GUILD, "  ")
        self.assertEqual(await self.library.summaries(GUILD), [])

    async def test_the_creator_is_recorded(self):
        playlist = await self.library.create(GUILD, "Mix", created_by=99)
        self.assertEqual(playlist.created_by, 99)

    async def test_extend_reports_what_it_added(self):
        await self.library.create(GUILD, "Mix")
        playlist, added = await self.library.extend(GUILD, "Mix", _saved(3))
        self.assertEqual(added, 3)
        self.assertEqual(len(playlist.tracks), 3)

    async def test_extend_returns_the_playlist_as_it_now_is(self):
        """The confirmation quotes this, so a stale copy would misreport."""
        await self.library.create(GUILD, "Mix")
        await self.library.extend(GUILD, "Mix", _saved(2))
        playlist, _added = await self.library.extend(GUILD, "Mix", _saved(3))
        self.assertEqual(len(playlist.tracks), 5)

    async def test_songs_keep_the_order_they_were_added_in(self):
        await self.library.create(GUILD, "Mix")
        await self.library.extend(GUILD, "Mix", _saved(3))
        playlist, _ = await self.library.extend(
            GUILD, "Mix", [SavedTrack("https://y/last", "Last", 10)]
        )
        self.assertEqual(
            [t.title for t in playlist.tracks],
            ["Song 0", "Song 1", "Song 2", "Last"],
        )

    async def test_extend_takes_what_fits_rather_than_refusing(self):
        await self.library.create(GUILD, "Mix")
        await self.library.extend(GUILD, "Mix", _saved(MAX_PLAYLIST_TRACKS - 2))
        playlist, added = await self.library.extend(GUILD, "Mix", _saved(10))
        self.assertEqual(added, 2)
        self.assertEqual(len(playlist.tracks), MAX_PLAYLIST_TRACKS)

    async def test_a_full_playlist_refuses_outright(self):
        await self.library.create(GUILD, "Mix")
        await self.library.extend(GUILD, "Mix", _saved(MAX_PLAYLIST_TRACKS))
        with self.assertRaises(PlaylistFull):
            await self.library.extend(GUILD, "Mix", _saved(1))

    async def test_remove_uses_the_numbers_the_listing_prints(self):
        await self.library.create(GUILD, "Mix")
        await self.library.extend(GUILD, "Mix", _saved(3))
        playlist, removed = await self.library.remove_at(GUILD, "Mix", 2)
        self.assertEqual(removed.title, "Song 1")
        self.assertEqual([t.title for t in playlist.tracks], ["Song 0", "Song 2"])

    async def test_removing_a_number_that_is_not_there(self):
        await self.library.create(GUILD, "Mix")
        await self.library.extend(GUILD, "Mix", _saved(2))
        with self.assertRaises(NoSuchSong) as caught:
            await self.library.remove_at(GUILD, "Mix", 5)
        self.assertEqual((caught.exception.asked, caught.exception.total), (5, 2))

    async def test_zero_is_not_a_song_number(self):
        await self.library.create(GUILD, "Mix")
        await self.library.extend(GUILD, "Mix", _saved(2))
        with self.assertRaises(NoSuchSong):
            await self.library.remove_at(GUILD, "Mix", 0)

    async def test_renaming_to_a_different_capitalisation_is_allowed(self):
        """Renaming a playlist onto its own key is a fix, not a collision."""
        await self.library.create(GUILD, "chill")
        await self.library.rename(GUILD, "chill", "Chill")
        self.assertEqual((await self.library.find(GUILD, "CHILL")).name, "Chill")

    async def test_renaming_onto_another_playlist_is_refused(self):
        await self.library.create(GUILD, "One")
        await self.library.create(GUILD, "Two")
        with self.assertRaises(PlaylistExists):
            await self.library.rename(GUILD, "One", "two")
        self.assertIsNotNone(await self.library.find(GUILD, "One"))

    async def test_renaming_keeps_the_songs(self):
        await self.library.create(GUILD, "One")
        await self.library.extend(GUILD, "One", _saved(3))
        renamed = await self.library.rename(GUILD, "One", "Two")
        self.assertEqual(len(renamed.tracks), 3)

    async def test_delete_removes_it(self):
        await self.library.create(GUILD, "Mix")
        await self.library.delete(GUILD, "mix")
        self.assertEqual(await self.library.summaries(GUILD), [])

    async def test_delete_returns_the_playlist_as_it_was(self):
        await self.library.create(GUILD, "Mix")
        await self.library.extend(GUILD, "Mix", _saved(3))
        deleted = await self.library.delete(GUILD, "Mix")
        self.assertEqual(len(deleted.tracks), 3)

    async def test_every_operation_names_a_playlist_that_is_not_there(self):
        for call in (
            self.library.require(GUILD, "ghost"),
            self.library.delete(GUILD, "ghost"),
            self.library.rename(GUILD, "ghost", "new"),
            self.library.extend(GUILD, "ghost", _saved(1)),
            self.library.remove_at(GUILD, "ghost", 1),
        ):
            with self.assertRaises(NoSuchPlaylist):
                await call


class TestTheSchemaHoldsTheInvariants(unittest.IsolatedAsyncioTestCase):
    """The rules that used to be Python are now the database's job.

    These reach into the connection on purpose: the point is that the *tables*
    enforce this, not the code above them.
    """

    def setUp(self):
        self.library = PlaylistLibrary(_fresh_db())

    def tearDown(self):
        self.library.close()

    def _count(self, sql, *args):
        return self.library._db.execute(sql, args).fetchone()[0]

    async def test_two_playlists_cannot_share_a_folded_name(self):
        await self.library.create(GUILD, "Chill")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.library._db:
                self.library._db.execute(
                    "INSERT INTO playlists "
                    "(guild_id, name, name_key, created_at, updated_at) "
                    "VALUES (?, ?, ?, 0, 0)",
                    (GUILD, "CHILL", fold("CHILL")),
                )

    async def test_deleting_a_playlist_takes_its_songs_with_it(self):
        """ON DELETE CASCADE, rather than a second statement we might forget."""
        await self.library.create(GUILD, "Mix")
        await self.library.extend(GUILD, "Mix", _saved(5))
        self.assertEqual(self._count("SELECT COUNT(*) FROM tracks"), 5)

        await self.library.delete(GUILD, "Mix")
        self.assertEqual(self._count("SELECT COUNT(*) FROM tracks"), 0)

    async def test_positions_stay_contiguous_after_a_removal(self):
        """The gap is closed, or the next insert collides with a stale index."""
        await self.library.create(GUILD, "Mix")
        await self.library.extend(GUILD, "Mix", _saved(5))
        await self.library.remove_at(GUILD, "Mix", 2)

        positions = [
            row[0]
            for row in self.library._db.execute(
                "SELECT position FROM tracks ORDER BY position"
            )
        ]
        self.assertEqual(positions, [0, 1, 2, 3])

    async def test_adding_after_a_removal_does_not_collide(self):
        await self.library.create(GUILD, "Mix")
        await self.library.extend(GUILD, "Mix", _saved(5))
        await self.library.remove_at(GUILD, "Mix", 1)
        playlist, added = await self.library.extend(
            GUILD, "Mix", [SavedTrack("https://y/new", "New", 10)]
        )
        self.assertEqual(added, 1)
        self.assertEqual(playlist.tracks[-1].title, "New")

    async def test_removing_the_last_song_is_fine(self):
        await self.library.create(GUILD, "Mix")
        await self.library.extend(GUILD, "Mix", _saved(3))
        playlist, _ = await self.library.remove_at(GUILD, "Mix", 3)
        self.assertEqual([t.title for t in playlist.tracks], ["Song 0", "Song 1"])

    async def test_a_database_error_surfaces_as_a_storage_error(self):
        """The cog reports this; it must not escape as a raw sqlite3 error."""
        await self.library.create(GUILD, "Mix")
        self.library.close()  # the database has gone out from under us
        with self.assertRaises(StorageError):
            await self.library.summaries(GUILD)


class TestPlaylistPersistence(unittest.IsolatedAsyncioTestCase):
    """A playlist that does not survive a restart is not one."""

    def setUp(self):
        self.path = _fresh_db()

    async def test_a_library_reopens_with_everything_in_it(self):
        library = PlaylistLibrary(self.path)
        await library.create(GUILD, "Late Night", created_by=7)
        await library.extend(GUILD, "Late Night", _saved(3, seconds=90))
        library.close()

        reopened = PlaylistLibrary(self.path)
        self.addCleanup(reopened.close)
        playlist = await reopened.find(GUILD, "late night")
        self.assertEqual(playlist.name, "Late Night")
        self.assertEqual(
            [t.title for t in playlist.tracks], ["Song 0", "Song 1", "Song 2"]
        )
        self.assertEqual(playlist.duration, 270)
        self.assertEqual(playlist.created_by, 7)

    async def test_opening_creates_the_folder_it_needs(self):
        library = PlaylistLibrary(self.path)
        self.addCleanup(library.close)
        self.assertTrue(self.path.is_file())

    async def test_a_corrupt_database_is_moved_aside_not_overwritten(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"this is definitely not a sqlite file" * 10)

        library = PlaylistLibrary(self.path)
        self.addCleanup(library.close)

        self.assertEqual(await library.summaries(GUILD), [])
        spoiled = [p for p in self.path.parent.iterdir() if "corrupt" in p.name]
        self.assertEqual(len(spoiled), 1)
        self.assertIn(b"not a sqlite file", spoiled[0].read_bytes())


class TestImportingTheOldJsonStore(unittest.IsolatedAsyncioTestCase):
    """The store used to be a JSON file. Nobody should lose it on upgrade."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="playlist-test-"))
        self.db = self.dir / "playlists.db"
        self.legacy = self.dir / "playlists.json"

    def _write(self, payload):
        self.legacy.write_text(json.dumps(payload), encoding="utf-8")

    @staticmethod
    def _entry(name, songs=1, created_by=None):
        return {
            "name": name,
            "created_by": created_by,
            "tracks": [
                {"url": f"https://y/{name}{i}", "title": f"{name} {i}", "duration": 60}
                for i in range(songs)
            ],
        }

    def _open(self):
        library = PlaylistLibrary(self.db)
        self.addCleanup(library.close)
        return library

    async def test_a_version_3_file_is_imported(self):
        self._write({"version": 3, "guilds": {"1": [self._entry("Party", 3, 42)]}})
        library = self._open()
        playlist = await library.find(GUILD, "Party")
        self.assertEqual(len(playlist.tracks), 3)
        self.assertEqual(playlist.created_by, 42)

    async def test_the_imported_file_is_kept_not_deleted(self):
        self._write({"version": 3, "guilds": {"1": [self._entry("Party")]}})
        self._open()
        self.assertFalse(self.legacy.exists())
        kept = [p for p in self.dir.iterdir() if "imported" in p.name]
        self.assertEqual(len(kept), 1)
        self.assertIn("Party", kept[0].read_text(encoding="utf-8"))

    async def test_a_version_2_file_imports_only_the_server_playlists(self):
        self._write(
            {
                "version": 2,
                "owners": {
                    "guild:1": [self._entry("Party")],
                    "user:42": [self._entry("Private")],
                },
            }
        )
        library = self._open()
        self.assertEqual([p.name for p in await library.summaries(GUILD)], ["Party"])

    async def test_a_version_1_file_has_nothing_to_import(self):
        """Everything in one belonged to a person, not a server."""
        self._write({"version": 1, "users": {"42": [self._entry("Old")]}})
        library = self._open()
        self.assertEqual(await library.summaries(GUILD), [])
        self.assertFalse(self.legacy.exists())

    async def test_one_malformed_row_costs_that_row_and_nothing_else(self):
        self._write(
            {
                "version": 3,
                "guilds": {
                    "1": [
                        {
                            "name": "Mix",
                            "tracks": [
                                {"url": "https://y/a", "title": "A", "duration": 10},
                                {"title": "no url"},
                                "not even an object",
                                {"url": "https://y/b", "title": "B", "duration": "x"},
                            ],
                        }
                    ]
                },
            }
        )
        library = self._open()
        playlist = await library.find(GUILD, "Mix")
        self.assertEqual([t.title for t in playlist.tracks], ["A", "B"])
        self.assertEqual(playlist.tracks[1].duration, 0)

    async def test_an_unreadable_file_leaves_the_database_alone(self):
        self.legacy.write_text("{ not json", encoding="utf-8")
        library = self._open()
        self.assertEqual(await library.summaries(GUILD), [])
        # Left in place rather than renamed: it was not imported, so it is
        # still the only copy of whatever it holds.
        self.assertTrue(self.legacy.exists())

    async def test_a_second_startup_does_not_import_twice(self):
        self._write({"version": 3, "guilds": {"1": [self._entry("Party", 2)]}})
        first = self._open()
        self.assertEqual(len(await first.summaries(GUILD)), 1)
        first.close()

        again = self._open()
        self.assertEqual(len(await again.summaries(GUILD)), 1)

    async def test_a_json_file_appearing_later_is_not_merged_in(self):
        """The database is already the source of truth by then."""
        library = self._open()
        await library.create(GUILD, "Live One")
        library.close()

        self._write({"version": 3, "guilds": {"1": [self._entry("Stale")]}})
        again = self._open()
        self.assertEqual([p.name for p in await again.summaries(GUILD)], ["Live One"])
        self.assertTrue(self.legacy.exists())


class _PlaylistCogTestCase(unittest.IsolatedAsyncioTestCase):
    """Shared rig: a cog wired to a temp database and a fake extractor."""

    def setUp(self):
        from music_player.cogs.player import Player
        from music_player.cogs.playlists import Playlists

        self.library = PlaylistLibrary(_fresh_db())
        self.addCleanup(self.library.close)
        self.youtube = FakeYouTube()
        self.youtube.fetch = self._fetch
        self.entries = 3
        self.unavailable = 0

        self.music = MusicState()
        self.player = Player(MagicMock(), self.music, self.youtube)
        self.cog = Playlists(
            MagicMock(), self.music, self.youtube, self.library, self.player
        )
        self.author = FakeUser()
        self.member = FakeUser(7, "someone-else")
        self.mod = FakeUser(8, "a-mod", manage_guild=True)

    async def _fetch(self, url):
        return FetchResult(
            entries=[
                TrackInfo(f"https://y/f{i}", f"Fetched {i}", 100)
                for i in range(self.entries)
            ],
            playlist_title="Road Trip" if self.entries > 1 else None,
            unavailable=self.unavailable,
        )

    def ctx(self, author=None, guild_id=GUILD):
        context = FakeContext(FakeChannel())
        context.author = author or self.author
        context.guild = FakeGuild(guild_id) if guild_id else None
        return context

    @staticmethod
    def last(ctx):
        return ctx.sent[-1]["embed"]

    async def songs_in(self, name, guild_id=GUILD):
        playlist = await self.library.find(guild_id, name)
        return None if playlist is None else len(playlist.tracks)


class TestPlaylistCommands(_PlaylistCogTestCase):
    async def test_create_then_list_shows_it(self):
        ctx = self.ctx()
        await self.cog.create.callback(self.cog, ctx, name="Late Night")
        await self.cog.playlist.callback(self.cog, ctx)
        self.assertIn("Late Night", self.last(ctx).description)

    async def test_creating_writes_it_immediately(self):
        await self.cog.create.callback(self.cog, self.ctx(), name="Mix")
        self.assertIsNotNone(await self.library.find(GUILD, "Mix"))

    async def test_the_creator_is_recorded_for_the_permission_check(self):
        await self.cog.create.callback(self.cog, self.ctx(), name="Mix")
        self.assertEqual((await self.library.find(GUILD, "Mix")).created_by, 42)

    async def test_a_database_failure_is_reported_and_changes_nothing(self):
        ctx = self.ctx()
        self.library.close()  # the database has gone out from under us
        await self.cog.create.callback(self.cog, ctx, name="Mix")

        self.assertEqual(len(ctx.sent), 1)
        self.assertIn("Nothing was changed", self.last(ctx).description)

        # And the claim holds when somebody comes back to look.
        recovered = PlaylistLibrary(self.library.path)
        self.addCleanup(recovered.close)
        self.assertEqual(await recovered.summaries(GUILD), [])

    async def test_an_empty_library_says_how_to_start_one(self):
        ctx = self.ctx()
        await self.cog.playlist.callback(self.cog, ctx)
        self.assertIn("?playlist create", self.last(ctx).description)

    async def test_add_puts_every_fetched_song_in(self):
        ctx = self.ctx()
        await self.cog.create.callback(self.cog, ctx, name="Mix")
        await self.cog.add.callback(self.cog, ctx, "mix", link="https://y/pl")
        self.assertEqual(await self.songs_in("Mix"), 3)
        self.assertIn("Added 3 songs", self.last(ctx).author.name)

    async def test_the_confirmation_counts_what_the_playlist_now_holds(self):
        """It quotes the row the write returned, not the one looked up before."""
        ctx = self.ctx()
        await self.cog.create.callback(self.cog, ctx, name="Mix")
        await self.cog.add.callback(self.cog, ctx, "Mix", link="https://y/pl")
        await self.cog.add.callback(self.cog, ctx, "Mix", link="https://y/pl")
        self.assertIn("6 songs", self.last(ctx).footer.text)

    async def test_add_names_a_playlist_that_does_not_exist(self):
        ctx = self.ctx()
        await self.cog.add.callback(self.cog, ctx, "ghost", link="https://y/x")
        self.assertIn("This server doesn't have", self.last(ctx).description)

    async def test_a_bad_link_does_not_touch_the_playlist(self):
        ctx = self.ctx()
        await self.cog.create.callback(self.cog, ctx, name="Mix")

        async def boom(url):
            raise ExtractionError("nope")

        self.youtube.fetch = boom
        await self.cog.add.callback(self.cog, ctx, "Mix", link="https://y/x")
        self.assertEqual(await self.songs_in("Mix"), 0)
        self.assertIn("couldn't read that link", self.last(ctx).description)

    async def test_a_partial_add_says_how_many_did_not_fit(self):
        ctx = self.ctx()
        await self.library.create(GUILD, "Mix")
        await self.library.extend(GUILD, "Mix", _saved(MAX_PLAYLIST_TRACKS - 1))
        await self.cog.add.callback(self.cog, ctx, "Mix", link="https://y/pl")
        self.assertIn("2 more didn't fit", self.last(ctx).description)

    async def test_unavailable_videos_are_stated(self):
        ctx = self.ctx()
        self.unavailable = 4
        await self.library.create(GUILD, "Mix")
        await self.cog.add.callback(self.cog, ctx, "Mix", link="https://y/pl")
        self.assertIn("Skipped 4 unavailable", self.last(ctx).description)

    async def test_remove_takes_the_numbered_song_out(self):
        ctx = self.ctx()
        await self.library.create(GUILD, "Mix")
        await self.library.extend(GUILD, "Mix", _saved(3))
        await self.cog.remove.callback(self.cog, ctx, "Mix", 2)
        playlist = await self.library.find(GUILD, "Mix")
        self.assertEqual([t.title for t in playlist.tracks], ["Song 0", "Song 2"])

    async def test_a_bad_song_number_states_the_real_range(self):
        ctx = self.ctx()
        await self.library.create(GUILD, "Mix")
        await self.library.extend(GUILD, "Mix", _saved(3))
        await self.cog.remove.callback(self.cog, ctx, "Mix", 9)
        self.assertIn("**3**", self.last(ctx).description)

    async def test_rename_reports_both_names(self):
        ctx = self.ctx()
        await self.library.create(GUILD, "Old", created_by=42)
        await self.cog.rename.callback(self.cog, ctx, "Old", new="New")
        self.assertIn("Old", self.last(ctx).description)
        self.assertIn("New", self.last(ctx).description)

    async def test_delete_removes_it_for_good(self):
        ctx = self.ctx()
        await self.library.create(GUILD, "Mix", created_by=42)
        await self.cog.delete.callback(self.cog, ctx, name="mix")
        self.assertEqual(await self.library.summaries(GUILD), [])

    async def test_show_opens_the_browser_on_that_playlist(self):
        ctx = self.ctx()
        await self.library.create(GUILD, "Mix")
        await self.library.extend(GUILD, "Mix", _saved(2))
        await self.cog.show.callback(self.cog, ctx, name="mix")
        self.assertEqual(ctx.sent[-1]["view"].selected.name, "Mix")
        self.assertEqual(self.last(ctx).title, "Mix")

    async def test_autocomplete_offers_this_servers_playlists(self):
        await self.library.create(GUILD, "Chill Vibes")
        await self.library.create(GUILD, "Gym")
        await self.library.create(OTHER_GUILD, "Another Server's")
        interaction = FakeInteraction(self.author, FakeChannel())
        choices = await self.cog.playlist_name_autocomplete(interaction, "vib")
        self.assertEqual([c.value for c in choices], ["Chill Vibes"])

    async def test_autocomplete_with_nothing_typed_offers_everything(self):
        await self.library.create(GUILD, "One")
        await self.library.create(GUILD, "Two")
        interaction = FakeInteraction(self.author, FakeChannel())
        choices = await self.cog.playlist_name_autocomplete(interaction, "")
        self.assertEqual([c.value for c in choices], ["One", "Two"])

    async def test_autocomplete_in_a_dm_offers_nothing(self):
        interaction = FakeInteraction(self.author, FakeChannel(), guild_id=None)
        self.assertEqual(
            await self.cog.playlist_name_autocomplete(interaction, ""), []
        )

    async def test_autocomplete_stays_quiet_when_the_database_fails(self):
        """There is no way to show an error in an autocomplete dropdown."""
        interaction = FakeInteraction(self.author, FakeChannel())
        self.library.close()
        self.assertEqual(
            await self.cog.playlist_name_autocomplete(interaction, ""), []
        )


class TestPlaylistsAreTheServers(_PlaylistCogTestCase):
    """A playlist made in server A is reachable only from server A."""

    async def test_another_server_cannot_see_it(self):
        await self.cog.create.callback(self.cog, self.ctx(), name="Ours")
        elsewhere = self.ctx(guild_id=OTHER_GUILD)
        await self.cog.playlist.callback(self.cog, elsewhere)
        self.assertIn("hasn't saved any playlists", self.last(elsewhere).description)

    async def test_another_server_cannot_play_it(self):
        await self.library.create(GUILD, "Ours", created_by=42)
        await self.library.extend(GUILD, "Ours", _saved(3))
        elsewhere = self.ctx(guild_id=OTHER_GUILD)
        await self.cog.play.callback(self.cog, elsewhere, name="Ours")
        self.assertEqual(self.music.get(OTHER_GUILD).queue, [])
        self.assertIn("This server doesn't have", self.last(elsewhere).description)

    async def test_another_server_cannot_delete_it(self):
        await self.library.create(GUILD, "Ours", created_by=42)
        elsewhere = self.ctx(guild_id=OTHER_GUILD)
        await self.cog.delete.callback(self.cog, elsewhere, name="Ours")
        self.assertIsNotNone(await self.library.find(GUILD, "Ours"))

    async def test_everyone_in_the_server_sees_the_same_list(self):
        await self.cog.create.callback(self.cog, self.ctx(), name="Party Mix")
        ctx = self.ctx(self.member)
        await self.cog.playlist.callback(self.cog, ctx)
        self.assertIn("Party Mix", self.last(ctx).description)

    async def test_the_overview_is_headed_by_the_server(self):
        await self.cog.create.callback(self.cog, self.ctx(), name="Party Mix")
        ctx = self.ctx()
        await self.cog.playlist.callback(self.cog, ctx)
        self.assertEqual(self.last(ctx).author.name, "Test Server")

    async def test_someone_else_can_add_songs(self):
        await self.cog.create.callback(self.cog, self.ctx(), name="Party Mix")
        ctx = self.ctx(self.member)
        await self.cog.add.callback(self.cog, ctx, "party mix", link="https://y/pl")
        self.assertEqual(await self.songs_in("Party Mix"), 3)

    async def test_someone_else_can_remove_songs(self):
        await self.library.create(GUILD, "Party Mix", created_by=42)
        await self.library.extend(GUILD, "Party Mix", _saved(3))
        await self.cog.remove.callback(self.cog, self.ctx(self.member), "Party Mix", 1)
        self.assertEqual(await self.songs_in("Party Mix"), 2)

    async def test_someone_else_cannot_delete_it(self):
        await self.library.create(GUILD, "Party Mix", created_by=42)
        ctx = self.ctx(self.member)
        await self.cog.delete.callback(self.cog, ctx, name="Party Mix")
        self.assertIsNotNone(await self.library.find(GUILD, "Party Mix"))
        self.assertIn("isn't yours", self.last(ctx).description)

    async def test_the_refusal_says_what_they_can_still_do(self):
        """Otherwise it reads as though the playlists are not shared at all."""
        await self.library.create(GUILD, "Party Mix", created_by=42)
        ctx = self.ctx(self.member)
        await self.cog.delete.callback(self.cog, ctx, name="Party Mix")
        self.assertIn("?playlist add", self.last(ctx).description)

    async def test_someone_else_cannot_rename_it(self):
        await self.library.create(GUILD, "Party Mix", created_by=42)
        await self.cog.rename.callback(
            self.cog, self.ctx(self.member), "Party Mix", new="Mine Now"
        )
        self.assertIsNotNone(await self.library.find(GUILD, "Party Mix"))

    async def test_the_creator_can_delete_it(self):
        await self.library.create(GUILD, "Party Mix", created_by=42)
        await self.cog.delete.callback(self.cog, self.ctx(), name="Party Mix")
        self.assertIsNone(await self.library.find(GUILD, "Party Mix"))

    async def test_a_moderator_can_delete_someone_elses(self):
        await self.library.create(GUILD, "Party Mix", created_by=42)
        await self.cog.delete.callback(self.cog, self.ctx(self.mod), name="Party Mix")
        self.assertIsNone(await self.library.find(GUILD, "Party Mix"))

    async def test_a_moderator_can_rename_someone_elses(self):
        await self.library.create(GUILD, "Party Mix", created_by=42)
        await self.cog.rename.callback(
            self.cog, self.ctx(self.mod), "Party Mix", new="House Rules"
        )
        self.assertIsNotNone(await self.library.find(GUILD, "House Rules"))

    async def test_a_playlist_with_no_recorded_creator_is_moderators_only(self):
        """A hand-edited row, or one imported from an older store."""
        await self.library.create(GUILD, "Legacy")
        await self.cog.delete.callback(self.cog, self.ctx(), name="Legacy")
        self.assertIsNotNone(await self.library.find(GUILD, "Legacy"))

        await self.cog.delete.callback(self.cog, self.ctx(self.mod), name="Legacy")
        self.assertIsNone(await self.library.find(GUILD, "Legacy"))

    async def test_a_dm_is_refused_by_the_cog_check(self):
        ctx = self.ctx(guild_id=None)
        with self.assertRaises(commands.NoPrivateMessage):
            await self.cog.cog_check(ctx)

    async def test_the_dm_refusal_explains_why(self):
        ctx = self.ctx()
        ctx.command = self.cog.create
        await self.cog.cog_command_error(ctx, commands.NoPrivateMessage())
        self.assertIn("live in a server", self.last(ctx).description)


class TestPlaylistIntoTheQueue(_PlaylistCogTestCase):
    """Loading a playlist is a copy into the guild queue, not a link to it."""

    async def asyncSetUp(self):
        await self.library.create(GUILD, "Mix", created_by=42)
        await self.library.extend(GUILD, "Mix", _saved(3))
        self.state = self.music.get(GUILD)

    async def test_queue_appends_and_leaves_what_was_waiting(self):
        self.state.queue.append(Track("https://y/old", "Old", 60, 1, "u"))
        ctx = self.ctx()
        await self.cog.enqueue.callback(self.cog, ctx, name="Mix")
        self.assertEqual(
            [t.title for t in self.state.queue],
            ["Old", "Song 0", "Song 1", "Song 2"],
        )
        self.assertIn("#2 in queue", self.last(ctx).footer.text)

    async def test_play_replaces_an_idle_queue(self):
        self.state.queue.append(Track("https://y/old", "Old", 60, 1, "u"))
        await self.cog.play.callback(self.cog, self.ctx(), name="Mix")
        self.assertEqual(
            [t.title for t in self.state.queue], ["Song 0", "Song 1", "Song 2"]
        )

    async def test_play_says_the_queue_was_replaced(self):
        ctx = self.ctx()
        await self.cog.play.callback(self.cog, ctx, name="Mix")
        self.assertIn("queue was replaced", self.last(ctx).footer.text)

    async def test_play_without_a_voice_connection_says_how_to_get_one(self):
        ctx = self.ctx()
        await self.cog.play.callback(self.cog, ctx, name="Mix")
        self.assertIn("/join", self.last(ctx).description)

    async def test_play_over_a_live_track_jumps_into_the_playlist(self):
        """The song on air is skipped past, not left at the head of the queue."""
        self.state.voice = FakeVoice()
        self.state.voice.playing = True
        self.state.mark_started()
        self.state.queue.extend(
            [
                Track("https://y/live", "Live", 300, 1, "u"),
                Track("https://y/waiting", "Waiting", 90, 1, "u"),
            ]
        )
        await self.cog.play.callback(self.cog, self.ctx(), name="Mix")
        self.assertEqual(
            [t.title for t in self.state.queue], ["Song 0", "Song 1", "Song 2"]
        )

    async def test_the_loaded_tracks_credit_whoever_asked(self):
        await self.cog.play.callback(self.cog, self.ctx(self.member), name="Mix")
        self.assertTrue(all(t.requester_id == 7 for t in self.state.queue))

    async def test_editing_the_queue_does_not_edit_the_playlist(self):
        await self.cog.play.callback(self.cog, self.ctx(), name="Mix")
        self.state.queue.clear()
        self.assertEqual(await self.songs_in("Mix"), 3)

    async def test_an_empty_playlist_is_not_loaded(self):
        ctx = self.ctx()
        await self.library.create(GUILD, "Empty")
        await self.cog.play.callback(self.cog, ctx, name="Empty")
        self.assertEqual(self.state.queue, [])
        self.assertIn("is empty", self.last(ctx).description)

    async def test_each_server_gets_its_own_queue(self):
        await self.library.create(OTHER_GUILD, "Theirs", created_by=1)
        await self.library.extend(OTHER_GUILD, "Theirs", _saved(2))
        await self.cog.play.callback(self.cog, self.ctx(), name="Mix")
        await self.cog.play.callback(
            self.cog, self.ctx(guild_id=OTHER_GUILD), name="Theirs"
        )
        self.assertEqual(len(self.music.get(GUILD).queue), 3)
        self.assertEqual(len(self.music.get(OTHER_GUILD).queue), 2)

    async def _drain(self):
        """Empty the queue the way the voice after-callback does.

        ``_on_track_end`` schedules one ``_advance`` per finished track; the
        last one clears the queue. Calling it directly exercises that same
        path without needing real audio.
        """
        channel = FakeChannel()
        while self.state.queue:
            await self.player._advance(
                channel, self.state, forced=False, expect=self.state.current
            )
        self.state.cancel_idle_disconnect()

    async def test_playing_a_playlist_to_the_end_does_not_touch_it(self):
        """The queue empties itself when the last song finishes.

        The playlist it was loaded from has to be exactly as it was. Loading is
        a copy, so nothing in the playback path can reach the stored songs, and
        this is the test that says so.
        """
        await self.cog.play.callback(self.cog, self.ctx(), name="Mix")
        self.assertEqual(len(self.state.queue), 3)

        await self._drain()

        self.assertEqual(self.state.queue, [])
        self.assertEqual(await self.songs_in("Mix"), 3)

    async def test_the_playlist_can_be_played_again_afterwards(self):
        """The real proof that nothing was consumed: do it twice."""
        await self.cog.play.callback(self.cog, self.ctx(), name="Mix")
        await self._drain()

        await self.cog.play.callback(self.cog, self.ctx(), name="Mix")
        self.assertEqual(
            [t.title for t in self.state.queue], ["Song 0", "Song 1", "Song 2"]
        )

    async def test_stop_clears_the_queue_and_leaves_the_playlist(self):
        await self.cog.play.callback(self.cog, self.ctx(), name="Mix")
        await self.player.stop.callback(self.player, self.ctx())
        self.assertEqual(self.state.queue, [])
        self.assertEqual(await self.songs_in("Mix"), 3)

    async def test_clear_leaves_the_playlist(self):
        await self.cog.play.callback(self.cog, self.ctx(), name="Mix")
        await self.player.clear.callback(self.player, self.ctx())
        self.assertEqual(self.state.queue, [])
        self.assertEqual(await self.songs_in("Mix"), 3)

    async def test_skipping_through_every_song_leaves_the_playlist(self):
        await self.cog.play.callback(self.cog, self.ctx(), name="Mix")
        channel = FakeChannel()
        for _ in range(3):
            await self.player.perform_skip(channel, self.state)
        self.state.cancel_idle_disconnect()
        self.assertEqual(await self.songs_in("Mix"), 3)



class TestWhereTheSongCameFrom(_PlaylistCogTestCase):
    """A queued song remembers the playlist it arrived with, and says so."""

    async def asyncSetUp(self):
        await self.library.create(GUILD, "Late Night", created_by=42)
        await self.library.extend(GUILD, "Late Night", _saved(3))
        self.state = self.music.get(GUILD)

    async def test_loading_a_playlist_stamps_every_song(self):
        await self.cog.play.callback(self.cog, self.ctx(), name="Late Night")
        self.assertTrue(all(t.source == "Late Night" for t in self.state.queue))

    async def test_the_name_is_snapshotted_not_looked_up(self):
        """Renaming the playlist must not rewrite history in the queue."""
        await self.cog.play.callback(self.cog, self.ctx(), name="Late Night")
        await self.library.rename(GUILD, "Late Night", "Something Else")
        self.assertEqual(self.state.queue[0].source, "Late Night")

    async def test_a_song_added_on_its_own_has_no_source(self):
        self.entries = 1
        ctx = self.ctx()
        await self.player.add.callback(self.player, ctx, url="https://y/one")
        self.assertIsNone(self.state.queue[-1].source)

    async def test_a_youtube_playlist_link_names_itself(self):
        """?add of a playlist link is the other way a song arrives in a group."""
        ctx = self.ctx()
        await self.player.add.callback(self.player, ctx, url="https://y/pl")
        self.assertTrue(all(t.source == "Road Trip" for t in self.state.queue))

    async def test_now_playing_shows_where_it_came_from(self):
        await self.cog.play.callback(self.cog, self.ctx(), name="Late Night")
        # Nothing is actually on air without a voice client, so build the
        # snapshot the same way _play_current would.
        snapshot = ui.NowPlaying(
            title="x", url="y", duration=10, thumbnail=None, requester=None,
            volume=0.1, position=1, total=3, up_next=None, remaining=30,
            source=self.state.queue[0].source,
        )
        rendered = ui.now_playing(snapshot)
        field = next(f for f in rendered.fields if f.name == "From")
        self.assertIn("Late Night", field.value)

    async def test_the_from_field_is_absent_without_a_source(self):
        rendered = ui.now_playing(
            ui.NowPlaying(
                title="x", url="y", duration=10, thumbnail=None, requester=None,
                volume=0.1, position=1, total=1, up_next=None, remaining=10,
            )
        )
        self.assertNotIn("From", [f.name for f in rendered.fields])

    async def test_the_queue_names_it_on_the_live_track(self):
        await self.cog.play.callback(self.cog, self.ctx(), name="Late Night")
        rendered = ui.queue_page(self.state.queue, 1, status=ui.PLAYING_MARKER)
        self.assertIn("from **Late Night**", rendered.description)

    async def test_the_listing_marks_the_playlist_that_is_on_air(self):
        await self.cog.play.callback(self.cog, self.ctx(), name="Late Night")
        await self.library.create(GUILD, "Gym", created_by=42)

        ctx = self.ctx()
        await self.cog.playlist.callback(self.cog, ctx)
        rows = self.last(ctx).description.splitlines()

        self.assertIn(ui.PLAYING_MARKER, rows[0])
        self.assertNotIn(ui.PLAYING_MARKER, rows[1])

    async def test_the_listing_marks_nothing_when_nothing_plays(self):
        ctx = self.ctx()
        await self.cog.playlist.callback(self.cog, ctx)
        self.assertNotIn(ui.PLAYING_MARKER, self.last(ctx).description)

    async def test_the_listing_credits_whoever_made_each_one(self):
        ctx = self.ctx()
        await self.cog.playlist.callback(self.cog, ctx)
        self.assertIn("<@42>", self.last(ctx).description)

    async def test_the_marker_follows_a_rename_of_the_live_playlist(self):
        """The queue keeps the old name, so the renamed row is not marked."""
        await self.cog.play.callback(self.cog, self.ctx(), name="Late Night")
        await self.library.rename(GUILD, "Late Night", "Something Else")

        ctx = self.ctx()
        await self.cog.playlist.callback(self.cog, ctx)
        self.assertNotIn(ui.PLAYING_MARKER, self.last(ctx).description)

    async def test_the_playlist_page_marks_the_song_on_air(self):
        await self.cog.play.callback(self.cog, self.ctx(), name="Late Night")
        playlist = await self.library.require(GUILD, "Late Night")
        rendered = ui.playlist_page(
            playlist, 1, playing_url=self.state.queue[0].url
        )
        rows = rendered.description.splitlines()
        self.assertTrue(rows[0].endswith(ui.PLAYING_MARKER))
        self.assertFalse(rows[1].endswith(ui.PLAYING_MARKER))

    async def test_the_page_marks_nothing_when_nothing_matches(self):
        playlist = await self.library.require(GUILD, "Late Night")
        rendered = ui.playlist_page(playlist, 1, playing_url="https://y/elsewhere")
        self.assertNotIn(ui.PLAYING_MARKER, rendered.description)

class TestPlaylistBrowserView(_PlaylistCogTestCase):
    async def asyncSetUp(self):
        from music_player.ui.views import PlaylistBrowser

        await self.library.create(GUILD, "Chill", created_by=42)
        await self.library.extend(GUILD, "Chill", _saved(14))
        await self.library.create(GUILD, "Empty", created_by=42)
        self.channel = FakeChannel()
        self.view = PlaylistBrowser(
            self.cog, self.author, FakeGuild(), await self.library.summaries(GUILD)
        )

    def _select(self, index: int):
        self.view.choose._values = [str(index)]

    async def test_the_dropdown_lists_every_playlist(self):
        self.assertEqual(
            [option.label for option in self.view.choose.options], ["Chill", "Empty"]
        )

    async def test_the_menu_holds_no_songs_until_one_is_picked(self):
        """The listing reads a GROUP BY, not every track in the server.

        Opening the menu on a full server used to materialise a quarter of a
        million rows to print two numbers per line.
        """
        self.assertEqual([s.songs for s in self.view.summaries], [14, 0])
        self.assertFalse(any(hasattr(s, "tracks") for s in self.view.summaries))
        self.assertIsNone(self.view.selected)

    async def test_picking_one_reads_its_songs(self):
        self._select(0)
        await self.view._on_choose(FakeInteraction(self.author, self.channel))
        self.assertEqual(len(self.view.selected.tracks), 14)

    async def test_picking_one_that_was_just_deleted_says_so(self):
        """The menu outlives the query it was built from."""
        await self.library.delete(GUILD, "Chill")

        interaction = FakeInteraction(self.author, self.channel)
        self._select(0)
        await self.view._on_choose(interaction)

        self.assertIsNone(self.view.selected)
        self.assertEqual(interaction.response.edits, [])
        self.assertIn(
            "doesn't have a playlist",
            interaction.response.messages[0]["embed"].description,
        )

    async def test_the_landing_page_is_headed_by_the_server(self):
        self.assertEqual(self.view.render().author.name, "Test Server")

    async def test_nothing_is_playable_until_something_is_picked(self):
        self.assertTrue(self.view.play.disabled)
        self.assertTrue(self.view.enqueue.disabled)

    async def test_picking_one_opens_it(self):
        interaction = FakeInteraction(self.author, self.channel)
        self._select(0)
        await self.view._on_choose(interaction)
        embed = interaction.response.edits[0]["embed"]
        self.assertEqual(embed.title, "Chill")
        self.assertIn("Page 1/2", embed.footer.text)
        self.assertFalse(self.view.play.disabled)

    async def test_an_empty_playlist_cannot_be_played(self):
        self._select(1)
        await self.view._on_choose(FakeInteraction(self.author, self.channel))
        self.assertTrue(self.view.play.disabled)

    async def test_paging_stops_at_both_ends(self):
        self._select(0)
        await self.view._on_choose(FakeInteraction(self.author, self.channel))
        self.assertTrue(self.view.previous.disabled)

        await self.view.next.callback(FakeInteraction(self.author, self.channel))
        self.assertEqual(self.view.indicator.label, "2 / 2")
        self.assertTrue(self.view.next.disabled)
        self.assertFalse(self.view.previous.disabled)

    async def test_the_page_number_is_pressable_once_there_are_pages(self):
        """It used to be a dead read-out; 1,000 pages of arrows is not a UI."""
        self.assertTrue(self.view.indicator.disabled)  # nothing picked yet

        self._select(0)
        await self.view._on_choose(FakeInteraction(self.author, self.channel))
        self.assertFalse(self.view.indicator.disabled)
        self.assertEqual(self.view.indicator.label, "1 / 2")

    async def test_a_single_page_has_nowhere_to_jump_to(self):
        self._select(1)  # the empty playlist
        await self.view._on_choose(FakeInteraction(self.author, self.channel))
        self.assertTrue(self.view.indicator.disabled)

    async def test_jumping_moves_the_page(self):
        from music_player.ui.views import JumpToPage

        self._select(0)
        await self.view._on_choose(FakeInteraction(self.author, self.channel))

        modal = JumpToPage(self.view, 2)
        modal._number._value = "2"
        interaction = FakeInteraction(self.author, self.channel)
        await modal.on_submit(interaction)

        self.assertEqual(self.view.page, 2)
        self.assertIn("Page 2/2", interaction.response.edits[0]["embed"].footer.text)

    async def test_a_page_that_is_not_there_is_refused(self):
        from music_player.ui.views import JumpToPage

        self._select(0)
        await self.view._on_choose(FakeInteraction(self.author, self.channel))

        modal = JumpToPage(self.view, 2)
        modal._number._value = "99"
        interaction = FakeInteraction(self.author, self.channel)
        await modal.on_submit(interaction)

        self.assertEqual(self.view.page, 1)
        self.assertIn(
            "no page 99", interaction.response.messages[0]["embed"].description
        )

    async def test_something_that_is_not_a_number_is_refused(self):
        from music_player.ui.views import JumpToPage

        self._select(0)
        await self.view._on_choose(FakeInteraction(self.author, self.channel))

        modal = JumpToPage(self.view, 2)
        modal._number._value = "last one"
        interaction = FakeInteraction(self.author, self.channel)
        await modal.on_submit(interaction)

        self.assertEqual(self.view.page, 1)
        self.assertIn(
            "isn't a page number",
            interaction.response.messages[0]["embed"].description,
        )

    async def test_the_input_is_sized_to_the_page_count(self):
        """A four-digit playlist needs four digits of room."""
        from music_player.ui.views import JumpToPage

        self.assertEqual(JumpToPage(self.view, 1000)._number.max_length, 4)

    async def test_the_play_button_fills_the_queue(self):
        self._select(0)
        await self.view._on_choose(FakeInteraction(self.author, self.channel))
        interaction = FakeInteraction(self.author, self.channel)
        await self.view.play.callback(interaction)

        self.assertTrue(interaction.response.deferred)
        self.assertEqual(len(self.music.get(GUILD).queue), 14)
        posted = self.channel.sent[-1]["embed"]
        self.assertIn("Playing", posted.author.name)
        self.assertEqual(posted.title, "Chill")

    async def test_the_queue_button_appends_instead(self):
        self._select(0)
        await self.view._on_choose(FakeInteraction(self.author, self.channel))
        self.music.get(GUILD).queue.append(Track("https://y/old", "Old", 60, 1, "u"))
        await self.view.enqueue.callback(FakeInteraction(self.author, self.channel))
        self.assertEqual(self.music.get(GUILD).queue[0].title, "Old")
        self.assertEqual(len(self.music.get(GUILD).queue), 15)

    async def test_a_playlist_deleted_under_an_open_view_is_not_played(self):
        """The view outlives the command, and anyone here can delete."""
        self._select(0)
        await self.view._on_choose(FakeInteraction(self.author, self.channel))
        await self.library.delete(GUILD, "Chill")

        interaction = FakeInteraction(self.author, self.channel)
        await self.view.play.callback(interaction)

        self.assertEqual(self.music.get(GUILD).queue, [])
        self.assertIn(
            "doesn't have a playlist",
            interaction.response.messages[0]["embed"].description,
        )

    async def test_someone_elses_view_cannot_be_driven(self):
        """The playlists are shared; this particular message is not."""
        stranger = FakeInteraction(self.member, self.channel)
        self.assertFalse(await self.view.interaction_check(stranger))
        self.assertIn(
            "belongs to someone else",
            stranger.response.messages[0]["embed"].description,
        )

    async def test_the_owner_can_drive_it(self):
        mine = FakeInteraction(self.author, self.channel)
        self.assertTrue(await self.view.interaction_check(mine))


class TestPlaylistEmbeds(unittest.TestCase):
    """Playlist names come from users, and every dead end names its way out."""

    def test_a_name_cannot_break_the_layout_it_is_shown_in(self):
        rendered = ui.no_such_playlist("line one\nline two")
        self.assertNotIn("line one\nline two", rendered.description)
        self.assertIn("line one line two", rendered.description)

    def test_a_very_long_name_is_clipped(self):
        rendered = ui.no_such_playlist("x" * 400)
        self.assertLess(len(rendered.description), 200)

    def test_dead_ends_name_the_command_that_fixes_them(self):
        self.assertIn("?playlist create", ui.no_playlists().description)
        self.assertIn("?playlist", ui.no_such_playlist("x").description)
        self.assertIn("?playlist delete", ui.too_many_playlists(25).description)
        self.assertIn("?playlist remove", ui.playlist_full("x", 500).description)
        self.assertIn("?playlist add", ui.playlist_is_empty(Playlist("x")).description)
        self.assertIn(
            "?playlist show", ui.no_such_playlist_song("x", 9, 3).description
        )

    def test_every_message_speaks_of_the_server_not_the_person(self):
        for rendered in (ui.no_playlists(), ui.no_such_playlist("x")):
            self.assertIn("server", rendered.description.lower())
            self.assertNotIn("You don't have", rendered.description)

    def test_the_storage_failure_says_nothing_changed(self):
        """A rolled-back transaction leaves nothing half-applied to explain."""
        self.assertIn("Nothing was changed", ui.playlist_not_saved().description)

    def test_an_empty_playlist_is_told_apart_from_a_bad_number(self):
        self.assertIn("is empty", ui.no_such_playlist_song("x", 1, 0).description)

    def test_the_refusal_credits_whoever_started_the_playlist(self):
        rendered = ui.not_your_playlist(Playlist("Mix", created_by=77))
        self.assertIn("<@77>", rendered.description)

    def test_the_refusal_copes_with_no_recorded_creator(self):
        rendered = ui.not_your_playlist(Playlist("Mix"))
        self.assertIn("no recorded creator", rendered.description)
        self.assertIn("Manage Server", rendered.description)

    def test_big_counts_are_readable(self):
        """The cap is five digits now, so they carry separators."""
        playlist = Playlist("Mix", tracks=_saved(10_000))
        self.assertIn("10,000 songs", ui.playlist_page(playlist, 1).footer.text)
        self.assertIn("10,000 songs", ui.playlist_full("Mix", 10_000).description)

    def test_the_overview_counts_songs_and_time(self):
        playlist = Playlist("Mix", tracks=_saved(3, seconds=120))
        rendered = ui.playlist_overview([playlist], title="Playlists")
        self.assertIn("3 songs", rendered.description)
        self.assertIn("6 min", rendered.description)

    def test_an_empty_playlist_reads_as_empty_rather_than_zero(self):
        rendered = ui.playlist_overview([Playlist("Mix")], title="Playlists")
        self.assertIn("empty", rendered.description)

    def test_a_page_numbers_rows_the_way_remove_counts(self):
        playlist = Playlist("Mix", tracks=_saved(14))
        rendered = ui.playlist_page(playlist, 2)
        self.assertTrue(rendered.description.startswith("`11.`"))
        self.assertIn("Page 2/2", rendered.footer.text)

    def test_a_page_past_the_end_lands_on_the_last_one(self):
        playlist = Playlist("Mix", tracks=_saved(3))
        self.assertIn("Page 1/1", ui.playlist_page(playlist, 99).footer.text)

    def test_every_row_links_its_song(self):
        playlist = Playlist("Mix", tracks=_saved(3))
        for track in playlist.tracks:
            self.assertIn(f"(<{track.url}>)", ui.playlist_page(playlist, 1).description)

    def test_the_queue_confirmation_names_the_first_two_songs(self):
        """"40 songs" without saying which one starts leaves the obvious
        question unanswered."""
        playlist = Playlist("Late Night", tracks=_saved(3))
        rendered = ui.playlist_queued(playlist, 3, position=5, starts_in=740)

        self.assertEqual(rendered.title, "Late Night")
        self.assertEqual(
            [f.name for f in rendered.fields], ["First up", "Then"]
        )
        self.assertIn("Song 0", rendered.fields[0].value)
        self.assertIn("Song 1", rendered.fields[1].value)

    def test_the_play_confirmation_names_what_starts_and_what_follows(self):
        playlist = Playlist("Late Night", tracks=_saved(3))
        rendered = ui.playlist_playing(playlist, 3)
        self.assertEqual(
            [f.name for f in rendered.fields], ["Starting with", "Up next"]
        )

    def test_a_one_song_playlist_has_nothing_to_follow_with(self):
        playlist = Playlist("Solo", tracks=_saved(1))
        self.assertEqual(
            [f.name for f in ui.playlist_queued(playlist, 1).fields], ["First up"]
        )
        self.assertEqual(
            [f.name for f in ui.playlist_playing(playlist, 1).fields],
            ["Starting with"],
        )

    def test_both_confirmations_carry_the_cover_of_the_first_song(self):
        playlist = Playlist(
            "Late Night",
            tracks=[SavedTrack("https://youtu.be/dQw4w9WgXcQ", "A", 213)],
        )
        for rendered in (
            ui.playlist_queued(playlist, 1),
            ui.playlist_playing(playlist, 1),
        ):
            self.assertIn("dQw4w9WgXcQ", rendered.thumbnail.url)

    def test_a_playlist_of_non_youtube_links_still_renders(self):
        """artwork() returns None for anything it cannot key on."""
        playlist = Playlist("Odd", tracks=[SavedTrack("https://example/x", "A", 60)])
        rendered = ui.playlist_queued(playlist, 1)
        self.assertIsNone(rendered.thumbnail.url)
        self.assertEqual([f.name for f in rendered.fields], ["First up"])

    def test_the_play_confirmation_offers_the_non_destructive_command(self):
        playlist = Playlist("Mix", tracks=_saved(3))
        rendered = ui.playlist_playing(playlist, 3)
        self.assertIn("?playlist queue", rendered.footer.text)

    def test_creating_one_says_who_may_later_delete_it(self):
        rendered = ui.playlist_created(Playlist("Mix"))
        self.assertIn("Anyone in this server can add", rendered.description)
        self.assertIn("moderator", rendered.description)


class TestReachingDiscordCanFail(unittest.IsolatedAsyncioTestCase):
    """Not reaching Discord is not the same as Discord saying no.

    ``discord.HTTPException`` is Discord *answering* with an error. A TLS
    handshake it refuses, a dropped connection, a socket timeout - those
    arrive as aiohttp or socket errors, and a guard naming only the first
    lets them straight through the try that was meant to contain them.
    """

    @staticmethod
    def _ssl_failure():
        """The exact exception a refused TLS handshake to Discord raises."""
        import aiohttp

        return aiohttp.ClientConnectorSSLError(
            MagicMock(ssl=True, host="discord.com", port=443, is_ssl=True),
            OSError("[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] handshake failure"),
        )

    def test_the_failure_is_not_an_http_exception(self):
        """The premise: this is why the old guard missed it."""
        self.assertNotIsInstance(self._ssl_failure(), discord.HTTPException)

    def test_but_it_is_covered_now(self):
        from music_player.errors import DELIVERY_FAILED

        self.assertIsInstance(self._ssl_failure(), DELIVERY_FAILED)

    def test_every_shape_of_delivery_failure_is_covered(self):
        import aiohttp
        from music_player.errors import DELIVERY_FAILED

        cases = [
            self._ssl_failure(),
            aiohttp.ServerDisconnectedError(),          # not an OSError
            aiohttp.ClientPayloadError(),               # nor this
            TimeoutError(),                             # not an aiohttp error
            OSError("connection reset"),
            discord.HTTPException(MagicMock(status=500, reason="x"), "boom"),
        ]
        for exc in cases:
            self.assertIsInstance(exc, DELIVERY_FAILED, type(exc).__name__)

    async def test_a_lost_connection_does_not_stop_the_music(self):
        """The song is already playing by the time the announcement is sent."""
        from music_player.cogs.player import Player

        player = Player(MagicMock(), MusicState(), FakeYouTube())
        player.ffmpeg_path = "ffmpeg"

        state = player.state.get(1)
        state.voice = FakeVoice()
        state.queue.append(Track("https://youtu.be/dQw4w9WgXcQ", "A Song", 213, 1, "u"))

        channel = MagicMock()
        channel.send = AsyncMock(side_effect=self._ssl_failure())
        channel.guild = None

        source = MagicMock()
        source.wait_until_ready = AsyncMock()

        with patch.object(discord, "FFmpegPCMAudio", lambda *a, **k: MagicMock()),              patch("music_player.cogs.player.BufferedAudioSource",
                   lambda *a, **k: source),              patch.object(discord, "PCMVolumeTransformer", lambda s, volume=1.0: s):
            # Must not raise: the audio is already going.
            await player.start_queue(channel, state)

        self.addCleanup(state.cancel_idle_disconnect)
        self.assertTrue(state.voice.playing, "the song stopped over a failed message")


class TestErrorsStillReachTheUser(unittest.IsolatedAsyncioTestCase):
    """A dead interaction must not turn a failure into silence.

    Discord expects a slash command acknowledged within three seconds and
    answers ``10062 Unknown interaction`` after that. A gateway outage is
    exactly when it happens: events arrive late and the token is already
    stale, so the reply 404s. The channel is still there.
    """

    def _ctx(self, *, slash: bool, send_fails: bool):
        import app

        ctx = MagicMock()
        ctx.interaction = MagicMock() if slash else None
        ctx.send = AsyncMock()
        if send_fails:
            response = MagicMock(status=404, reason="Not Found")
            ctx.send.side_effect = discord.NotFound(response, "Unknown interaction")
        ctx.channel = MagicMock()
        ctx.channel.send = AsyncMock()
        return app.bot, ctx

    async def test_the_normal_path_replies_once(self):
        bot, ctx = self._ctx(slash=True, send_fails=False)
        await bot._report_failure(ctx)
        ctx.send.assert_awaited_once()
        ctx.channel.send.assert_not_awaited()

    async def test_a_dead_interaction_falls_back_to_the_channel(self):
        bot, ctx = self._ctx(slash=True, send_fails=True)
        await bot._report_failure(ctx)
        ctx.channel.send.assert_awaited_once()

    async def test_a_prefix_command_does_not_retry_the_same_route(self):
        """ctx.send already *was* the channel; a second go fails identically."""
        bot, ctx = self._ctx(slash=False, send_fails=True)
        await bot._report_failure(ctx)
        ctx.channel.send.assert_not_awaited()

    async def test_a_channel_that_also_refuses_is_survived(self):
        bot, ctx = self._ctx(slash=True, send_fails=True)
        response = MagicMock(status=403, reason="Forbidden")
        ctx.channel.send.side_effect = discord.Forbidden(response, "no")
        await bot._report_failure(ctx)  # must not raise


class TestSlashCommandSync(unittest.IsolatedAsyncioTestCase):
    """Publishing the slash commands.

    A global sync reaches every server but Discord can take an hour to roll it
    out, during which a command that was just added simply is not there.
    SYNC_GUILD_ID is the shortcut, and it must never be able to cost everyone
    else their commands.
    """

    def setUp(self):
        import app

        self.app = app
        self.tree = app.bot.tree

    async def _run(self, guild_id, sync=None):
        sync = sync or AsyncMock(return_value=[])
        copy = MagicMock()
        with patch.object(self.tree, "sync", sync), patch.object(
            self.tree, "copy_global_to", copy
        ), patch.object(self.app, "SYNC_GUILD_ID", guild_id):
            await self.app.bot._sync_commands()
        return sync, copy

    async def test_without_a_guild_id_only_the_global_sync_runs(self):
        sync, copy = await self._run(0)
        copy.assert_not_called()
        sync.assert_awaited_once_with()

    async def test_a_guild_id_publishes_there_as_well_as_globally(self):
        sync, copy = await self._run(123)
        copy.assert_called_once()
        self.assertEqual(copy.call_args.kwargs["guild"].id, 123)
        self.assertEqual(len(sync.await_args_list), 2)
        self.assertEqual(sync.await_args_list[0].kwargs["guild"].id, 123)
        # The global sync takes no guild at all.
        self.assertEqual(sync.await_args_list[1].kwargs, {})

    async def test_the_guild_sync_happens_first(self):
        """It is the one the person restarting the bot is waiting on."""
        sync, _copy = await self._run(123)
        self.assertIsNotNone(sync.await_args_list[0].kwargs.get("guild"))

    async def test_a_bad_guild_id_does_not_stop_the_global_sync(self):
        """One wrong id in .env must not leave every server without commands."""
        seen = []

        async def flaky(*, guild=None):
            seen.append(guild)
            if guild is not None:
                raise discord.HTTPException(
                    MagicMock(status=403, reason="Forbidden"), "not in that guild"
                )
            return []

        await self._run(999, sync=flaky)
        self.assertEqual([g.id if g else None for g in seen], [999, None])

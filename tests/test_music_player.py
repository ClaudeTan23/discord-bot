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
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

from music_player import ui  # noqa: E402
from music_player.audio import (  # noqa: E402
    FRAME_SIZE,
    BufferedAudioSource,
)
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
        embed = ui.added(track, extra_count=159, unavailable=39)
        self.assertIn("160", embed.author.name)
        # Discord subtext: present, but not competing with what was added.
        self.assertIn("-# Skipped 39 unavailable videos", embed.description)

    def test_a_single_dropped_video_is_not_pluralised(self):
        from music_player import ui

        track = Track(WATCH, "Song", 10, 42, "t")
        body = ui.added(track, extra_count=5, unavailable=1).description
        self.assertIn("1 unavailable video —", body)

    def test_added_embed_is_unchanged_when_nothing_was_dropped(self):
        from music_player import ui

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
        from music_player.help import HelpManual

        self._touch("first version")
        manual = HelpManual(self.path)
        self.assertEqual(manual.text(), "first version")

        self._touch("second version, longer")
        self.assertEqual(manual.text(), "second version, longer")

    def test_unchanged_file_is_not_read_again(self):
        from music_player.help import HelpManual

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
        from music_player.help import HelpManual

        manual = HelpManual(self.path)  # never created
        self.assertEqual(manual.text(), HelpManual.FALLBACK)

        self._touch("now it exists")
        self.assertEqual(manual.text(), "now it exists")

    def test_blank_save_keeps_the_previous_text(self):
        from music_player.help import HelpManual

        self._touch("real content")
        manual = HelpManual(self.path)

        self._touch("   \n  ")  # caught mid-edit
        self.assertEqual(manual.text(), "real content")

    def test_shipped_manual_fits_in_an_embed(self):
        from music_player.config import HELP_FILE

        text = HELP_FILE.read_text(encoding="utf-8")
        self.assertLessEqual(len(text), 4096, "Discord truncates past 4096 chars")
        self.assertTrue(text.strip())

    def test_sections_are_reparsed_on_reload(self):
        from music_player.help import HelpManual

        self._touch("intro\n\n**One**\nbody one")
        manual = HelpManual(self.path)
        self.assertEqual([s.name for s in manual.sections()], ["One"])

        self._touch("intro\n\n**One**\nbody one\n\n**Two**\nbody two")
        self.assertEqual([s.name for s in manual.sections()], ["One", "Two"])


class TestHelpParsing(unittest.TestCase):
    """help.txt drives the dropdown, so its headings must parse predictably."""

    def test_intro_and_sections_are_separated(self):
        from music_player.help import parse

        intro, sections = parse(
            "Read me first.\n\n**Playback**\n- ?play\n\n**Queue**\n- ?queue\n"
        )
        self.assertEqual(intro, "Read me first.")
        self.assertEqual([s.name for s in sections], ["Playback", "Queue"])
        self.assertEqual(sections[0].body, "- ?play")

    def test_a_line_that_merely_starts_bold_is_content(self):
        """`**?help** - display this list` is an entry, not a new category."""
        from music_player.help import parse

        _, sections = parse("**Start**\n**`?help`** — Display this list.\n")
        self.assertEqual([s.name for s in sections], ["Start"])
        self.assertIn("?help", sections[0].body)

    def test_a_file_without_headings_renders_whole(self):
        from music_player.help import parse

        intro, sections = parse("just a flat list of commands")
        self.assertEqual(intro, "")
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].body, "just a flat list of commands")

    def test_empty_sections_are_dropped(self):
        from music_player.help import parse

        _, sections = parse("**Empty**\n\n**Real**\ncontent")
        self.assertEqual([s.name for s in sections], ["Real"])

    def test_shipped_manual_parses_into_categories(self):
        from music_player.config import HELP_FILE
        from music_player.help import build_embed, parse

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

        from music_player.help import HelpManual

        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = Path(d.name) / "help.txt"
        path.write_text(text or self.MANUAL, encoding="utf-8")
        return HelpManual(path)

    async def test_one_option_per_section(self):
        from music_player.help import HelpView

        view = HelpView(self._manual(), user_id=7)
        select = view.children[0]
        self.assertEqual([o.label for o in select.options], ["One", "Two"])

    async def test_landing_page_carries_the_intro(self):
        from music_player.help import HelpView

        view = HelpView(self._manual(), user_id=7)
        self.assertEqual(view.landing.title, "One")
        self.assertIn("Lead line.", view.landing.description)
        self.assertIn("body one", view.landing.description)

    async def test_a_single_section_gets_no_dropdown(self):
        from music_player.help import HelpView

        view = HelpView(self._manual("flat text, no headings"), user_id=7)
        self.assertEqual(view.children, [], "nothing to choose between")

    async def test_choosing_swaps_the_page(self):
        from unittest.mock import AsyncMock

        from music_player.help import HelpView

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

        from music_player.help import HelpView

        view = HelpView(self._manual(), user_id=7)
        interaction = MagicMock()
        interaction.user.id = 999
        interaction.response.send_message = AsyncMock()

        self.assertFalse(await view.interaction_check(interaction))
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_owner_is_let_through(self):
        from music_player.help import HelpView

        view = HelpView(self._manual(), user_id=7)
        interaction = MagicMock()
        interaction.user.id = 7
        self.assertTrue(await view.interaction_check(interaction))

    async def test_timeout_disables_the_menu(self):
        from unittest.mock import AsyncMock

        from music_player.help import HelpView

        view = HelpView(self._manual(), user_id=7)
        view.message = MagicMock()
        view.message.edit = AsyncMock()

        await view.on_timeout()

        self.assertTrue(view.children[0].disabled)
        view.message.edit.assert_awaited_once()

    async def test_timeout_without_a_message_is_harmless(self):
        from music_player.help import HelpView

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
        self.assertLessEqual(len(clipped), 21)

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


class TestVideoId(unittest.TestCase):
    """Artwork is derived from the id, so a wrong id means a broken image."""

    def test_watch_url(self):
        from music_player.ytdl import video_id
        self.assertEqual(video_id(WATCH), VIDEO)

    def test_short_link(self):
        from music_player.ytdl import video_id
        self.assertEqual(video_id(f"https://youtu.be/{VIDEO}"), VIDEO)

    def test_shorts_and_embed_paths(self):
        from music_player.ytdl import video_id
        for path in ("shorts", "embed", "live", "v"):
            self.assertEqual(
                video_id(f"https://www.youtube.com/{path}/{VIDEO}"), VIDEO, path
            )

    def test_music_subdomain(self):
        from music_player.ytdl import video_id
        self.assertEqual(video_id(f"https://music.youtube.com/watch?v={VIDEO}"), VIDEO)

    def test_playlist_ride_along_is_ignored(self):
        from music_player.ytdl import video_id
        self.assertEqual(video_id(f"{WATCH}&list={LIST}"), VIDEO)

    def test_a_wrong_length_id_is_rejected(self):
        """Better no image than a request for a video that doesn't exist."""
        from music_player.ytdl import video_id
        self.assertIsNone(video_id("https://www.youtube.com/watch?v=short"))

    def test_non_youtube_hosts_are_rejected(self):
        from music_player.ytdl import video_id
        self.assertIsNone(video_id(f"https://example.com/watch?v={VIDEO}"))
        self.assertIsNone(video_id(f"https://notyoutube.com/watch?v={VIDEO}"))

    def test_bare_playlist_url_has_no_video(self):
        from music_player.ytdl import video_id
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
        self.assertIn("Skipped", ui.skipped(None).description)

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
        from music_player.player import Player
        from music_player.ytdl import FetchResult, TrackInfo

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

    def test_a_just_started_track_shows_no_bar(self):
        """A bar posted at 0:00 stays at 0:00 for the whole song."""
        line = ui.playback_line(0, 213, now=self.NOW)
        self.assertNotIn("▰", line)
        self.assertNotIn("▱", line)
        self.assertIn("3:33", line)

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
        from music_player.controls import QueuePages
        from music_player.player import Player

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
        from music_player.controls import PlayerControls
        from music_player.player import Player

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
        from music_player.player import Player

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
        from music_player.player import Player

        state = GuildState(1)
        self.assertEqual(Player.status_marker(state), "")

        state.voice = FakeVoice()
        state.voice.playing = True
        self.assertEqual(Player.status_marker(state), ui.PLAYING_MARKER)

        state.voice.playing = False
        state.voice.paused = True
        self.assertEqual(Player.status_marker(state), ui.PAUSED_MARKER)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Buttons under the Now Playing and Queue embeds.

Every control here is a shortcut for a command that already exists - the cog
methods stay the single implementation, and these callbacks only decide who is
allowed to press and what the message should look like afterwards.

Two rules apply throughout:

* **A press edits, it does not post.** Pausing repaints the existing Now
  Playing embed rather than adding a "paused!" message under it, so a song
  paused and resumed a few times leaves the channel exactly as it found it.
* **Permission matches consequence.** Anything that changes what the channel
  hears is restricted to people in the voice channel; paging through the queue
  changes nothing, so it only guards against someone else moving your page.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING, Optional

import discord

from music_player.ui import embeds as ui
from music_player.errors import DELIVERY_FAILED
from music_player.config import (
    CONTROLS_TIMEOUT,
    MAX_PLAYLISTS_PER_GUILD,
    PLAYLIST_PAGE_SIZE,
    PLAYLIST_VIEW_TIMEOUT,
    QUEUE_PAGE_SIZE,
    QUEUE_VIEW_TIMEOUT,
)
from music_player.services.library import Playlist, PlaylistSummary, StorageError
from music_player.state import GuildState

if TYPE_CHECKING:  # pragma: no cover - the cycle only matters to type checkers
    from music_player.cogs.player import Player
    from music_player.cogs.playlists import Playlists

log = logging.getLogger(__name__)

#: Discord truncates a select option's label past 100 characters.
_LABEL_LIMIT = 100


class _FadingView(discord.ui.View):
    """A view that greys itself out instead of leaving controls that do nothing.

    Discord keeps the message forever, but a view only lives as long as the
    process. Without this, every restart leaves buttons scattered through the
    channel that look pressable and silently fail.
    """

    def __init__(self, *, timeout: Optional[float]) -> None:
        super().__init__(timeout=timeout)
        self.message: Optional[discord.Message] = None

    def disable_all(self) -> None:
        for item in self.children:
            if isinstance(item, (discord.ui.Button, discord.ui.Select)):
                item.disabled = True

    async def fade(self) -> None:
        self.disable_all()
        self.stop()
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except DELIVERY_FAILED:
            log.debug("could not disable an expired view", exc_info=True)

    async def on_timeout(self) -> None:
        await self.fade()


class JumpToPage(discord.ui.Modal, title="Jump to page"):
    """Type a page number instead of clicking an arrow that many times.

    The number between the arrows used to be a disabled read-out. On a queue
    of a few pages that was fine; a playlist can hold 10,000 songs, which is a
    thousand pages, and the arrows alone made the far end unreachable. The
    read-out is the natural place to put the control, since it is already
    where the reader looks to find out where they are.
    """

    def __init__(self, view: "_Paged", pages: int) -> None:
        super().__init__()
        self._view = view
        self._pages = pages
        self._number = discord.ui.TextInput(
            label=f"Page number (1 to {pages})",
            placeholder="1",
            max_length=len(str(pages)),
            required=True,
        )
        self.add_item(self._number)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self._number.value).strip()
        try:
            page = int(raw)
        except ValueError:
            await interaction.response.send_message(
                embed=ui.error(
                    f"**“{raw[:40]}” isn't a page number.**\n"
                    f"Type a number from **1** to **{self._pages}**."
                ),
                ephemeral=True,
            )
            return
        if not 1 <= page <= self._pages:
            await interaction.response.send_message(
                embed=ui.no_such_page(page, self._pages), ephemeral=True
            )
            return

        self._view.page = page
        await interaction.response.edit_message(
            embed=self._view.render(), view=self._view
        )


class _Paged(_FadingView):
    """A view whose middle button says where you are and jumps you elsewhere.

    Subclasses own ``page``, ``render()`` and a ``sync()`` that calls
    :meth:`sync_indicator`; this only holds the part both pagers share.
    """

    page: int

    def sync_indicator(self, pages: int) -> None:
        """Label the read-out, and let it be pressed when there is somewhere
        to go."""
        self.indicator.label = f"{self.page} / {pages}"
        self.indicator.disabled = pages <= 1

    async def open_jump(self, interaction: discord.Interaction, pages: int) -> None:
        if pages <= 1:  # pragma: no cover - the button is disabled by then
            await interaction.response.defer()
            return
        await interaction.response.send_modal(JumpToPage(self, pages))


class PlayerControls(_FadingView):
    """Pause / skip / queue, attached to a Now Playing message.

    Restricted to listeners: a button sits in a public channel where ``?skip``
    at least required typing, so this is where "you have to be in the voice
    channel you are affecting" is enforced.
    """

    def __init__(
        self,
        cog: "Player",
        state: GuildState,
        snapshot: ui.NowPlaying,
        *,
        timeout: float = CONTROLS_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.state = state
        #: The data the embed was built from, so a repaint can redraw the
        #: progress bar without re-resolving the stream.
        self.snapshot = snapshot
        #: Set by :meth:`fade` when this message stops being maintained.
        self.frozen = False
        #: Whoever last pressed pause. The button edits this card rather than
        #: posting a reply, so this is the only place it can be said.
        self.actor: Optional[discord.abc.User] = None
        self.sync()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        voice = self.state.voice
        if voice is None or not self.state.connected:
            await interaction.response.send_message(
                embed=ui.error("**I've left the channel.** These controls are done."),
                ephemeral=True,
            )
            return False

        user_voice = getattr(interaction.user, "voice", None)
        if user_voice is None or user_voice.channel != voice.channel:
            await interaction.response.send_message(
                embed=ui.error(
                    f"**Join {voice.channel.name} first.**\n"
                    "These controls are for people listening."
                ),
                ephemeral=True,
            )
            return False
        return True

    # -- rendering ----------------------------------------------------------

    def sync(self) -> None:
        """Point the toggle at whichever action is currently available."""
        paused = self.state.paused
        self.toggle.emoji = "▶️" if paused else "⏸️"
        self.toggle.label = "Resume" if paused else "Pause"
        self.toggle.style = (
            discord.ButtonStyle.success if paused else discord.ButtonStyle.secondary
        )

    def render(self) -> discord.Embed:
        """The Now Playing embed as it should look right now."""
        self.snapshot = dataclasses.replace(
            self.snapshot,
            elapsed=self.state.elapsed,
            paused=self.state.paused,
            volume=self.state.volume,
            stale=self.frozen,
            actor=self.actor,
        )
        return ui.now_playing(self.snapshot)

    async def repaint(self) -> None:
        """Push the current state onto the message this view is attached to."""
        if self.is_finished() or self.message is None:
            return
        self.sync()
        try:
            await self.message.edit(embed=self.render(), view=self)
        except DELIVERY_FAILED:
            log.debug("could not repaint the now playing message", exc_info=True)

    async def fade(self) -> None:
        """Grey the buttons out *and* stop the embed claiming to be live.

        The base class only disables the controls, which is right for a queue
        listing - it has nothing that moves. This message does: the finish
        time is a Discord ``<t:...:R>`` stamp that the *client* keeps counting
        down, with no help from us. Useful while somebody is still repainting
        the message on every pause; a confident lie the moment nobody is.

        ``fade`` is where that moment happens. It calls ``stop()``, after
        which :meth:`repaint` gives up forever - so this is the last chance to
        take the countdown off, and it takes it.
        """
        self.frozen = True
        self.disable_all()
        self.stop()
        if self.message is None:
            return
        try:
            await self.message.edit(embed=self.render(), view=self)
        except DELIVERY_FAILED:
            log.debug("could not retire the now playing message", exc_info=True)

    async def retire(self) -> None:
        """Called when this track stops being the one playing."""
        await self.fade()

    # -- controls -----------------------------------------------------------

    @discord.ui.button(emoji="⏸️", label="Pause", style=discord.ButtonStyle.secondary)
    async def toggle(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.state.paused:
            self.cog.apply_resume(self.state)
            self.actor = None
        elif self.state.playing:
            self.cog.apply_pause(self.state)
            self.actor = interaction.user
        else:
            await interaction.response.send_message(
                embed=ui.nothing_playing(), ephemeral=True
            )
            return

        self.sync()
        await interaction.response.edit_message(embed=self.render(), view=self)

    @discord.ui.button(emoji="⏭️", label="Skip", style=discord.ButtonStyle.secondary)
    async def skip(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self.state.queue:
            await interaction.response.send_message(
                embed=ui.empty_queue(), ephemeral=True
            )
            return

        skipping = self.state.current
        # Defer first: resolving the next track's stream can outlast Discord's
        # 3s window, and perform_skip is what greys this strip out.
        await interaction.response.defer()
        await interaction.followup.send(
            embed=ui.skipped(skipping, by=interaction.user)
        )
        await self.cog.perform_skip(interaction.channel, self.state)

    @discord.ui.button(emoji="📜", label="Queue", style=discord.ButtonStyle.secondary)
    async def queue(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self.state.queue:
            await interaction.response.send_message(
                embed=ui.empty_queue(), ephemeral=True
            )
            return
        # Ephemeral: reading the queue is a private act and should not push the
        # Now Playing message up the channel for everyone else.
        view = QueuePages(self.cog, self.state, user_id=interaction.user.id)
        await interaction.response.send_message(
            embed=view.render(), view=view, ephemeral=True
        )


class QueuePages(_Paged):
    """Page through the queue with buttons instead of ``?queueto <n>``.

    Read-only, so anyone may open one - but only the person who did can turn
    its pages, otherwise two people browsing the same message fight over it.
    """

    def __init__(
        self,
        cog: "Player",
        state: GuildState,
        *,
        user_id: int,
        page: int = 1,
        timeout: float = QUEUE_VIEW_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.state = state
        self.user_id = user_id
        self.page = page
        self.sync()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user is not None and interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            embed=ui.neutral(
                "**This queue view belongs to someone else.**\n"
                "Run **`?queue`** to get your own."
            ),
            ephemeral=True,
        )
        return False

    def sync(self) -> None:
        """Clamp the page and label the buttons for where we actually are.

        The queue is live - songs finish and are added while a page is open -
        so the page count is recomputed on every repaint rather than trusted.
        """
        pages = ui.total_pages(len(self.state.queue), QUEUE_PAGE_SIZE)
        self.page = min(max(1, self.page), pages)
        self.previous.disabled = self.page <= 1
        self.next.disabled = self.page >= pages
        self.sync_indicator(pages)

    def render(self) -> discord.Embed:
        self.sync()
        return ui.queue_page(
            self.state.queue, self.page, status=self.cog.status_marker(self.state)
        )

    async def _turn(self, interaction: discord.Interaction, delta: int) -> None:
        self.page += delta
        await interaction.response.edit_message(embed=self.render(), view=self)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._turn(interaction, -1)

    #: Where you are, and how to go somewhere else. Sits between the arrows so
    #: the page number is with the things that change it.
    @discord.ui.button(label="1 / 1", style=discord.ButtonStyle.secondary)
    async def indicator(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.open_jump(
            interaction, ui.total_pages(len(self.state.queue), QUEUE_PAGE_SIZE)
        )

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._turn(interaction, 1)

    @discord.ui.button(emoji="🔄", style=discord.ButtonStyle.secondary)
    async def refresh(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Re-read the queue without leaving the page you were on."""
        await interaction.response.edit_message(embed=self.render(), view=self)


class PlaylistBrowser(_Paged):
    """Pick a playlist, read it, and play it - all on the one message.

    Splitting "choose" and "look inside" across two messages meant anyone who
    picked the wrong one had to run the command again. Here the dropdown stays
    put and choosing swaps the page underneath it.

    ``viewer`` and ``guild`` are deliberately separate. The playlists belong to
    the whole server, but this *message* still belongs to whoever ran the
    command - otherwise two people browsing the same message would fight over
    what page it is on.

    The dropdown is built from *summaries* - a name and two totals each - so
    opening the menu costs one aggregate query rather than every song in the
    server. Songs are read only for the playlist actually picked.

    That set is snapshotted when the view is built, the same way the help menu
    snapshots its sections: somebody adding a playlist while this is open
    cannot renumber the options under them. The buttons re-read the live
    playlist, so what gets played is never the stale copy.
    """

    def __init__(
        self,
        cog: Playlists,
        viewer: discord.abc.User,
        guild: discord.Guild,
        summaries: Sequence[PlaylistSummary],
        *,
        selected: Optional[Playlist] = None,
        index: Optional[int] = None,
        timeout: float = PLAYLIST_VIEW_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.viewer = viewer
        self.guild = guild
        self.summaries = list(summaries)[:MAX_PLAYLISTS_PER_GUILD]
        #: Which option is chosen, and the playlist behind it once its songs
        #: have been read. They move together; ``index`` alone is what the
        #: dropdown needs.
        self.index = index
        self.selected = selected
        self.page = 1

        self.choose: discord.ui.Select = discord.ui.Select(
            placeholder="Choose a playlist",
            row=0,
            # Indexed rather than keyed by name: NFKC folding can grow a name
            # past the 100 characters Discord allows in an option value, and
            # the index is stable for as long as this snapshot is.
            options=[
                discord.SelectOption(
                    label=ui.clip(playlist.name, _LABEL_LIMIT),
                    description=ui.playlist_summary(playlist),
                    value=str(index),
                )
                for index, playlist in enumerate(self.summaries)
            ],
        )
        self.choose.callback = self._on_choose
        self.add_item(self.choose)
        self.sync()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user is not None and interaction.user.id == self.viewer.id:
            return True
        await interaction.response.send_message(
            embed=ui.neutral(
                "**This browser belongs to someone else.**\n"
                "The playlists are the server's — run **`?playlist`** to get "
                "your own copy of this menu."
            ),
            ephemeral=True,
        )
        return False

    # -- rendering ----------------------------------------------------------

    def sync(self) -> None:
        """Point every control at the playlist and page currently on screen."""
        chosen = self.selected
        for index, option in enumerate(self.choose.options):
            option.default = index == self.index

        pages = (
            ui.total_pages(len(chosen.tracks), PLAYLIST_PAGE_SIZE) if chosen else 1
        )
        self.page = min(max(1, self.page), pages)

        empty = chosen is None or not chosen.tracks
        self.previous.disabled = chosen is None or self.page <= 1
        self.next.disabled = chosen is None or self.page >= pages
        if chosen is None:
            self.indicator.label = "—"
            self.indicator.disabled = True
        else:
            self.sync_indicator(pages)
        self.play.disabled = empty
        self.enqueue.disabled = empty

    def playing_now(self) -> Optional[str]:
        """The playlist the current song came from, if it came from one.

        Read live rather than snapshotted: the menu can sit open across
        several songs, and the marker should follow what is actually on air.
        """
        current = self.cog.state.get(self.guild.id).current
        return current.source if current is not None else None

    def render(self) -> discord.Embed:
        self.sync()
        if self.selected is None:
            return ui.playlist_overview(
                self.summaries,
                title="Playlists",
                author=getattr(self.guild, "name", None),
                icon_url=self.guild.icon.url if self.guild.icon else None,
                playing=self.playing_now(),
            )
        current = self.cog.state.get(self.guild.id).current
        return ui.playlist_page(
            self.selected,
            self.page,
            playing_url=current.url if current is not None else None,
        )

    async def _on_choose(self, interaction: discord.Interaction) -> None:
        """Load the picked playlist's songs and show its first page."""
        index = int(self.choose.values[0])
        if not 0 <= index < len(self.summaries):  # pragma: no cover - Discord
            return
        try:
            playlist = await self.cog.library.find(
                interaction.guild.id, self.summaries[index].name
            )
        except StorageError as exc:
            await interaction.response.send_message(
                embed=ui.explain(exc), ephemeral=True
            )
            return
        if playlist is None:
            # Someone deleted it between this menu being posted and now.
            await interaction.response.send_message(
                embed=ui.no_such_playlist(self.summaries[index].name),
                ephemeral=True,
            )
            return

        self.index = index
        self.selected = playlist
        self.page = 1
        await interaction.response.edit_message(embed=self.render(), view=self)

    async def _turn(self, interaction: discord.Interaction, delta: int) -> None:
        self.page += delta
        await interaction.response.edit_message(embed=self.render(), view=self)

    # -- controls -----------------------------------------------------------

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def previous(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._turn(interaction, -1)

    #: Where you are, and how to get somewhere else. A full playlist runs to a
    #: thousand pages, which the arrows alone cannot reach.
    @discord.ui.button(label="—", style=discord.ButtonStyle.secondary, row=1)
    async def indicator(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.selected is None:  # pragma: no cover - disabled by then
            await interaction.response.defer()
            return
        await self.open_jump(
            interaction, ui.total_pages(len(self.selected.tracks), PLAYLIST_PAGE_SIZE)
        )

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def next(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._turn(interaction, 1)

    @discord.ui.button(
        emoji="▶️", label="Play", style=discord.ButtonStyle.success, row=2
    )
    async def play(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._load(interaction, replace=True)

    @discord.ui.button(
        emoji="➕", label="Add to queue", style=discord.ButtonStyle.secondary, row=2
    )
    async def enqueue(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._load(interaction, replace=False)

    async def _load(
        self, interaction: discord.Interaction, *, replace: bool
    ) -> None:
        if self.selected is None:
            await interaction.response.send_message(
                embed=ui.pick_a_playlist(), ephemeral=True
            )
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=ui.playlist_needs_a_server(), ephemeral=True
            )
            return

        # Re-read it: this view outlives the command that posted it, and anyone
        # in the server may have renamed or deleted the playlist meanwhile.
        try:
            playlist = await self.cog.library.find(
                interaction.guild.id, self.selected.name
            )
        except StorageError as exc:
            await interaction.response.send_message(
                embed=ui.explain(exc), ephemeral=True
            )
            return
        if playlist is None:
            await interaction.response.send_message(
                embed=ui.no_such_playlist(self.selected.name), ephemeral=True
            )
            return
        if not playlist.tracks:
            await interaction.response.send_message(
                embed=ui.playlist_is_empty(playlist), ephemeral=True
            )
            return

        # Resolving the first track's stream can outlast Discord's 3s window,
        # and this browser may be an ephemeral message - so the confirmation
        # goes to the channel, where everyone affected by it can see it.
        await interaction.response.defer()
        state = self.cog.state.get(interaction.guild.id)
        await self.cog.load(
            interaction.channel, state, playlist, interaction.user, replace=replace
        )

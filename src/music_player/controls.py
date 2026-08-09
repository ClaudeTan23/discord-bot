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

from music_player import ui
from music_player.config import (
    CONTROLS_TIMEOUT,
    QUEUE_PAGE_SIZE,
    QUEUE_VIEW_TIMEOUT,
)
from music_player.state import GuildState

if TYPE_CHECKING:  # pragma: no cover - the cycle only matters to type checkers
    from music_player.player import Player

log = logging.getLogger(__name__)


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
        except discord.HTTPException:
            log.debug("could not disable an expired view", exc_info=True)

    async def on_timeout(self) -> None:
        await self.fade()


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
        )
        return ui.now_playing(self.snapshot)

    async def repaint(self) -> None:
        """Push the current state onto the message this view is attached to."""
        if self.is_finished() or self.message is None:
            return
        self.sync()
        try:
            await self.message.edit(embed=self.render(), view=self)
        except discord.HTTPException:
            log.debug("could not repaint the now playing message", exc_info=True)

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
        elif self.state.playing:
            self.cog.apply_pause(self.state)
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
        await interaction.followup.send(embed=ui.skipped(skipping))
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


class QueuePages(_FadingView):
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
        self.indicator.label = f"{self.page} / {pages}"

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

    #: Not a control - a read-out. Disabled so it cannot be pressed, and placed
    #: between the arrows so the page number sits with what changes it.
    @discord.ui.button(label="1 / 1", style=discord.ButtonStyle.secondary, disabled=True)
    async def indicator(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:  # pragma: no cover - a disabled button never fires
        pass

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

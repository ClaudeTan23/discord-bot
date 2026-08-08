"""The ``?help`` manual: a text file on disk, rendered as a browsable embed.

``help.txt`` stays the single place the wording lives. Its bold-only lines are
read as section headings, so adding a category to the file adds an entry to the
dropdown - no Python change, and the file is still perfectly readable on its own.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import discord
from discord.ext import commands

log = logging.getLogger(__name__)

#: A heading is a line that is *only* bold - ``**Playback**``. A line that
#: merely starts bold, like ``**`?skip`** - skip to the next song``, is content.
_HEADING = re.compile(r"^\*\*([^*]+)\*\*$")

# Discord's limits on the pieces this module fills.
_DESCRIPTION_LIMIT = 4096
_LABEL_LIMIT = 100
_OPTION_LIMIT = 25

_DEFAULT_TITLE = "Commands"
_FOOTER = "Use the menu below to browse the other categories."
_EXPIRED = "This menu has expired"


@dataclass(frozen=True, slots=True)
class Section:
    """One headed block of the manual - a single page of the dropdown."""

    name: str
    body: str


def parse(text: str) -> Tuple[str, List[Section]]:
    """Split the manual into its lead paragraph and its headed sections.

    A file with no headings at all is not an error: it comes back as one
    section holding everything, which renders exactly like the old flat embed.
    """
    intro: List[str] = []
    sections: List[Section] = []
    name: Optional[str] = None
    body: List[str] = []

    for line in text.splitlines():
        heading = _HEADING.match(line.strip())
        if heading:
            if name is not None:
                sections.append(Section(name, "\n".join(body).strip()))
            name, body = heading.group(1).strip(), []
        elif name is None:
            intro.append(line)
        else:
            body.append(line)

    if name is not None:
        sections.append(Section(name, "\n".join(body).strip()))

    sections = [section for section in sections if section.body][:_OPTION_LIMIT]
    if not sections:
        return "", [Section(_DEFAULT_TITLE, text.strip())]
    return "\n".join(intro).strip(), sections


def build_embed(section: Section, *, intro: str = "") -> discord.Embed:
    """Render one section. ``intro`` is prepended on the landing page only."""
    body = f"{intro}\n\n{section.body}" if intro else section.body
    if len(body) > _DESCRIPTION_LIMIT:
        body = body[: _DESCRIPTION_LIMIT - 1] + "…"
    return discord.Embed(
        colour=discord.Colour.dark_grey(),
        title=section.name,
        description=body,
    )


class HelpManual:
    """The help text, re-read whenever the file behind it changes.

    Loading once at startup meant every wording fix needed the bot restarted.
    Re-reading on every ``?help`` would undo that for no reason, so this
    compares the file's timestamp and size first and only pays for the read -
    and the re-parse - when there is something new to read.

    A blank file is ignored and the previous text kept: saving mid-edit should
    not leave the command answering with nothing.
    """

    __slots__ = ("_path", "_text", "_intro", "_sections", "_stamp")

    FALLBACK = "Help manual is unavailable."

    def __init__(self, path: Path) -> None:
        self._path = path
        self._text = self.FALLBACK
        self._intro = ""
        self._sections: List[Section] = [Section(_DEFAULT_TITLE, self.FALLBACK)]
        self._stamp: tuple[int, int] | None = None
        self.text()  # Surface a missing or unreadable file at startup.

    def text(self) -> str:
        """The current manual, reloading it first if the file has changed."""
        try:
            stat = self._path.stat()
        except OSError:
            log.warning("help file %s is not readable", self._path)
            return self._text

        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp:
            return self._text

        try:
            fresh = self._path.read_text(encoding="utf-8")
        except OSError:
            log.exception("could not read help file at %s", self._path)
            return self._text

        # Accept the new timestamp either way, so a file that is genuinely
        # empty is not re-read on every invocation.
        self._stamp = stamp
        if fresh.strip():
            self._text = fresh
            self._intro, self._sections = parse(fresh)
            log.info(
                "loaded help manual from %s (%d chars, %d sections)",
                self._path,
                len(fresh),
                len(self._sections),
            )
        else:
            log.warning("help file %s is empty; keeping the previous text", self._path)
        return self._text

    def intro(self) -> str:
        self.text()
        return self._intro

    def sections(self) -> List[Section]:
        self.text()
        return self._sections


class HelpView(discord.ui.View):
    """A category picker over the manual's sections.

    The sections are snapshotted when the view is built. Editing ``help.txt``
    while somebody has the menu open therefore cannot renumber the options
    under them; the next ``?help`` picks the change up.
    """

    def __init__(
        self, manual: HelpManual, *, user_id: int, timeout: float = 180.0
    ) -> None:
        super().__init__(timeout=timeout)
        self._manual = manual
        self._user_id = user_id
        self._sections = manual.sections()
        self._intro = manual.intro()
        self.message: Optional[discord.Message] = None

        self._select: discord.ui.Select = discord.ui.Select(
            placeholder="Choose a category",
            options=[
                discord.SelectOption(label=section.name[:_LABEL_LIMIT], value=str(index))
                for index, section in enumerate(self._sections)
            ],
        )
        self._select.callback = self._choose
        # A manual with a single section has nothing to choose between.
        if len(self._sections) > 1:
            self.add_item(self._select)

    @property
    def landing(self) -> discord.Embed:
        """The page shown before anything is picked."""
        embed = build_embed(self._sections[0], intro=self._intro)
        if len(self._sections) > 1:
            embed.set_footer(text=_FOOTER)
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Keep the menu answering only to whoever asked for it."""
        if interaction.user is not None and interaction.user.id == self._user_id:
            return True
        await interaction.response.send_message(
            "This menu belongs to someone else. Run the command yourself to get "
            "your own.",
            ephemeral=True,
        )
        return False

    async def _choose(self, interaction: discord.Interaction) -> None:
        chosen = self._select.values[0]
        # Keep the picked entry shown in the closed dropdown.
        for option in self._select.options:
            option.default = option.value == chosen
        embed = build_embed(self._sections[int(chosen)])
        embed.set_footer(text=_FOOTER)
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self) -> None:
        """Grey the menu out rather than leaving a control that silently fails."""
        self._select.disabled = True
        self._select.placeholder = _EXPIRED
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            log.debug("could not disable the expired help menu", exc_info=True)


async def send_manual(ctx: commands.Context, manual: HelpManual) -> None:
    """Answer a ``?help`` / ``/help`` invocation.

    Slash invocations reply privately, so browsing the manual never fills the
    channel. Discord has no equivalent for prefix commands, so ``?help`` stays
    public.
    """
    ephemeral = ctx.interaction is not None
    view = HelpView(manual, user_id=ctx.author.id)

    if not view.children:
        view.stop()
        await ctx.send(embed=view.landing, ephemeral=ephemeral)
        return

    view.message = await ctx.send(embed=view.landing, view=view, ephemeral=ephemeral)

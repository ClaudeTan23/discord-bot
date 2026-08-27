"""Saved playlists: the ``?playlist`` command group and its browser.

A playlist belongs to the **server it was made in**. Everyone there can see it,
add songs, take songs out, and play it; nobody outside that server can reach it
at all. Renaming or deleting one outright is kept to whoever created it and to
moderators with **Manage Server**, because throwing away a list the whole
channel built is a different act from taking one song out of it.

A playlist is not the live queue. Loading one is a *copy* into it - editing
"Late Night" while it plays changes the playlist, never the songs already lined
up - and two commands do that copy:

* ``?playlist play`` replaces what was waiting and starts the playlist now;
* ``?playlist queue`` leaves the queue alone and adds to the end of it.

Every library call is awaited: the store is SQLite, and its queries run on a
worker thread rather than on the loop that also drives audio. A mutation either
commits or raises :class:`~music_player.services.library.StorageError` with nothing
changed, so a command has one failure to report rather than a half-applied one.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Sequence

import discord
from discord import Interaction, app_commands
from discord.ext import commands

from music_player.ui import embeds as ui
from music_player.config import ADD_PER, ADD_RATE

# _FadingView is package-internal rather than private to ``controls``: every
# view the bot posts has to grey itself out when it stops working, and there is
# no reason for this module to reimplement that.
from music_player.ui.views import PlaylistBrowser
from music_player.services.library import (
    Playlist,
    PlaylistError,
    PlaylistLibrary,
    SavedTrack,
    StorageError,
    fold,
)
from music_player.cogs.player import Player
from music_player.state import GuildState, MusicState, Track
from music_player.services.youtube import ExtractionError, YouTubeService

log = logging.getLogger(__name__)

#: Discord rejects an autocomplete choice whose name or value passes 100 chars,
#: and shows at most 25 of them.
_CHOICE_LIMIT = 100
_CHOICE_COUNT = 25


def _usage(ctx: commands.Context, missing: Optional[str]) -> str:
    """"This command needs a name" plus the shape it actually wants."""
    command = ctx.command
    signature = f"?{command.qualified_name} {command.signature}".strip()
    lead = (
        f"**`?{command.qualified_name}` needs a {missing}.**"
        if missing
        else "**I couldn't read that.**"
    )
    return (
        f"{lead}\nIt goes: **`{signature}`**\n"
        "-# A name with spaces needs quotes — or use the slash command, which "
        "lists the playlists as you type."
    )


class Playlists(commands.Cog):
    """``?playlist``: create, fill, and play the server's saved playlists."""

    def __init__(
        self,
        bot: commands.Bot,
        state: MusicState,
        youtube: YouTubeService,
        library: PlaylistLibrary,
        player: Player,
    ) -> None:
        self.bot = bot
        self.state = state
        self.youtube = youtube
        self.library = library
        # Playback belongs to the Player cog. Reaching for it directly, rather
        # than reimplementing "start the queue", is what keeps a playlist and a
        # plain ?play from drifting into two different pipelines.
        self.player = player

    async def cog_check(self, ctx: commands.Context) -> bool:
        """Every command here needs a server, because every playlist has one.

        A cog-level check rather than ``guild_only()`` on each of the nine:
        parent checks are skipped for the subcommands of a group that can be
        invoked on its own, so the group's own decorator would not cover them.
        """
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    # ------------------------------------------------------------------
    # permissions
    # ------------------------------------------------------------------

    @staticmethod
    def may_manage(ctx: commands.Context, playlist: Playlist) -> bool:
        """May this person rename or delete the playlist itself?

        Adding and removing songs is open to everyone in the server - that is
        the point. Destroying the list is not, or a single person could wipe
        something the whole channel built.
        """
        if playlist.created_by is not None and playlist.created_by == ctx.author.id:
            return True
        permissions = getattr(ctx.author, "guild_permissions", None)
        return bool(permissions is not None and permissions.manage_guild)

    # ------------------------------------------------------------------
    # shared helpers
    # ------------------------------------------------------------------

    async def load(
        self,
        destination: discord.abc.Messageable,
        state: GuildState,
        playlist: Playlist,
        requester: discord.abc.User,
        *,
        replace: bool,
    ) -> None:
        """Copy a saved playlist into the server's live queue.

        ``destination`` receives the confirmation. For a command invocation it
        is the Context, so a deferred slash command actually gets answered;
        anything posted afterwards (the Now Playing message) goes to the plain
        channel, since an interaction can only be responded to once.
        """
        channel = getattr(destination, "channel", destination)
        tracks = [
            Track(
                url=saved.url,
                title=saved.title,
                duration=saved.duration,
                requester_id=requester.id,
                requester_name=requester.name,
                source=playlist.name,
            )
            for saved in playlist.tracks
        ]
        if not tracks:
            await destination.send(embed=ui.playlist_is_empty(playlist))
            return

        if not replace:
            # Deliberately the same shape as ?add, down to the countdown: this
            # is ?add with a hundred links, and it should read like it.
            first_index = len(state.queue)
            state.queue.extend(tracks)
            starts_in = None
            if state.playing:
                ahead = ui.total_duration(state.queue[:first_index])
                starts_in = max(0, ahead - int(state.elapsed))
            await destination.send(
                embed=ui.playlist_queued(
                    playlist,
                    len(tracks),
                    position=first_index + 1,
                    starts_in=starts_in,
                )
            )
            return

        live = (
            state.connected
            and state.current is not None
            and (state.playing or state.paused)
        )
        if live:
            # Something is on air. Leave it at the head so ``perform_skip`` has
            # a track to skip *past*, replace everything behind it, and let the
            # ordinary skip path start the playlist - hand-rolling a stop here
            # would race the voice client's after-callback.
            del state.queue[1:]
            state.queue.extend(tracks)
            await destination.send(embed=ui.playlist_playing(playlist, len(tracks)))
            await self.player.perform_skip(channel, state)
            return

        state.queue.clear()
        state.queue.extend(tracks)
        state.skip_requested = False
        state.suppress_advance = False
        state.retried_url = None
        await destination.send(
            embed=ui.playlist_playing(
                playlist, len(tracks), connected=state.connected
            )
        )
        if state.connected:
            await self.player.start_queue(channel, state)

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    @commands.hybrid_group(
        name="playlist",
        fallback="list",
        description="Playlists saved in this server",
    )
    @commands.guild_only()
    async def playlist(self, ctx: commands.Context) -> None:
        """Bare ``?playlist``: everything this server has, with a picker."""
        try:
            owned = await self.library.summaries(ctx.guild.id)
        except StorageError as exc:
            await ctx.send(embed=ui.explain(exc))
            return
        if not owned:
            await ctx.send(embed=ui.no_playlists())
            return
        view = PlaylistBrowser(self, ctx.author, ctx.guild, owned)
        view.message = await ctx.send(embed=view.render(), view=view)

    @playlist.command(
        name="create", description="Start a new playlist everyone here can fill"
    )
    @app_commands.describe(name="What to call it")
    async def create(self, ctx: commands.Context, *, name: str) -> None:
        try:
            created = await self.library.create(
                ctx.guild.id, name, created_by=ctx.author.id
            )
        except (PlaylistError, StorageError) as exc:
            await ctx.send(embed=ui.explain(exc))
            return
        await ctx.send(embed=ui.playlist_created(created))

    @playlist.command(
        name="delete", description="Delete a playlist (its creator, or mods)"
    )
    @app_commands.describe(name="Which playlist")
    async def delete(self, ctx: commands.Context, *, name: str) -> None:
        try:
            playlist = await self.library.require(ctx.guild.id, name)
            if not self.may_manage(ctx, playlist):
                await ctx.send(embed=ui.not_your_playlist(playlist))
                return
            await self.library.delete(ctx.guild.id, playlist.name)
        except (PlaylistError, StorageError) as exc:
            await ctx.send(embed=ui.explain(exc))
            return
        await ctx.send(embed=ui.playlist_deleted(playlist.name))

    @playlist.command(
        name="rename", description="Rename a playlist (its creator, or mods)"
    )
    @app_commands.describe(old="The playlist to rename", new="Its new name")
    async def rename(self, ctx: commands.Context, old: str, *, new: str) -> None:
        try:
            playlist = await self.library.require(ctx.guild.id, old)
            if not self.may_manage(ctx, playlist):
                await ctx.send(embed=ui.not_your_playlist(playlist))
                return
            was = playlist.name
            renamed = await self.library.rename(ctx.guild.id, was, new)
        except (PlaylistError, StorageError) as exc:
            await ctx.send(embed=ui.explain(exc))
            return
        await ctx.send(embed=ui.playlist_renamed(was, renamed.name))

    @playlist.command(name="add", description="Add a YouTube link to a playlist")
    @app_commands.describe(
        name="Which playlist", link="A YouTube video or playlist link"
    )
    # The same throttles ?add carries: one extraction per user at a time, so
    # nobody can occupy several extractor threads at once.
    @commands.max_concurrency(1, per=commands.BucketType.user, wait=False)
    @commands.cooldown(ADD_RATE, ADD_PER, commands.BucketType.user)
    async def add(self, ctx: commands.Context, name: str, *, link: str) -> None:
        try:
            playlist = await self.library.find(ctx.guild.id, name)
        except StorageError as exc:
            await ctx.send(embed=ui.explain(exc))
            return
        if playlist is None:
            await ctx.send(embed=ui.no_such_playlist(name))
            return

        async with ui.thinking(ctx):
            try:
                result = await self.youtube.fetch(link)
            except ExtractionError as exc:
                log.info("playlist add failed for %r: %s", link, exc)
                await ctx.send(embed=ui.invalid_link())
                return
            except Exception:
                log.exception("unexpected error adding %r to a playlist", link)
                await ctx.send(embed=ui.generic_error())
                return

            wanted = [
                SavedTrack(url=entry.url, title=entry.title, duration=entry.duration)
                for entry in result.entries
            ]
            try:
                # The updated playlist comes back, so the confirmation's song
                # count is what the database now holds rather than what it
                # held when the name was looked up.
                playlist, added = await self.library.extend(
                    ctx.guild.id, playlist.name, wanted
                )
            except (PlaylistError, StorageError) as exc:
                await ctx.send(embed=ui.explain(exc))
                return

            await ctx.send(
                embed=ui.playlist_added(
                    playlist,
                    wanted[0],
                    added=added,
                    requested=len(wanted),
                    unavailable=result.unavailable,
                )
            )

    @playlist.command(name="remove", description="Remove one song from a playlist")
    @app_commands.describe(
        name="Which playlist", number="The number of the song inside it"
    )
    async def remove(self, ctx: commands.Context, name: str, number: int) -> None:
        try:
            playlist, track = await self.library.remove_at(
                ctx.guild.id, name, number
            )
        except (PlaylistError, StorageError) as exc:
            await ctx.send(embed=ui.explain(exc))
            return
        await ctx.send(embed=ui.playlist_removed(playlist, track))

    @playlist.command(name="show", description="Look inside a playlist")
    @app_commands.describe(name="Which playlist")
    async def show(self, ctx: commands.Context, *, name: str) -> None:
        try:
            owned = await self.library.summaries(ctx.guild.id)
            key = fold(name)
            index = next(
                (i for i, s in enumerate(owned) if s.key == key), None
            )
            if index is None:
                await ctx.send(embed=ui.no_such_playlist(name))
                return
            # Only the one being opened is loaded in full.
            playlist = await self.library.require(ctx.guild.id, owned[index].name)
        except (PlaylistError, StorageError) as exc:
            await ctx.send(embed=ui.explain(exc))
            return

        view = PlaylistBrowser(
            self, ctx.author, ctx.guild, owned, selected=playlist, index=index
        )
        view.message = await ctx.send(embed=view.render(), view=view)

    @playlist.command(
        name="play", description="Play a playlist, replacing the queue"
    )
    @app_commands.describe(name="Which playlist")
    async def play(self, ctx: commands.Context, *, name: str) -> None:
        playlist = await self._lookup(ctx, name)
        if playlist is None:
            return
        # Starting audio means resolving a stream - long enough that a slash
        # command has to be deferred first.
        async with ui.thinking(ctx):
            await self.load(
                ctx, self.state.get(ctx.guild.id), playlist, ctx.author, replace=True
            )

    @playlist.command(
        name="queue", description="Add a playlist to the end of the queue"
    )
    @app_commands.describe(name="Which playlist")
    async def enqueue(self, ctx: commands.Context, *, name: str) -> None:
        playlist = await self._lookup(ctx, name)
        if playlist is None:
            return
        await self.load(
            ctx, self.state.get(ctx.guild.id), playlist, ctx.author, replace=False
        )

    async def _lookup(
        self, ctx: commands.Context, name: str
    ) -> Optional[Playlist]:
        """Resolve a playlist, or answer the user and return ``None``."""
        try:
            playlist = await self.library.find(ctx.guild.id, name)
        except StorageError as exc:
            await ctx.send(embed=ui.explain(exc))
            return None
        if playlist is None:
            await ctx.send(embed=ui.no_such_playlist(name))
        return playlist

    # ------------------------------------------------------------------
    # autocomplete and errors
    # ------------------------------------------------------------------

    @delete.autocomplete("name")
    @rename.autocomplete("old")
    @add.autocomplete("name")
    @remove.autocomplete("name")
    @show.autocomplete("name")
    @play.autocomplete("name")
    @enqueue.autocomplete("name")
    async def playlist_name_autocomplete(
        self, interaction: Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        """Offer this server's playlists.

        Purely local - no network, no deadline to miss - which is what makes
        picking one from a list the normal way to use every command here.
        """
        if interaction.guild is None:
            return []
        try:
            names = await self.library.names(interaction.guild.id)
        except StorageError:
            # Autocomplete has no way to show an error; an empty list is the
            # honest answer and the command itself will report the failure.
            return []
        needle = fold(current)
        return [
            app_commands.Choice(name=name[:_CHOICE_LIMIT], value=name[:_CHOICE_LIMIT])
            for name in names
            if needle in fold(name)
        ][:_CHOICE_COUNT]

    async def cog_command_error(
        self, ctx: commands.Context, error: Exception
    ) -> None:
        """One handler for the whole group.

        Every subcommand fails in the same few ways - a missing argument, a
        number that is not a number, a throttle - so they answer with the same
        few messages instead of nine copies of them. ``app.py`` skips its own
        fallback for a cog that has this.
        """
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(
                embed=ui.notice(
                    f"⏳  **Slow down a moment** — try again in "
                    f"**{error.retry_after:.1f}s**."
                )
            )
            return
        if isinstance(error, commands.MaxConcurrencyReached):
            await ctx.send(
                embed=ui.notice(
                    "⏳  **Your last playlist add is still being processed.**\n"
                    "Nothing's lost — give it a second."
                )
            )
            return
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.send(embed=ui.playlist_needs_a_server())
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(embed=ui.error(_usage(ctx, error.param.name)))
            return
        if isinstance(error, (commands.BadArgument, commands.ConversionError)):
            await ctx.send(embed=ui.error(_usage(ctx, None)))
            return

        log.exception("playlist command error in %s", ctx.command, exc_info=error)
        await ctx.send(embed=ui.generic_error())

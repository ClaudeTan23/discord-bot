# Discord Bot Music Player

A Discord music bot that streams audio from YouTube into a voice channel. Supports
single videos and full playlists, a paged queue, saved playlists shared by the
server, volume control, and both prefix (`?play`) and slash (`/play`) commands.

Built on [discord.py](https://github.com/Rapptz/discord.py), [yt-dlp](https://github.com/yt-dlp/yt-dlp)
and ffmpeg.

---

## Table of contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Creating the Discord bot](#creating-the-discord-bot)
- [Configuration](#configuration)
- [Running the bot](#running-the-bot)
- [Commands](#commands)
- [Saved playlists](#saved-playlists)
- [How the code works](#how-the-code-works)
- [Tests](#tests)
- [Troubleshooting](#troubleshooting)

---

## Requirements

| | |
|---|---|
| Python | 3.10 or newer (developed and tested on 3.14) |
| ffmpeg | bundled in `ffmpeg/`, or any copy on your `PATH` |
| OS | Windows, macOS or Linux |

Python 3.13 removed the stdlib `audioop` module that discord.py needs for voice.
`audioop-lts` covers that and is already pinned in `requirements.txt`.

---

## Installation

**1. Clone the repo**

```bash
git clone https://github.com/ClaudeTan23/discord-bot.git
```

```bash
cd discord-bot
```

**2. Create a virtual environment**

```bash
python -m venv venv
```

Activate it — Windows (cmd prompt):

```bash
venv\Scripts\Activate
```

macOS / Linux:

```bash
source venv/bin/activate
```

**3. Install dependencies**

```bash
pip install -r requirements.txt
```

**4. Provide ffmpeg**

Download a build from [ffmpeg.org](https://ffmpeg.org/download.html), then point the
bot at it with `FFMPEG_PATH` in `src/.env` (see [Configuration](#configuration)):

```
FFMPEG_PATH = C:\ffmpeg\bin\ffmpeg.exe
```

You can give either the executable or the folder containing it — both the extracted
root and its `bin/` subfolder are accepted.

If `FFMPEG_PATH` is left blank the bot falls back to auto-detection:

1. any `ffmpeg/*/bin/ffmpeg.exe` (or `ffmpeg`) inside the repo
2. `ffmpeg` on your system `PATH`

So dropping a build into `ffmpeg/` also works without editing `.env`. The `ffmpeg/`
directory is gitignored.

---

## Creating the Discord bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**
2. Open **Bot** → **Reset Token** → copy the token (you will only see it once)
3. Still under **Bot**, enable all three **Privileged Gateway Intents**:
   - Presence Intent
   - Server Members Intent
   - **Message Content Intent** ← required, or prefix commands silently do nothing
4. Open **OAuth2 → URL Generator**, tick scopes **`bot`** and **`applications.commands`**,
   then grant these bot permissions:
   - View Channels, Send Messages, Embed Links
   - Connect, Speak
5. Open the generated URL and invite the bot to your server

> `applications.commands` is what makes `/play` and friends appear. Without it you
> only get the `?` prefix commands.

---

## Configuration

Copy the template and fill in your token:

```bash
cp src/.env.example src/.env
```

`src/.env`:

```
Bot-Token   = your-token-here
FFMPEG_PATH = C:\ffmpeg\bin\ffmpeg.exe
```

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `Bot-Token` | yes | - | Bot token from the Discord Developer Portal. |
| `FFMPEG_PATH` | yes | - | ffmpeg executable, or the folder holding it. Blank falls back to `ffmpeg/` in the repo, then `PATH`. A path that does not exist raises at startup rather than silently using a different build. |
| `SYNC_GUILD_ID` | no | - | Your server's id. Slash commands are published globally, which Discord can take up to an hour to roll out — so a command you just added looks missing. Setting this publishes to that one server instantly as well. |
| `PLAYLIST_DB` | no | `data/playlists.db` | SQLite file holding the saved playlists. It is the only copy — point it at something you back up. |


> **Never commit `src/.env`.** It is gitignored. If a token is ever pushed, treat it
> as compromised and reset it in the Developer Portal — deleting the file later does
> not remove it from git history.

Other tunables (cooldowns, cache sizes, idle timeout, page size) live in
[`src/music_player/config.py`](src/music_player/config.py).

---

## Running the bot

```bash
cd src && python app.py
```

You should see:

```
INFO  music_player.player: using ffmpeg at ...\ffmpeg.exe
INFO  music_bot: cogs registered and command tree synced
INFO  music_bot: YourBot#1234 online
```

Every command works as both `?add` and `/add` — they are the same command, declared
once as a discord.py *hybrid*.

Slash commands are synced globally on startup, which Discord can take up to an hour
to roll out; until it does, a newly added command appears to be missing. Set
`SYNC_GUILD_ID` to your own server's id and it is published there immediately as
well. A bad id there is logged and skipped rather than aborting the global sync, so
one typo cannot leave every server without commands.

### Typical session

```
?join <voice-channel>
?add https://www.youtube.com/watch?v=dQw4w9WgXcQ
?play
```

---

## Commands

Every command works as both a prefix command (`?add`) and a slash command (`/add`).

| Command | Argument | Description |
|---|---|---|
| `?join` | voice channel | Join a voice channel (autocompletes channel names) |
| `?leave` | | Leave the current voice channel |
| `?add` | YouTube URL | Queue a video or an entire playlist (autocompletes the title) |
| `?play` | | Start playing the queue |
| `?pause` | | Pause playback |
| `?resume` | | Resume playback |
| `?skip` | | Skip to the next song |
| `?skipto` | number | Skip to a specific position in the queue |
| `?queue` | | Show the first page of the queue |
| `?queueto` | page number | Show a specific queue page (10 songs per page) |
| `?clear` | | Clear the queue, keeping whatever is currently playing |
| `?playlist` | | The server's playlists, with a picker that plays one |
| `?playlist create` | name | Start a new, empty playlist |
| `?playlist add` | name, YouTube URL | Add a video or a whole playlist to it |
| `?playlist show` | name | List what is in it, 10 songs per page |
| `?playlist remove` | name, number | Take one song out |
| `?playlist play` | name | Replace the queue with it and start playing |
| `?playlist queue` | name | Add it to the end of the queue instead |
| `?playlist rename` / `delete` | name | Its creator, or **Manage Server**, only |
| `?volume` | 0–100 | Set playback volume (default 10%) |
| `?stop` | | Stop, leave the voice channel and clear the queue |
| `?help` | | Show the command manual |

### Accepted link formats

`?add` normalises what you paste, so all of these work:

```
https://www.youtube.com/watch?v=<id>
https://youtu.be/<id>
https://music.youtube.com/watch?v=<id>
https://www.youtube.com/shorts/<id>
<https://www.youtube.com/watch?v=<id>>        # Discord's no-embed wrapper
https://www.youtube.com/watch?v=<id>&list=<id>  # queues just that one video
https://www.youtube.com/playlist?list=<id>      # queues the whole playlist
```

A link with both `v=` and `list=` queues **only the video you clicked**. To queue an
entire playlist, use a bare `playlist?list=` URL.

### Rate limits and throttles

`?add` is limited to 3 uses per 10s per user, with one extraction in flight at a
time; `?queue` and `?queueto` allow 4 per 6s per user. These stop one person from
consuming the channel's shared message budget.

> **Prefer slash commands in busy servers.** Discord allows 5 messages per 5 seconds
> *per channel*, shared by everyone, so prefix replies queue behind each other.
> Slash replies use a separate per-interaction bucket and do not.

---

## Saved playlists

A playlist belongs to the **server it was created in**. Everyone in that server can
see it, add songs, remove songs, and play it; nobody outside can reach it at all, and
it survives a restart. The queue is the other per-guild thing, but it is live,
unnamed, and empty again when the process stops.

```
?playlist create Friday Night
?playlist add "Friday Night" https://www.youtube.com/playlist?list=<id>
?playlist play Friday Night
```

| | Songs (`add`, `remove`) | The list itself (`rename`, `delete`) |
|---|---|---|
| Anyone in the server | yes | no |
| Whoever created it | yes | yes |
| **Manage Server** | yes | yes |

Names are matched case-insensitively but shown exactly as typed, so
`?playlist play friday night` finds `Friday Night`. With the prefix form, a name with
spaces needs quotes on the two-argument commands (`add`, `remove`, `rename`) because
another argument follows it. The slash commands autocomplete the server's playlist
names and never need quoting.

Loading a playlist **copies** it into the queue. Skipping songs, clearing the queue,
or stopping the bot cannot change the playlist itself. `?playlist play` replaces
whatever was queued; `?playlist queue` leaves it alone and appends.

### Why deleting is gated but editing is not

A shared playlist anyone can delete is a griefing vector in a public server, and one
only a moderator can touch is not really shared. Splitting the two — songs open to
everyone, the list itself kept to whoever started it — is what makes it usable where
not everybody is trusted equally. The refusal message says so explicitly, or it reads
as though the playlists are not shared at all.

`created_by` is stored with the playlist for exactly this reason: "whoever started
this list" has to survive a restart along with it. A playlist with no recorded
creator — hand-edited, or migrated from an older schema — is moderators-only.

Limits: 25 playlists per server (also Discord's ceiling on the options in one
dropdown, which is what the picker is), 10,000 songs per playlist — enough for two
full YouTube playlists, which cap at 5,000 videos each. Adding a longer one takes
what it can and says how many did not fit.

### Storage

One SQLite file — `data/playlists.db` by default, overridable with `PLAYLIST_DB`.
`sqlite3` is in the standard library, so this adds no dependency.

```sql
playlists(id, guild_id, name, name_key, created_at, updated_at, created_by)
         UNIQUE (guild_id, name_key)
tracks   (playlist_id → playlists.id ON DELETE CASCADE, position, url, title, duration)
         PRIMARY KEY (playlist_id, position)
```

**Why not a JSON file.** It was one, and that was right while a playlist held 500
songs. Raising the cap to 10,000 changed the shape of the problem: a JSON document
is rewritten *in full* on every change, so adding one song to a server holding
250,000 meant serialising 43 MB and blocking the event loop for ~230 ms. The cost
tracked the size of everything instead of the size of the edit.

Measured on a full server (240,000 songs):

| operation | JSON | SQLite |
|---|---|---|
| add one song | ~230 ms | **0.5 ms** |
| autocomplete (per keystroke) | ~0 ms (in memory) | **0.13 ms** |
| play one playlist | ~0 ms (in memory) | 7.6 ms |
| `?playlist` listing | ~0 ms (in memory) | 79 ms |

The trade is explicit: writes stopped scaling with total data, and reads now cost a
query instead of a dict lookup. Two reads earn their own query rather than going
through the general one:

- **`names()`** for autocomplete, which fires on every keystroke and shows nothing
  but names. Reading the songs to render a name list took 239 ms per keypress on a
  full server; this takes 0.13 ms.
- **`summaries()`** for the listing and the picker, which show a name and two totals
  per row. A `GROUP BY` gets those in 79 ms where materialising every track took 228.
  Songs are read only for the playlist actually opened.

That leaves the listing proportional to the tracks table, because counting rows means
visiting them. Carrying denormalised totals on the `playlists` row would make it
constant, at the price of an invariant maintained by hand — not worth it for a
command a person runs occasionally and Discord's own latency hides.

Two things come along with the schema, and they are half the reason to switch:

- **The invariants are the database's job.** `UNIQUE (guild_id, name_key)` is what
  stops one server having two playlists whose names differ only in case; it used to
  be a Python check that was only as good as the code around it. `ON DELETE CASCADE`
  is what stops a deleted playlist leaving its songs behind.
- **A change commits or it does not.** There is no "live in memory but not on disk"
  state to report, because a failed transaction rolls back — which is why the failure
  message can say *nothing was changed* and mean it.

`sqlite3` blocks, so every library method is a coroutine running its SQL on a worker
thread under a lock. The lock is also what makes one shared connection safe and each
call atomic against the rest of the bot. WAL mode replaces the temp-file-fsync-rename
dance the JSON store needed for crash safety.

### Upgrading from the JSON store

Nothing to do. On startup, a `playlists.json` next to the database is imported once
and renamed to `playlists.imported-<timestamp>.json` — kept, not deleted, because it
is the only copy of that data. All three JSON schemas the file store ever used are
read; playlists that belonged to a *person* rather than a server (versions 1 and 2)
have no server to move to and are reported rather than silently dropped.

The import is skipped if the database already holds playlists, so a stale JSON file
reappearing later cannot merge itself back in.

## How the code works

### Layout

```
src/
  app.py                  entrypoint: logging, config, cog registration
  help.txt                text shown by ?help
  music_player/
    config.py             constants, tunables, ffmpeg discovery
    logs.py               dated log files, context, redaction
    state.py              Track, GuildState, MusicState
    audio.py              buffered audio source

    services/             everything outside Discord
      youtube.py          metadata and stream resolution (yt-dlp)
      library.py          saved playlists: schema and SQLite persistence

    ui/                   everything the user sees
      embeds.py           embed builders, formatting, typing indicator
      views.py            buttons and menus under those embeds
      manual.py           the ?help manual, parsed from help.txt

    cogs/                 the command surface, one per area
      player.py           queue and playback commands
      playlists.py        ?playlist and its subcommands
      voice.py            joining and leaving a voice channel
tests/
  test_music_player.py    unit tests
data/                     written at runtime, gitignored
  playlists.db            every server's saved playlists (SQLite)
logs/                     written at runtime, gitignored
  2026/08/09/bot.log      everything, that day
  2026/08/09/errors.log   warnings and errors only
```

Dependencies point one way: `cogs` may use `ui` and `services`, `ui` may use
`services`, and `services` depends on nothing but `config`. The only imports
that run the other way are `TYPE_CHECKING`-only annotations in `ui/views.py`,
so nothing is circular at runtime.

### State

All per-guild data lives on one `GuildState` (`state.py`): the voice client, the
queue, volume, and the flags coordinating playback. `MusicState` is the registry of
those, keyed by guild id, and is constructed once in `app.py` and injected into every
cog — so the cogs share state without reaching for globals.

Guilds are fully independent. Nothing one server does blocks another.

Saved playlists are per-guild too, but they outlive the process. `PlaylistLibrary`
(`library.py`) is keyed by guild id, is constructed once in `app.py` alongside
`MusicState`, and is the only thing in the bot that touches the disk during normal
operation.

### Playback flow

```
?add   -> YouTubeService.fetch()      -> Track objects appended to GuildState.queue
?play  -> Player._play_current()      -> resolve stream URL
                                      -> FFmpegPCMAudio + PCMVolumeTransformer
                                      -> voice.play(after=_on_track_end)
track ends -> _on_track_end()         -> _advance() -> _play_current() for the next
queue empty -> idle timer            -> auto-disconnect after 69s
```

While a track plays, the *next* track's stream URL is resolved in the background, so
the handover between songs is instant rather than a 1–2s pause.

### Keeping the bot responsive

`yt_dlp` is synchronous and does network I/O. Calling it directly inside an `async`
handler would freeze the entire bot — every guild, every command — for the duration.
Instead `services/youtube.py` runs every extraction on a dedicated thread pool, so command
handling continues while lookups are in flight.

Using the `yt_dlp` Python API (rather than shelling out to the `yt-dlp` binary) also
means no user-supplied string is ever interpolated into a command line.

### Avoiding duplicate work

- **Request coalescing** — if several guilds ask for the same URL at the same moment,
  one extraction runs and everyone shares the result.
- **Metadata cache** — bounded LRU of resolved queries.
- **Stream cache** — signed googlevideo URLs are reused until shortly before they
  expire (they are valid ~6 hours), with a safety margin so a track never starts on a
  link that would die mid-song.

### Concurrency safeguards

A single guild has one audio output, so starting a track must be exclusive even
though everything else is parallel:

- `GuildState.starting` prevents two `?play` calls from both resolving a stream.
- After the stream resolves, state is re-validated before `voice.play()` — the queue
  may have changed during the network round trip.
- `_advance(expect=track)` drops an advance whose track is no longer at the head, so
  two simultaneous `?skip` presses move forward one song, not two.

### Autocomplete

`?add` suggests the video or playlist title as you type. Discord discards an
autocomplete response after 3 seconds, so `services/youtube.py`:

1. rejects half-typed input offline (a YouTube id is always 11 characters) — no
   network call at all;
2. reads only the first entry of a playlist, not all of them;
3. enforces a 1.5s deadline, while leaving the lookup running in the background so
   the next keystroke hits a warm cache.

---

## Tests

```bash
python -m unittest discover -s tests -v
```

409 tests covering URL normalisation, duration formatting, queue pagination, state
transitions, autocomplete gating, caching and request coalescing, embed rendering,
button permissions and paging, the playlist schema's own constraints, importing the
old JSON store, per-server isolation and playlist permissions, command syncing, log
rotation and redaction, and the concurrency safeguards above. They use test doubles
and need no Discord token.

The suite does **not** cover live voice playback — that requires a real Discord
connection and should be smoke-tested manually.

---

## Troubleshooting

### Start with the logs

Everything is written to a folder per day, so "what happened last night" is one
folder away:

```
logs/2026/08/09/bot.log        everything
logs/2026/08/09/errors.log     warnings and errors only
```

Check `errors.log` first — if it is empty, nothing broke. Every line carries the
guild, user and command it came from, so you can follow one person's session
through a busy log:

```
2026-08-09 21:04:11 INFO  music_bot  [guild=Cool Server user=tan cmd=play] run play (guild=849... channel=#music via=slash)
2026-08-09 21:04:12 INFO  music_player.player  [guild=Cool Server user=tan cmd=play] using ffmpeg at ...
2026-08-09 21:04:12 INFO  music_bot  [guild=Cool Server user=tan cmd=play] done in 812ms
```

A `run` line with no matching `done` is a command that hung — that pairing is
the fastest way to find one.

For a stubborn problem, turn up the detail in `.env` and reproduce it:

```
LOG_LEVEL = DEBUG
```

DEBUG adds the queue-supersede and cache decisions, which explain most "why did
it skip that song" reports.

The bot token and signed stream URLs are redacted before anything is written, so
a log file is safe to attach to a bug report. Day folders older than
`LOG_RETENTION_DAYS` (default 14) are deleted at startup.

### Common problems

**`?add` says "I couldn't read that link"**
Usually an out-of-date yt-dlp; YouTube changes frequently. Update it:

```bash
pip install -U yt-dlp
```

**"ffmpeg is not installed or could not be found"**
Set `FFMPEG_PATH` in `src/.env`, put a build under `ffmpeg/<version>/bin/`, or install
ffmpeg on your `PATH`. The resolved path is logged at startup:

```
INFO  music_player.player: using ffmpeg at D:\...\bin\ffmpeg.exe
```

**"FFMPEG_PATH is set to '...', which does not exist"**
The path in `.env` is wrong. On Windows use the full path including `ffmpeg.exe`, and
do not wrap it in quotes. Both `C:\ffmpeg\bin\ffmpeg.exe` and `C:/ffmpeg/bin` work.

**Prefix commands (`?play`) do nothing, but slash commands work**
The **Message Content Intent** is not enabled in the Developer Portal.

**Slash commands don't appear**
The bot was invited without the `applications.commands` scope — re-invite it with a
URL that includes that scope. A global sync can also take a few minutes to propagate.

**The bot joins but no sound plays**
Check it has **Connect** and **Speak** permissions in that channel, that `PyNaCl` is
installed, and that volume isn't 0 (`?volume 50`).

**A song in a playlist is skipped with "is not available"**
Expected. Large playlists accumulate deleted, private and region-locked videos; the
bot reports them and moves on.

**The bot leaves on its own**
It disconnects after 69 seconds with nothing playing. Adjust
`IDLE_DISCONNECT_SECONDS` in `config.py`.

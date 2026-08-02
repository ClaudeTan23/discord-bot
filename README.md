# Discord Bot Music Player

A Discord music bot that streams audio from YouTube into a voice channel. Supports
single videos and full playlists, a paged queue, volume control, and both prefix
(`?play`) and slash (`/play`) commands.

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

Slash commands are synced globally on startup and can take a few minutes to appear
in Discord the first time.

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

## How the code works

### Layout

```
src/
  app.py                    entrypoint: logging, config, cog registration
  help.txt                  text shown by ?help
  music_player/
    config.py               constants, tunables, ffmpeg discovery
    state.py                Track, GuildState, MusicState
    ytdl.py                 YouTube metadata + stream resolution
    ui.py                   embed builders, formatting, typing indicator
    player.py               Player cog: queue and playback commands
    join_channel.py         JoinChannel cog
    leave_channel.py        LeaveChannel cog
tests/
  test_music_player.py      96 unit tests
```

### State

All per-guild data lives on one `GuildState` (`state.py`): the voice client, the
queue, volume, and the flags coordinating playback. `MusicState` is the registry of
those, keyed by guild id, and is constructed once in `app.py` and injected into every
cog — so the cogs share state without reaching for globals.

Guilds are fully independent. Nothing one server does blocks another.

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
Instead `ytdl.py` runs every extraction on a dedicated thread pool, so command
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
autocomplete response after 3 seconds, so `ytdl.py`:

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

96 tests covering URL normalisation, duration formatting, queue pagination, state
transitions, autocomplete gating, caching and request coalescing, and the concurrency
safeguards above. They use test doubles and need no Discord token.

The suite does **not** cover live voice playback — that requires a real Discord
connection and should be smoke-tested manually.

---

## Troubleshooting

**`?add` says "Invalid link or unavailable video/playlist"**
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

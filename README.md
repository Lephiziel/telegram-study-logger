# Telegram Study Logger

[![Cross-platform checks](https://github.com/Lephiziel/telegram-study-logger/actions/workflows/tests.yml/badge.svg)](https://github.com/Lephiziel/telegram-study-logger/actions/workflows/tests.yml)

Automatically export one Telegram chat to readable Markdown and JSONL, with local
transcription of voice messages and video notes (ordinary videos are opt-in). Useful for
reviewing conversations while learning a language.

**[Подробная инструкция на русском → README_RU.md](README_RU.md)**

After one-time setup, open Telegram as usual. A background watcher starts the
logger when Telegram is running. No separate window needs to stay open.

This is a standalone background companion using your Telegram account through
the Telegram API. It is a source release with installation scripts, not a plugin
loaded inside Telegram Desktop or a signed Windows/macOS application.
The current setup prompts and exported labels are in Russian; commands work as
shown below.


## Version 0.4.1: resume after PC shutdown

Fixed a watcher exit on the next login after shutdown. SIGTERM previously left
`watcher-stop` on disk, so the next watcher exited successfully and systemd's
`Restart=on-failure` did not restart it. Signals now stop only the current
process, and a new watcher clears the previous process's stop request.
`uninstall` still stops the live watcher and removes its startup registration.

Upgrade using the instructions below. For temporary recovery on 0.4.0, run
`PYTHON -m tgstudy install`, then `PYTHON -m tgstudy sync`, using the installed
runtime Python. The account session, archive and settings are retained.

## Version 0.4: an English/Russian study profile and durable media

- Automatic language selection is limited to `en,ru` by default. Other language
  votes cannot choose a Portuguese/Romanian/etc. decoder. Use `speech auto
  --languages en,ru` to set the expected language list explicitly.
- After language regions are identified, long regions are split at VAD pauses
  into chunks up to 18 seconds (configurable from 10 to 20). Without a suitable
  pause the hard boundary uses overlapping context and timestamp joining.
- Unexpected language votes, output language metadata, or non-Latin/non-Cyrillic
  letters trigger one retry with stricter context. Low-confidence words become
  `[unclear]`; JSONL segments retain `raw_text`, `uncertain_words`, and retry
  information. Confidence is a model estimate, not a calibrated error probability.
- Recognition uses `task=transcribe`, zero temperature, and a verbatim-style
  prompt. There is no grammar correction, translation, or rewriting stage.
  **Whisper itself can still normalize mistakes, omit fillers, or produce wrong
  words. The prompt cannot guarantee perfect verbatim speech.** Script checks
  also cannot identify every foreign phrase written with Latin letters.
- Only `voice` and `video_note` are transcribed by default. All webpage previews,
  including YouTube/Reels previews exposing a video, stay links without ASR.
  `videos off` also removes old ordinary-video transcripts from rebuilt exports,
  retaining message text and links. Existing saved video settings are preserved
  until this command is run; `videos on` explicitly enables attached videos.
- A separate queue downloads observed messages while ASR is busy. Complete media
  is stored atomically in `media-cache/<chat-id>/<message-id>-<media-identity>.media`.
  Recognition and retries can use it after a restart without refetching Telegram
  metadata. Default retention is 7 days since last use and a 1 GB size budget.
  Active files are protected from eviction; the budget may temporarily exceed
  its limit for active media. Missing messages are retried before a final error.
  A recording removed before its first successful download cannot be recovered.

Upgrade with the instructions below, then run:

```bash
"$TGSTUDY_PY" -m tgstudy speech auto --languages en,ru
"$TGSTUDY_PY" -m tgstudy videos off
"$TGSTUDY_PY" -m tgstudy retranscribe --unexpected-languages
```

The last command repairs stored recordings whose language metadata contains an
unexpected language, retaining good transcripts. It cannot identify older foreign
phrases that were stored without language metadata. Use `retranscribe --message-id
12345` for one selected-chat recording, or `--days 2` for yesterday and today.

## Version 0.3.1: keep both parts of a bilingual recording

Version 0.3.0 still selected one language for each decoder window of up to
30 seconds. A short English → Russian or Russian → English message could lose
an entire language portion. Simply shrinking the windows also lost boundary words.

The logger now probes overlapping short windows, joins adjacent regions of one
language, and checks speech on both sides of nearby pauses to choose a boundary.
Each region is transcribed with its detected language. Unpaused boundaries use
overlapping context and word timestamps for joining. JSONL `segments` now include
their own `language`.

This applies to voice messages, video notes, and ordinary video. Recognition stays
local and needs more CPU. An opt-in real Whisper `small` regression test checks
synthesized English/Russian speech through media decoding, SQLite, and exports.
Single foreign words and rapid switches can still be misrecognized. The original
reported voice note was not available for testing.

After upgrading, run `speech auto` and `retranscribe --today` below. Existing
transcripts require reprocessing; use `--days 7` for recordings on earlier dates.

## Version 0.3.0: offline catch-up and mixed speech

Linux now prefers a systemd user service, including in window managers without
XDG Autostart. At startup the newest messages are imported first, followed by
all messages after the saved checkpoint. Keeping the same export mode during
an upgrade preserves its initial date and offline backlog.

`auto` enables multilingual transcription with language detection per speech
region. Old configurations default to this mode even when they saved
`language: "en"`. Named English-only models such as `small.en` are replaced by
their multilingual counterpart, which may require a new download. Custom
English-only models must be replaced explicitly with `speech auto --model small`.

The task is always `transcribe`, never `translate`. English and Russian can both
appear in a mixed transcript. Whisper may still misrecognize or normalize a
short language switch, especially one word: verbatim accuracy is not guaranteed.
Try `medium` or `large-v3` for difficult speech; these need more memory and CPU.
`transcript_language` describes the initial detected language, not all languages
present in mixed speech. Existing transcripts are not changed until reprocessed.

### Upgrade an existing Linux/macOS installation

Git must be installed. Use the existing runtime Python (no system Python upgrade):

```bash
TGSTUDY_PY="$HOME/.local/share/telegram-study-logger/runtime/bin/python"
"$TGSTUDY_PY" -m tgstudy uninstall
# Wait for the watcher/worker to stop (up to a minute), then continue.
"$TGSTUDY_PY" -m pip install --upgrade 'git+https://github.com/Lephiziel/telegram-study-logger.git'
"$TGSTUDY_PY" -m tgstudy speech auto --languages en,ru
"$TGSTUDY_PY" -m tgstudy videos off
"$TGSTUDY_PY" -m tgstudy install
"$TGSTUDY_PY" -m tgstudy sync
"$TGSTUDY_PY" -m tgstudy retranscribe --today
```

Alternatively download the ZIP and rerun your OS installer. The account session,
chat, and archive are retained. `sync` fetches messages immediately without
requiring the watcher. `retranscribe` clears old speech results for the selected
period and queues the stored recordings for download/recognition again. It keeps
original message text. Use `--days 7` for seven calendar days, or `--days 0` for all
stored recordings. Leave Telegram open until processing finishes.

Additional commands:

```bash
"$TGSTUDY_PY" -m tgstudy speech auto --model medium
"$TGSTUDY_PY" -m tgstudy speech ru  # Explicit single-language mode
"$TGSTUDY_PY" -m tgstudy status
```

`status` now checks whether Telegram is detected and watcher/worker process locks
are actually held; a stale `working` file alone does not prove a process is alive.
It also shows `history_error`, last synchronization time, and the checkpoint.
On Linux, check `systemctl --user status telegram-study-logger.service`.
Do not share `config.json` or `account.session` when reporting issues.

## Export modes

| Mode | Behavior | Main files |
| --- | --- | --- |
| Daily | Import today's messages from midnight, update throughout the day, create a new dated file tomorrow | `today.md`, `today.jsonl`, `YYYY-MM-DD.md`, `YYYY-MM-DD.jsonl` |
| Last N days | Import and maintain one rolling export of N calendar days, including today | `history_7_days.md`, `history_7_days.jsonl` for N=7 |
| All history | Use N=0 to import all available history | `history_all.md`, `history_all.jsonl` |

Seven days means today plus six previous calendar dates, not exactly 168 hours.
Setup detects your computer's timezone and lets you confirm or change its IANA
name, such as `Europe/Berlin`. If detection fails, it offers `UTC`.

Dated files are retained when the rolling window moves forward. Transcription
that finishes tomorrow updates the date the message was sent. If Telegram or
the computer is off, the logger catches up at the next launch; it cannot create
files while the computer is off. Closing Telegram to its tray still counts as
running.

## Requirements

- Telegram Desktop and access to your selected chat. Telegram Web and secret
  chats are not supported.
- Python **3.11–3.13**, preferably **3.12**. The Windows installer explicitly uses
  Python 3.12 through the `py` launcher. Python 3.14 is not supported by this release.
- Your own Telegram `api_id` and `api_hash`, plus one-time account login.
- Internet for installation, model download, and receiving Telegram messages.
  Speech recognition runs locally on CPU after the model is downloaded.

Windows x64, macOS, and desktop Linux have dedicated startup integration. Native
Windows/macOS installers and a live account require verification on your own
computer. Automated checks cover all three operating systems in GitHub Actions;
see [VALIDATION.md](VALIDATION.md) for what has been verified.

## Install

Download the repository with **Code → Download ZIP** and extract it fully, or:

```bash
git clone https://github.com/Lephiziel/telegram-study-logger.git
cd telegram-study-logger
```

### Windows

1. Install Python **3.12 x64** from [python.org](https://www.python.org/downloads/),
   including Python Launcher (`py`).
2. Open `install-windows.cmd` in the extracted project folder.
3. Complete setup once, then close the installer window.

Startup uses the user's Startup folder and `pythonw.exe`, without a console.
Windows Script Host must be enabled. Administrator rights are not needed.
Windows ARM64 and 32-bit are not verified.

### macOS

Install Python 3.12 from [python.org](https://www.python.org/downloads/).
In Terminal, from the project folder:

```bash
bash install-macos.command
```

If `python3` points to an incompatible version, supply the Python executable:

```bash
TGSTUDY_PYTHON=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3 bash install-macos.command
```

Startup uses a user LaunchAgent. Allow background execution if macOS asks.
Access to Documents may also require permission; you can choose another output
folder during setup. Homebrew and a separate transcription application are not
required.

### Linux

With Python 3.11–3.13 available, run:

```bash
bash install-linux.sh
```

To select another installed Python:

```bash
TGSTUDY_PYTHON=/usr/bin/python3.12 bash install-linux.sh
```

Startup prefers a systemd user service. If a user systemd manager is unavailable,
it falls back to XDG Autostart as supported by GNOME/KDE. A window manager without
either may need its own startup entry using the installed Python with `-m tgstudy watch`.
See [the Russian startup instructions](README_RU.md#автозапуск-для-самостоятельного-оконного-менеджера).

### One-time setup

1. Open [my.telegram.org](https://my.telegram.org) → **API development tools**.
   Create your own application and copy its `api_id` and `api_hash` into the local
   setup prompt. An OpenAI API key is not needed.
2. Enter your phone number, the Telegram login code, and your 2FA password if enabled.
   The code/password are not saved. An additional Telegram session is created.
3. Search for your chat by name and select its number.
4. Confirm timezone, export mode, output folder, speech model, and language.
   Daily mode is the default for a new installation. `auto` supports mixed speech;
   `en`/`ru` explicitly force a single language. `small` is the default model; `base` is lighter.
5. Download the model when prompted. This normally happens once.

The installer copies the package into a persistent virtual environment. You can
move the downloaded source folder afterward. Rerunning the installer upgrades
the package and offers mode selection while preserving the account session.

## Commands

Use the installed Python, rather than a different system environment.
On Linux/macOS:

```bash
TGSTUDY_PY="$HOME/.local/share/telegram-study-logger/runtime/bin/python"
"$TGSTUDY_PY" -m tgstudy status
"$TGSTUDY_PY" -m tgstudy paths

# Automatic daily export; tomorrow gets a new file
"$TGSTUDY_PY" -m tgstudy mode daily

# Maintain a rolling export of the last seven calendar days
"$TGSTUDY_PY" -m tgstudy mode history --days 7

# One-off exports; do not change the automatic mode
"$TGSTUDY_PY" -m tgstudy export --days 7
"$TGSTUDY_PY" -m tgstudy export --today
```

On Windows PowerShell, use:

```powershell
$TgStudyPython = "$env:LOCALAPPDATA\TelegramStudyLogger\runtime\Scripts\python.exe"
& $TgStudyPython -m tgstudy mode daily
& $TgStudyPython -m tgstudy export --days 7
& $TgStudyPython -m tgstudy status
```

| Command after `-m tgstudy` | Purpose |
| --- | --- |
| `settings` | Change mode interactively and enable startup |
| `mode history --days 0` | Maintain all available history |
| `export --days 0` | One-off import/export of all available history |
| `export` | Rebuild exports from the local database |
| `retry` | Queue failed recordings for another attempt |
| `uninstall` | Disable startup and stop background processes; keep data |
| `install` | Enable startup again |
| `setup` | Change account/chat, timezone, model, or other setup choices |

Before `setup`, run `uninstall` and allow up to a minute for shutdown. `settings`,
`mode`, `export`, `sync`, `speech`, `retranscribe`, and `retry` coordinate with
the running worker automatically.
A historical export waits for available recordings to be processed and can take
a long time. Errors are reported instead of blocking forever.

## Output and privacy

Default output: `Documents/TelegramStudy/<chat-id>/`. One-off reports go into its
`reports/` subfolder. Markdown is for reading; JSONL contains one message per line
with separate original text and speech transcript, IDs, sender, timestamps,
language, duration, and transcription status. This is the project's own format,
not Telegram Desktop's official export schema.

Credentials, Telegram session, SQLite database, and model cache stay in your
user's application data directory; `paths` shows its location. Never commit or
share this directory: `account.session` grants access to your Telegram account.
The repository includes no account credentials or chat history.

Messages are not sent to a bot or transcription service. Model files are initially
downloaded from Hugging Face. Downloaded recordings remain in a private local
media cache for 7 days since last use, bounded to 1 GB by default; the exported
transcript remains after eviction. Set `media_cache_days` / `media_cache_max_mb`
to change these limits. The logger does not send messages or invoke mark-as-read methods.
There is no extra encryption at rest; protect your computer and export folder.

## Limits and troubleshooting

- Only one selected chat is logged at a time. Changing chat keeps its previous
  archive. Separate computers do not merge their databases automatically.
- New/edited messages are updated. The last 200 messages are periodically checked
  for edits made while offline; older edits may require another history import.
  Deletions are not mirrored into the archive. Already deleted messages cannot be
  recovered.
- Photos, stickers, and documents are described by type, not downloaded. Protected
  media, unavailable recordings, files over the default 100 MB limit, and videos
  without audio cannot be transcribed. Whisper can misrecognize speech.
- If nothing starts, run `status`/`paths`, check `runtime.log`, make sure Telegram
  is running, and verify OS startup/background permissions. Custom clients may
  need their exact executable name added to `process_names` in `config.json`.
- If a model download fails, check connectivity and retry. If a recording remains
  in an error state after the automatic retries, use `retry`.
- Telegram Desktop proxy settings are not inherited by the separate API client.
  This release needs direct access to Telegram API servers. Model downloads support
  SOCKS proxy environment variables, but this does not configure Telegram access.
- Use `uninstall` before manual configuration edits, then `install`. Use `setup`
  for changing timezone so the selected range is imported with the new boundaries.
- To revoke account access, stop the logger and terminate **Telegram Study Logger**
  under Telegram → Settings → Devices.

## Development

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install '.[test]'
.venv/bin/python -m pytest -q
```

On Windows, use `.venv\Scripts\python.exe`. Tests cover synchronization ordering,
deduplication, durable transcription jobs, media decoding, exports, day boundaries,
DST, mode changes, and startup generation. The CI matrix runs Python 3.12 on
Ubuntu, Windows, and macOS. Tests do not require Telegram credentials or download
a Whisper model by default. The four real speech tests are skipped in the fast
suite and run in a separate Linux CI job, alongside a long English → Russian →
English regression. Run them locally with:

```bash
.venv/bin/python -m pip install '.[test,speech-test]'
TGSTUDY_REAL_SPEECH=1 TGSTUDY_SPEECH_CACHE="$PWD/.speech-test-cache" \
  .venv/bin/python -m pytest tests/test_speech_inference.py -q
```

This developer-only extra downloads Whisper `small` and two Piper synthesis
voices to generate bilingual audio. Piper is not installed with the logger and
is not used for chat transcription. The tests check both language orders in
OGG/Opus and MP4/AAC through the real worker, SQLite, and Markdown/JSONL exports.
They need no Telegram credentials. See [VALIDATION.md](VALIDATION.md) for results.

Built with [Telethon](https://docs.telethon.dev/en/stable/),
[faster-whisper](https://github.com/SYSTRAN/faster-whisper), and
[PyAV](https://pyav.org/docs/stable/).

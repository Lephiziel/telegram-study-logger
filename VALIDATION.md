# Validation — 2026-10-05

Tested locally on Linux x86_64 with Python 3.12.14. Updated for version 0.2.0.
The same test suite also runs natively in the repository's three-OS GitHub Actions matrix.

## Passed

- 44 pytest checks, including real Telethon message objects (voice/video note/video),
  durable SQLite jobs, deduplication, media replacement, caption edits, per-chat
  isolation, timestamps, JSONL/Markdown exports, history/event checkpoint ordering,
  retries, file-size limits, and platform-specific startup file generation.
- Added tests for calendar-day periods (including 23/25-hour DST dates), full-day
  midnight boundaries, daily rollover with no messages, late transcription into
  yesterday's archive, rolling history windows, old configuration migration,
  maintenance lock release, and watcher pause/configuration reload/restart.
- System timezone detection, UTC fallback when unavailable, and preserving the
  timezone of an existing installation.
- Export tests explicitly read UTF-8 on every OS. The initial Windows CI run
  found that three tests incorrectly used Windows' default encoding; production
  export writing/reading already used explicit UTF-8.
- A one-off historical import with a test client verifies that older voice messages
  are fetched/transcribed/exported while the configured daily mode and continuous
  history checkpoint remain unchanged.
- Real PyAV encoding and faster-whisper decoding of OGG/Opus and MP4 with a video
  stream and an AAC audio stream.
- Bundled ONNX voice activity detector on silence.
- Ruff checks for syntax/import defects and import ordering.
- `bash -n` for Linux and macOS installation scripts.
- Python package wheel build, dependency import checks, CLI help, and `pip check`.
- Real CPU/int8 inference with Whisper `tiny.en` on the public JFK speech fixture
  from OpenAI's Whisper repository. The same sample was re-encoded to OGG/Opus and
  MP4, passed through the actual `Worker.process_job` and `Transcriber.run`, stored
  in SQLite, and exported to daily Markdown and JSONL. Both media types produced
  English transcripts containing the expected words “ask” and “country”.

Speech fixture:
https://github.com/openai/whisper/blob/main/tests/jfk.flac

## Dependency compatibility fix

PyAV 19.0.1 removed the `metadata_errors` argument used by faster-whisper 1.2.1.
Real decoding tests detected this failure. This release pins faster-whisper 1.2.1
and PyAV 16.1.0; the real OGG/MP4 tests and inference then passed.

## Not verified here

- A live Telegram account login, authorization, network download, and update stream:
  these require the owner's API credentials and one-time login on their computer.
  Telegram interactions in automated worker tests use a test client, while message
  metadata uses actual Telethon types.
- A user's interactive Windows/macOS install and startup after an actual OS login.
  Startup-file generation is tested, including on native GitHub Actions runners;
  this is distinct from launching Telegram in a real desktop session.
- Whisper `small`/`medium` model quality, multilingual accents, long recordings,
  and every supported hardware configuration. The actual inference smoke test used
  `tiny.en`; setup defaults to multilingual `small`.
- Native installers, signed application bundles, Windows ARM64/32-bit, and Telegram Web.

This is an initial runnable source release. `README_RU.md` includes the local
installation and live account acceptance steps. The archive contains no API
credentials, session, private chat export, or downloaded model.

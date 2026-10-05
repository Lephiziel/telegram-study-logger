# Validation — 2026-10-05

Tested locally on Linux x86_64 with Python 3.12.14. Updated for version 0.3.1.
The same test suite also runs natively in the repository's three-OS GitHub Actions matrix.

## Passed

- 75 fast pytest checks (four model inference tests skipped in the fast suite),
  including real Telethon message objects (voice/video note/video),
  durable SQLite jobs, deduplication, media replacement, caption edits, per-chat
  isolation, timestamps, JSONL/Markdown exports, history/event checkpoint ordering,
  retries, file-size limits, and platform-specific startup file generation.
- Added tests for calendar-day periods (including 23/25-hour DST dates), full-day
  midnight boundaries, daily rollover with no messages, late transcription into
  yesterday's archive, rolling history windows, old configuration migration,
  maintenance lock release, and watcher pause/configuration reload/restart.
- System timezone detection, UTC fallback when unavailable, and preserving the
  timezone of an existing installation.
- A reboot scenario closes/reopens the SQLite database and catches up 391 offline
  messages, beyond the 200-message recent-edit window, without duplicates or a
  live event skipping the history checkpoint. Interrupted imports resume safely.
- Linux systemd startup generation, migration from XDG, uninstall, and XDG fallback
  on service enable failure. Tests mock all startup side effects.
- Upgrade mode selection retains the initial capture floor. Manual sync uses the
  existing session and exports messages without requiring a desktop watcher.
- Multilingual options bypass an old forced-English setting, preserve returned
  Russian/mixed text, upgrade named English-only models, and reject custom
  English-only models in multilingual mode. These are configuration/contract
  tests with a test recognizer, not Russian speech quality measurements.
- Reprocessing clears only speech results in the requested range and queues them
  again, retaining original text and other dates.
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

## Bilingual regression in 0.3.1

The old 30-second-window decoder reproduced a lost language portion on synthesized
English/Russian speech: an English-first sample retained only English; a
Russian-first sample also retained only English. Six-second decoder windows
returned both languages but still lost English boundary words. Local language
regions and explicit per-region decoding resolved these tested failures.

- Four actual CPU/int8 Whisper `small` tests passed in 123 seconds locally:
  English → Russian and Russian → English in both OGG/Opus and MP4/AAC with a
  video stream. Piper `en_US-lessac-medium` and `ru_RU-irina-medium` generate the
  speech in a developer-only fixture; they are not runtime dependencies.
- The actual `Worker.process_job` downloads fixture media through a test Telegram
  client, decodes and recognizes it, stores it in SQLite, and exports daily
  Markdown and JSONL. Assertions require the English greeting, "test voice
  message", "beginning is saved", Russian "говорить по-русски", "обе части
  сообщения", and "расшифровке", in the original order and without duplicated
  sentences. Temporary media cleanup is also checked.
- Fast tests cover language changes at multiple offsets in a 24-second timeline,
  both language orders, repeated switches, silence, word ownership in overlapping
  clips, original timestamps, and preserving a greeting after a real pause.
- A separate Linux GitHub Actions job runs the four real inference tests. The
  Windows/macOS/Linux matrix runs the fast suite. Only synthesized test media and
  public models are downloaded; no Telegram credentials are needed.
- The original user's 24-second voice message was not supplied as audio, so its
  exact recognition result has not been verified.

To reproduce, install `.[test,speech-test]` and run:

```bash
TGSTUDY_REAL_SPEECH=1 TGSTUDY_SPEECH_CACHE="$PWD/.speech-test-cache" \
  python -m pytest tests/test_speech_inference.py -q
```

Test voice models:
https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/lessac/medium
https://huggingface.co/rhasspy/piper-voices/tree/main/ru/ru_RU/irina/medium

## Dependency compatibility fix

PyAV 19.0.1 removed the `metadata_errors` argument used by faster-whisper 1.2.1.
Real decoding tests detected this failure. This release pins faster-whisper 1.2.1
and PyAV 16.1.0; the real OGG/MP4 tests and inference then passed.

## Not verified here

- A live Telegram account login, authorization, network download, and update stream:
  these require the owner's API credentials and one-time login on their computer.
  Telegram interactions in automated worker tests use a test client, while message
  metadata uses actual Telethon types.
- A user's interactive install and startup after an actual OS login on any OS.
  Startup-file generation is tested, including on native GitHub Actions runners;
  this is distinct from launching Telegram in a real desktop session.
- General Whisper accuracy across natural multilingual accents, very rapid
  switches, isolated foreign words, long recordings, and every supported hardware
  configuration. The bilingual tests use synthesized speech and `small`, which
  is also the setup default; they are regression checks, not an accuracy benchmark.
- Native installers, signed application bundles, Windows ARM64/32-bit, and Telegram Web.

This is an initial runnable source release. `README_RU.md` includes the local
installation and live account acceptance steps. The archive contains no API
credentials, session, private chat export, or downloaded model.

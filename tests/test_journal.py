import json
from pathlib import Path

import pytest
from conftest import message

from tgstudy.capture import to_record
from tgstudy.exporter import export_dirty
from tgstudy.store import Store


async def test_deduplicated_daily_export_and_duration(store, cfg):
    m = message(1, kind="voice", sender_id=10)
    record = await to_record(m, cfg)
    store.upsert(record)
    store.upsert(record)
    store.result(
        20,
        1,
        state="done",
        transcript="I have been studying English.",
        language="en",
        segments=[{"start": 0, "end": 12, "text": "I have been studying English."}],
    )
    assert export_dirty(store, cfg) == 1
    assert export_dirty(store, cfg) == 0
    path = Path(cfg.output_dir) / "20/2026-10-05.jsonl"
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    r = json.loads(lines[0])
    assert r["duration_seconds"] == 12
    assert r["outgoing"] is True
    assert r["text"] == "Hello"
    assert r["transcript_source"] == "local_whisper"
    assert r["date_local"].startswith("2026-10-05T01:30")
    assert r["transcript"] in path.with_suffix(".md").read_text()


async def test_caption_edit_preserves_transcript_and_media_edit_resets(store, cfg):
    original = await to_record(message(2, kind="video_note"), cfg)
    store.upsert(original)
    store.result(20, 2, state="done", transcript="Test", language="en")
    edited = dict(original, text="Edited caption")
    store.upsert(edited)
    assert store.get(20, 2)["transcript"] == "Test"
    edited["media_key"] = "replacement"
    store.upsert(edited)
    assert store.get(20, 2)["transcript"] is None
    assert store.get(20, 2)["transcription_state"] == "pending"
    store.result(
        20,
        2,
        state="done",
        transcript="stale",
        expected_media_key=original["media_key"],
    )
    assert store.get(20, 2)["transcript"] is None


async def test_newer_edit_not_rolled_back_by_history(store, cfg):
    original = await to_record(message(3), cfg)
    fresh = dict(original, text="New text", edited_at="2026-10-05T01:00:00+00:00")
    store.upsert(fresh)
    store.upsert(original)
    assert store.get(20, 3)["text"] == "New text"


async def test_jobs_survive_restart_and_retries_are_deferred(tmp_path, cfg):
    path = tmp_path / "journal.sqlite3"
    db = Store(path)
    db.upsert(await to_record(message(1, kind="voice"), cfg))
    db.close()
    db = Store(path)
    assert db.next_job(20)["message_id"] == 1
    db.result(20, 1, state="retry", error="NetworkError", retry_seconds=60)
    assert db.next_job(20) is None
    db.retry_errors(20)
    assert db.next_job(20)["message_id"] == 1
    db.close()


@pytest.mark.parametrize("kind", ["voice", "video_note", "video"])
async def test_media_kinds(kind, cfg):
    r = await to_record(message(1, kind=kind), cfg)
    assert r["kind"] == kind
    assert r["transcription_state"] == "pending"
    assert r["duration_seconds"] > 0


async def test_two_chats_same_message_id_cannot_collide(store, cfg):
    r = await to_record(message(1), cfg)
    store.upsert(r)
    store.upsert(dict(r, chat_id=30, text="Other chat"))
    assert store.get(20, 1)["text"] == "Hello"
    assert store.get(30, 1)["text"] == "Other chat"


def test_invalid_timezone_rejected(cfg):
    cfg.timezone = "Unknown/Timezone"
    with pytest.raises(KeyError):
        cfg.validate()

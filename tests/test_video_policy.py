import sys

import pytest
from conftest import message
from telethon.tl.types import MessageMediaWebPage, WebPage

from tgstudy import cli
from tgstudy.capture import to_record
from tgstudy.config import Config


@pytest.mark.parametrize("enabled", [True, False])
async def test_youtube_preview_is_never_a_transcription_job(cfg, enabled):
    m = message(1, kind="video", text="https://www.youtube.com/watch?v=test")
    m.media = MessageMediaWebPage(
        WebPage(
            id=1, url=m.message, display_url="youtube.com", hash=0, document=m.document
        )
    )
    assert m.video is not None  # This is the actual Telethon preview trap.
    cfg.transcribe_videos = enabled
    record = await to_record(m, cfg)
    assert record["kind"] == "link"
    assert record["media_key"] is None
    assert record["transcription_state"] == "not_applicable"
    assert record["text"] == m.message


async def test_default_ignores_video_but_keeps_voice_and_video_notes(cfg):
    assert cfg.transcribe_videos is False
    for kind in ["voice", "video_note", "video"]:
        row = await to_record(message(1, kind=kind), cfg)
        assert row["transcription_state"] == (
            "not_applicable" if kind == "video" else "pending"
        )


async def test_videos_off_clears_old_video_asr_but_retains_text_and_voice(
    cfg, store, tmp_path, monkeypatch
):
    cfg.transcribe_videos = True
    cfg.save(tmp_path)
    for mid, kind in [(1, "video"), (2, "voice")]:
        store.upsert(
            await to_record(message(mid, kind=kind, text="original caption"), cfg)
        )
        store.result(20, mid, state="done", transcript="old transcript", language="en")
    monkeypatch.setenv("TGSTUDY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["tgstudy", "videos", "off"])
    cli.main()
    assert Config.load(tmp_path).transcribe_videos is False
    video = store.get(20, 1)
    assert (
        video["transcription_state"] == "not_applicable" and video["transcript"] is None
    )
    assert video["text"] == "original caption"
    assert store.get(20, 2)["transcript"] == "old transcript"

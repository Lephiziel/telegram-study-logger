import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon.tl.types import Channel, MessageFwdHeader

from conftest import message
from tgstudy import cli
from tgstudy.capture import to_record
from tgstudy.config import Chat, Config
from tgstudy.exporter import export_dirty
from tgstudy.worker import MultiWorker, sync_now

CHANNEL = -1000000000030


def two_chats(cfg):
    cfg.history_since = None
    cfg.export_mode = "daily"
    cfg.extra_chats = [Chat(CHANNEL, "English practice", own_posts=True)]
    return cfg


class Client:
    def __init__(self, own_id=10, unavailable=None):
        self.own_id = own_id
        self.unavailable = unavailable
        self.downloads = []
        self.handlers = []
        self.disconnected = False
        self.release = asyncio.Event()

    async def connect(self):
        pass

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return SimpleNamespace(id=self.own_id)

    async def get_input_entity(self, chat):
        if chat == self.unavailable:
            raise ValueError("not available")
        return chat

    async def iter_messages(self, peer, **kwargs):
        if peer == self.unavailable:
            raise ValueError("not available")
        for mid in range(1, 4 if peer == CHANNEL else 3):
            if mid > kwargs.get("min_id", 0):
                yield message(
                    mid, kind="voice" if mid == 1 else "text", text=f"chat {peer}"
                )

    async def download_media(self, msg, file):
        self.downloads.append(file)
        Path(file).write_bytes(b"audio")
        return file

    def add_event_handler(self, handler, builder):
        self.handlers.append(handler)

    async def run_until_disconnected(self):
        await self.release.wait()

    async def disconnect(self):
        self.disconnected = True


class Recognizer:
    def run(self, path):
        return {
            "text": f"audio from {path.parent.name}",
            "language": "en",
            "segments": [],
        }


def test_legacy_config_and_scopes_round_trip(cfg, tmp_path):
    cfg.save(tmp_path)
    path = tmp_path / "config.json"
    values = json.loads(path.read_text(encoding="utf-8"))
    values.pop("extra_chats")
    values.pop("own_posts")
    path.write_text(json.dumps(values), encoding="utf-8")
    assert Config.load(tmp_path).extra_chats == []
    two_chats(cfg).save(tmp_path)
    loaded = Config.load(tmp_path)
    scopes = loaded.chat_configs()
    assert [c.chat_id for c in scopes] == [20, CHANNEL]
    assert scopes[1].own_posts and not scopes[0].own_posts
    scopes[1].history_since = "2026-10-01T00:00:00+00:00"
    assert loaded.history_since is None and loaded.extra_chats[0].history_since is None
    assert loaded.select_chat(CHANNEL).model == cfg.model


def test_duplicate_chat_rejected(cfg):
    cfg.extra_chats = [Chat(cfg.chat_id, "Duplicate")]
    with pytest.raises(ValueError):
        cfg.validate()


async def test_channel_authorship_is_explicit_and_forwarded_posts_are_not_own(cfg):
    post = message(1, sender_id=30)
    assert not (await to_record(post, cfg))["outgoing"]
    cfg.own_posts = True
    assert (await to_record(post, cfg))["outgoing"]
    post.fwd_from = MessageFwdHeader(date=post.date)
    record = await to_record(post, cfg)
    assert record["forwarded"] and not record["outgoing"]


async def test_multi_sync_has_independent_cursors_and_exports(
    cfg, tmp_path, monkeypatch
):
    import tgstudy.worker as module

    client = Client()
    monkeypatch.setattr(module, "telegram_client", lambda *args: client)
    two_chats(cfg)
    cfg.save(tmp_path)
    assert await sync_now(tmp_path, cfg) == 5
    # Reload configuration like a real reboot, then repeat without duplicates.
    assert await sync_now(tmp_path, Config.load(tmp_path)) == 0
    from tgstudy.store import Store

    db = Store(tmp_path / "journal.sqlite3")
    try:
        assert db.cursor(20) == 2 and db.cursor(CHANNEL) == 3
        assert db.get(20, 1)["text"] == "chat 20"
        assert db.get(CHANNEL, 1)["text"] == f"chat {CHANNEL}"
        for scoped in cfg.chat_configs():
            export_dirty(db, scoped, now=datetime(2026, 10, 4, 21, tzinfo=timezone.utc))
            path = Path(cfg.output_dir) / str(scoped.chat_id) / "today.jsonl"
            rows = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            assert {row["chat_id"] for row in rows} == {scoped.chat_id}
    finally:
        db.close()
    assert client.disconnected


async def test_live_dispatch_ignores_unselected_chat_and_asr_ids_do_not_collide(
    cfg, tmp_path, store
):
    group = MultiWorker(Client(), two_chats(cfg), tmp_path, store, Recognizer())
    for chat_id in [20, CHANNEL, 999]:
        await group.on_message(
            SimpleNamespace(chat_id=chat_id, message=message(1, kind="voice"))
        )
    assert store.get(999, 1) is None
    assert all(store.get(chat, 1) is not None for chat in [20, CHANNEL])
    for worker in group.workers.values():
        await worker.process_job(store.next_job(worker.cfg.chat_id))
    assert store.get(20, 1)["transcript"] == "audio from 20"
    assert store.get(CHANNEL, 1)["transcript"] == f"audio from {CHANNEL}"
    assert len(group.client.downloads) == 2


@pytest.mark.parametrize("unavailable", [None, CHANNEL])
async def test_background_multi_worker_stops_and_unavailable_channel_does_not_block(
    cfg, tmp_path, store, unavailable
):
    client = Client(unavailable=unavailable)
    group = MultiWorker(client, two_chats(cfg), tmp_path, store, Recognizer())
    task = asyncio.create_task(group.run(tmp_path / "stop"))
    try:
        for _ in range(100):
            if store.cursor(20) == 2 and (
                group.workers[CHANNEL].history_error
                if unavailable
                else store.cursor(CHANNEL) == 3
            ):
                break
            await asyncio.sleep(0.01)
        assert store.cursor(20) == 2
        assert group.workers[CHANNEL].history_error == (
            "ValueError" if unavailable else None
        )
        if not unavailable:
            assert store.cursor(CHANNEL) == 3
        group.export_status("working")
        status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
        assert len(status["chats"]) == 2
    finally:
        group.stop.set()
        await asyncio.wait_for(task, 5)
    assert client.disconnected and len(client.handlers) == 2
    assert (
        json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))["state"]
        == "paused"
    )


async def test_caption_edit_in_channel_does_not_touch_matching_id_in_other_chat(
    cfg, tmp_path, store
):
    group = MultiWorker(Client(), two_chats(cfg), tmp_path, store, Recognizer())
    for chat_id in [20, CHANNEL]:
        await group.on_message(
            SimpleNamespace(chat_id=chat_id, message=message(1, kind="video_note"))
        )
        worker = group.workers[chat_id]
        await worker.process_job(store.next_job(chat_id))
    edit = message(
        1,
        kind="video_note",
        text="Changed channel caption",
        edited=datetime(2026, 10, 5, tzinfo=timezone.utc),
    )
    await group.on_message(SimpleNamespace(chat_id=CHANNEL, message=edit))
    assert store.get(CHANNEL, 1)["text"] == "Changed channel caption"
    assert store.get(CHANNEL, 1)["transcript"] == f"audio from {CHANNEL}"
    assert store.get(20, 1)["text"] == "Hello"


async def test_add_chat_uses_existing_session_and_preserves_primary(
    cfg, tmp_path, monkeypatch
):
    client = Client()
    selected = SimpleNamespace(
        entity=Channel(
            id=30, title="English practice", photo=None, date=None, broadcast=True
        ),
        name="English practice",
    )

    async def select(_):
        return selected

    monkeypatch.setattr(cli, "choose_chat", select)
    monkeypatch.setattr(cli, "telegram_client", lambda *args: client)
    cfg.save(tmp_path)
    await cli.add_chat(tmp_path, cfg, days=7, own_posts=True)
    loaded = Config.load(tmp_path)
    assert loaded.chat_id == 20 and loaded.chat_title == "Test Chat"
    assert loaded.extra_chats[0].chat_id == CHANNEL
    assert loaded.extra_chats[0].own_posts
    assert loaded.extra_chats[0].history_since is not None
    await cli.add_chat(tmp_path, loaded, days=7, own_posts=True)
    assert len(Config.load(tmp_path).extra_chats) == 1
    assert client.disconnected


def test_remove_primary_promotes_channel_without_deleting_archive(cfg, tmp_path, store):
    two_chats(cfg).save(tmp_path)
    export_dirty(store, cfg)
    cli.remove_chat(tmp_path, cfg, 20)
    loaded = Config.load(tmp_path)
    assert loaded.chat_id == CHANNEL and loaded.own_posts
    assert (Path(cfg.output_dir) / "20/chat.json").exists()
    with pytest.raises(ValueError):
        cli.remove_chat(tmp_path, loaded, CHANNEL)


async def test_retranscribe_requires_chat_id_when_message_numbers_overlap(
    cfg, tmp_path, store, monkeypatch
):
    two_chats(cfg).save(tmp_path)
    for scoped in cfg.chat_configs():
        store.upsert(await to_record(message(1, kind="voice"), scoped))
        store.result(
            scoped.chat_id, 1, state="done", transcript="Keep me", language="en"
        )
    monkeypatch.setenv("TGSTUDY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["tgstudy", "retranscribe", "--message-id", "1"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    monkeypatch.setattr(
        sys,
        "argv",
        ["tgstudy", "retranscribe", "--message-id", "1", "--chat-id", str(CHANNEL)],
    )
    cli.main()
    assert store.get(20, 1)["transcript"] == "Keep me"
    assert store.get(CHANNEL, 1)["transcription_state"] == "pending"


def test_mode_updates_all_chat_floors_and_speech_keeps_chat_list(
    cfg, tmp_path, monkeypatch
):
    from tgstudy.periods import set_period

    two_chats(cfg)
    set_period(cfg, "history", 7)
    assert cfg.history_since == cfg.extra_chats[0].history_since
    cfg.save(tmp_path)
    monkeypatch.setenv("TGSTUDY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        sys, "argv", ["tgstudy", "speech", "auto", "--languages", "en,ru"]
    )
    cli.main()
    assert Config.load(tmp_path).extra_chats == cfg.extra_chats

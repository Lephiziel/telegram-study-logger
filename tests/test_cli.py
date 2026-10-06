import sys
from pathlib import Path

from conftest import message

from tgstudy import cli
from tgstudy.capture import to_record
from tgstudy.config import Config
from tgstudy.store import Store
from tgstudy.worker import sync_now


def test_speech_auto_upgrades_existing_english_setting(cfg, tmp_path, monkeypatch):
    cfg.language = "en"
    cfg.model = "small.en"
    cfg.multilingual = False
    cfg.save(tmp_path)
    monkeypatch.setenv("TGSTUDY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["tgstudy", "speech", "auto"])
    cli.main()
    saved = Config.load(tmp_path)
    assert saved.language is None and saved.multilingual is True
    assert saved.model == "small"
    assert saved.chat_id == cfg.chat_id


async def test_sync_command_uses_stored_session_and_exports_without_watcher(
    cfg, tmp_path, monkeypatch
):
    import tgstudy.worker as worker_module

    class Client:
        disconnected = False

        async def connect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            from types import SimpleNamespace

            return SimpleNamespace(id=cfg.own_id)

        async def get_input_entity(self, chat):
            return chat

        async def iter_messages(self, peer, **kwargs):
            yield message(1)
            yield message(2, kind="voice")

        async def disconnect(self):
            self.disconnected = True

    cfg.history_since = None
    client = Client()
    monkeypatch.setattr(worker_module, "telegram_client", lambda *args: client)
    assert await sync_now(tmp_path, cfg) == 2
    assert client.disconnected
    db = Store(tmp_path / "journal.sqlite3")
    assert db.cursor(cfg.chat_id) == 2
    assert db.get(cfg.chat_id, 2)["transcription_state"] == "pending"
    db.close()
    assert (Path(cfg.output_dir) / "20/2026-10-05.md").exists()


async def test_cli_retranscribe_queues_completed_voice(cfg, tmp_path, monkeypatch):
    cfg.save(tmp_path)
    db = Store(tmp_path / "journal.sqlite3")
    db.upsert(await to_record(message(1, kind="voice"), cfg))
    db.result(cfg.chat_id, 1, state="done", transcript="Old result", language="en")
    db.close()
    monkeypatch.setenv("TGSTUDY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["tgstudy", "retranscribe", "--days", "0"])
    cli.main()
    db = Store(tmp_path / "journal.sqlite3")
    assert db.get(cfg.chat_id, 1)["transcription_state"] == "pending"
    assert db.get(cfg.chat_id, 1)["transcript"] is None
    db.close()


async def test_selective_language_repair_keeps_good_transcripts(
    cfg, store, tmp_path, monkeypatch
):
    cfg.save(tmp_path)
    for mid in [1, 2, 3]:
        store.upsert(await to_record(message(mid, kind="voice"), cfg))
    store.result(20, 1, state="done", transcript="Hello", language="en")
    store.result(20, 2, state="done", transcript="Foreign", language="pt")
    store.result(
        20,
        3,
        state="done",
        transcript="Mixed",
        language="en",
        segments=[{"language": "ro"}],
    )
    monkeypatch.setenv("TGSTUDY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        sys, "argv", ["tgstudy", "retranscribe", "--unexpected-languages"]
    )
    cli.main()
    assert store.get(20, 1)["transcript"] == "Hello"
    assert [store.get(20, i)["transcription_state"] for i in [2, 3]] == [
        "pending",
        "pending",
    ]


async def test_retranscribe_one_message_does_not_clear_other_results(
    cfg, store, tmp_path, monkeypatch
):
    cfg.save(tmp_path)
    for mid in [1, 2]:
        store.upsert(await to_record(message(mid, kind="voice"), cfg))
        store.result(20, mid, state="done", transcript="Hello", language="en")
    monkeypatch.setenv("TGSTUDY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["tgstudy", "retranscribe", "--message-id", "2"])
    cli.main()
    assert store.get(20, 1)["transcript"] == "Hello"
    assert store.get(20, 2)["transcription_state"] == "pending"

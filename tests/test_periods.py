import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import message

from tgstudy.capture import to_record
from tgstudy.config import Config
from tgstudy.control import lock, maintenance, maintenance_running
from tgstudy.exporter import export_dirty
from tgstudy.history import export_history
from tgstudy.periods import day_window, set_period
from tgstudy.worker import Worker

NOW = datetime(2026, 10, 5, 1, 55, tzinfo=timezone.utc)


def local_midnight(day):
    return datetime.fromisoformat(f"{day}T00:00:00+05:00")


def test_calendar_days_include_whole_today_not_last_24_hours():
    first, today, start, end = day_window("Asia/Yekaterinburg", 7, NOW)
    assert str(first) == "2026-09-29"
    assert str(today) == "2026-10-05"
    assert start == local_midnight("2026-09-29").astimezone(timezone.utc)
    assert end == local_midnight("2026-10-06").astimezone(timezone.utc)


@pytest.mark.parametrize("date,hours", [("2026-03-08", 23), ("2026-11-01", 25)])
def test_dst_day_boundaries(date, hours):
    now = datetime.fromisoformat(date + "T17:00:00+00:00")
    _, _, start, end = day_window("America/New_York", 1, now)
    assert (end - start).total_seconds() == hours * 3600


def test_modes_persist_initial_floor_for_offline_backfill(cfg, tmp_path):
    set_period(cfg, "daily", now=NOW)
    cfg.save(tmp_path)
    loaded = Config.load(tmp_path)
    assert loaded.export_mode == "daily"
    assert datetime.fromisoformat(loaded.history_since) == local_midnight("2026-10-05")
    # A restart tomorrow keeps the configured initial floor, not tomorrow's midnight.
    worker = Worker(None, loaded, tmp_path, None)
    assert worker.since == datetime.fromisoformat(cfg.history_since)
    set_period(cfg, "history", 7, now=NOW)
    assert cfg.history_days == 7
    assert datetime.fromisoformat(cfg.history_since) == local_midnight("2026-09-29")


def test_old_config_loads_with_defaults(cfg, tmp_path):
    old = asdict(cfg)
    old.pop("export_mode")
    old.pop("history_days")
    (tmp_path / "config.json").write_text(json.dumps(old), encoding="utf-8")
    assert Config.load(tmp_path).export_mode == "history"


@pytest.mark.parametrize("days", [-1, 2.5, True])
def test_invalid_periods_rejected(days):
    with pytest.raises(ValueError):
        day_window("UTC", days, NOW)


async def test_daily_midnight_keeps_yesterday_and_switches_today(store, cfg):
    set_period(cfg, "daily", now=NOW)
    old = message(1, kind="voice", date=local_midnight("2026-10-05"))
    store.upsert(await to_record(old, cfg))
    export_dirty(store, cfg, now=NOW)
    root = Path(cfg.output_dir) / "20"
    before = (root / "2026-10-05.md").read_text(encoding="utf-8")
    assert "#1" in (root / "today.md").read_text(encoding="utf-8")
    tomorrow = local_midnight("2026-10-06")
    export_dirty(store, cfg, now=tomorrow)
    assert (root / "2026-10-05.md").read_text(encoding="utf-8") == before
    assert (root / "2026-10-06.md").exists()
    assert (root / "today.jsonl").read_text(encoding="utf-8") == ""
    # A late transcript is written into yesterday's archive, not today's view.
    store.result(20, 1, state="done", transcript="Yesterday's speech", language="en")
    export_dirty(store, cfg, now=tomorrow)
    assert "Yesterday's speech" in (root / "2026-10-05.md").read_text(encoding="utf-8")
    assert "Yesterday's speech" not in (root / "today.md").read_text(encoding="utf-8")


async def test_history_view_excludes_old_records_and_rolls_tomorrow(store, cfg):
    set_period(cfg, "history", 7, now=NOW)
    for mid, date in [(1, "2026-09-28"), (2, "2026-09-29"), (3, "2026-10-05")]:
        store.upsert(await to_record(message(mid, date=local_midnight(date)), cfg))
    export_dirty(store, cfg, now=NOW)
    path = Path(cfg.output_dir) / "20/history_7_days.jsonl"
    ids = [json.loads(line)["message_id"] for line in path.read_text(encoding="utf-8").splitlines()]
    assert ids == [2, 3]
    export_dirty(store, cfg, now=NOW + timedelta(days=1))
    assert [
        json.loads(line)["message_id"] for line in path.read_text(encoding="utf-8").splitlines()
    ] == [3]
    assert (path.parent / "2026-09-29.md").exists()


def test_maintenance_lock_releases_even_on_exception(tmp_path):
    assert not maintenance_running(tmp_path)
    with pytest.raises(RuntimeError):
        with maintenance(tmp_path):
            assert maintenance_running(tmp_path)
            assert (tmp_path / "worker-stop").exists()
            raise RuntimeError("test")
    assert not maintenance_running(tmp_path)
    with lock(tmp_path, "worker"):
        pass


async def test_today_catchup_includes_exact_midnight(store, cfg, tmp_path):
    set_period(cfg, "daily", now=NOW)

    class Client:
        kwargs = None

        async def iter_messages(self, peer, **kwargs):
            self.kwargs = kwargs
            yield message(1, date=local_midnight("2026-10-04"))
            yield message(2, date=local_midnight("2026-10-05"))

    client = Client()
    worker = Worker(client, cfg, tmp_path, store)
    await worker.sync_history()
    assert store.get(20, 1) is None
    assert store.get(20, 2) is not None
    assert client.kwargs["offset_date"] == datetime.fromisoformat(
        cfg.history_since
    ) - timedelta(seconds=1)


async def test_one_off_export_fetches_older_days_without_changing_daily_mode(
    cfg, tmp_path
):
    set_period(cfg, "daily", now=NOW)
    saved_since = cfg.history_since
    messages = [
        message(1, date=local_midnight("2026-09-28")),
        message(2, kind="voice", date=local_midnight("2026-09-29")),
        message(3, date=local_midnight("2026-10-05")),
        message(4, date=local_midnight("2026-10-06")),
    ]

    class Client:
        disconnected = False

        async def connect(self):
            pass

        async def disconnect(self):
            self.disconnected = True

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return SimpleNamespace(id=10)

        async def get_input_entity(self, chat):
            return chat

        async def iter_messages(self, peer, **kwargs):
            for m in messages:
                yield m

        async def get_messages(self, peer, ids):
            return next(m for m in messages if m.id == ids)

        async def download_media(self, m, file):
            Path(file).write_bytes(b"test")
            return file

    class Recognizer:
        def run(self, path):
            return {"text": "Historical voice", "language": "en", "segments": []}

    client = Client()
    path, count, errors = await export_history(
        tmp_path, cfg, 7, now=NOW, client=client, transcriber=Recognizer()
    )
    assert count == 2 and errors == 0
    assert "Historical voice" in path.read_text(encoding="utf-8")
    assert cfg.export_mode == "daily" and cfg.history_since == saved_since
    assert client.disconnected
    from tgstudy.store import Store

    db = Store(tmp_path / "journal.sqlite3")
    assert db.cursor(20) == 0
    assert db.get(20, 4) is None
    db.close()

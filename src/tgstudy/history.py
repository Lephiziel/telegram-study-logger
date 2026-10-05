from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .exporter import export_period
from .periods import day_window
from .store import Store
from .worker import Worker, telegram_client


async def export_history(
    root: Path, cfg: Config, days: int, *, now=None, client=None, transcriber=None
):
    now = now or datetime.now(timezone.utc)
    first, today, start, end = day_window(cfg.timezone, days, now)
    scoped = replace(
        cfg,
        history_since=start.isoformat() if start else None,
        export_mode="history",
        history_days=days,
    )
    client = client or telegram_client(scoped, root)
    store = Store(root / "journal.sqlite3")
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("AuthorizationRequiredRunSetup")
        me = await client.get_me()
        if me.id != cfg.own_id:
            raise RuntimeError("SessionAccountMismatchRunSetup")
        worker = Worker(client, scoped, root, store, transcriber)
        worker.peer = await client.get_input_entity(cfg.chat_id)
        kwargs = {"reverse": True, "wait_time": 1}
        if start:
            kwargs["offset_date"] = start - timedelta(seconds=1)
        async for message in client.iter_messages(worker.peer, **kwargs):
            if message.date and message.date >= end:
                break
            await worker.ingest(message)
        # Do not change the continuous journal's history checkpoint.
        destination = (
            Path(cfg.output_dir)
            / str(cfg.chat_id)
            / "reports"
            / f"history_{first or 'all'}_{today}"
        )
        export_period(store, cfg, days, now=now, destination=destination)
        while True:
            jobs = [
                row
                for row in store.rows_between(cfg.chat_id, start, end)
                if row["transcription_state"] in {"pending", "retry"}
            ]
            if not jobs:
                break
            job = next((row for row in jobs if row["retry_at"] <= time.time()), None)
            if job:
                await worker.process_job(job)
                export_period(store, cfg, days, now=now, destination=destination)
            else:
                await asyncio.sleep(
                    max(
                        0.2, min(30, min(row["retry_at"] for row in jobs) - time.time())
                    )
                )
        path, count = export_period(store, cfg, days, now=now, destination=destination)
        errors = sum(
            row["transcription_state"] == "error"
            for row in store.rows_between(cfg.chat_id, start, end)
        )
        return path, count, errors
    finally:
        await client.disconnect()
        store.close()

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

from .capture import media_key, to_record
from .config import Config, status_write
from .exporter import export_dirty
from .store import Store
from .transcribe import Transcriber

log = logging.getLogger(__name__)


def telegram_client(cfg: Config, root: Path):
    return TelegramClient(
        str(root / "account"),
        cfg.api_id,
        cfg.api_hash,
        device_model="Telegram Study Logger",
        app_version="0.3.0",
        flood_sleep_threshold=60,
        catch_up=True,
    )


class Worker:
    def __init__(self, client, cfg: Config, root: Path, store: Store, transcriber=None):
        self.client = client
        self.cfg = cfg
        self.root = root
        self.store = store
        self.peer = None
        self.stop = asyncio.Event()
        self.transcriber = transcriber or Transcriber(cfg, root)
        self.last_sync = None
        self.current_job = None
        self.history_error = None
        self.last_sync_count = 0
        self.since = (
            datetime.fromisoformat(cfg.history_since) if cfg.history_since else None
        )

    async def ingest(self, message):
        if self.since and message.date and message.date < self.since:
            return
        self.store.upsert(await to_record(message, self.cfg))

    async def sync_history(self):
        cursor = self.store.cursor(self.cfg.chat_id)
        kwargs = {"reverse": True, "min_id": cursor, "wait_time": 1}
        if not cursor and self.since:
            # Telegram's date offset is exclusive and has second precision.
            kwargs["offset_date"] = self.since - timedelta(seconds=1)
        count = 0
        async for message in self.client.iter_messages(self.peer, **kwargs):
            if self.stop.is_set():
                return
            await self.ingest(message)
            self.store.advance(self.cfg.chat_id, message.id)
            count += 1
            if count % 100 == 0:
                # Yield so recognition/events can run during a large initial import.
                await asyncio.sleep(0)
        self.last_sync = time.time()
        self.last_sync_count = count

    async def sync_once(self):
        # Show the newest messages first, even if the initial import is long.
        # Refresh never advances the ordered history checkpoint.
        await self.refresh_recent()
        await self.sync_history()

    async def refresh_recent(self):
        # Recover caption/text edits made while the module was offline.
        async for message in self.client.iter_messages(
            self.peer, limit=200, wait_time=1
        ):
            if self.stop.is_set():
                return
            await self.ingest(message)

    async def wait(self, seconds: float):
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def history_loop(self):
        while not self.stop.is_set():
            delay = self.cfg.poll_seconds
            try:
                await self.sync_once()
                self.history_error = None
            except FloodWaitError as exc:
                delay = max(delay, exc.seconds + 1)
                self.history_error = "TelegramFloodWait"
            except Exception as exc:
                # Message content and credentials must not appear in logs.
                self.history_error = type(exc).__name__
                log.warning("History sync failed: %s", self.history_error)
            await self.wait(delay)

    def finish(self, job: dict, **kwargs):
        self.store.result(
            self.cfg.chat_id,
            job["message_id"],
            expected_media_key=job["media_key"],
            **kwargs,
        )

    async def process_job(self, job: dict):
        mid = job["message_id"]
        if job["media_bytes"] > self.cfg.max_media_mb * 1024 * 1024:
            self.finish(job, state="error", error="media_exceeds_size_limit")
            return
        self.current_job = mid
        try:
            message = await self.client.get_messages(self.peer, ids=mid)
            if not message:
                self.finish(job, state="error", error="message_no_longer_available")
                return
            if media_key(message) != job["media_key"]:
                # Existing jobs can predate the daily capture floor after a one-off export.
                self.store.upsert(await to_record(message, self.cfg))
                return
            if getattr(message, "noforwards", False):
                self.finish(job, state="error", error="protected_content")
                return
            media_root = self.root / "temporary-media"
            media_root.mkdir(exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(dir=media_root) as tmp:
                downloaded = await asyncio.wait_for(
                    self.client.download_media(
                        message, file=str(Path(tmp) / "recording")
                    ),
                    timeout=600,
                )
                if not downloaded:
                    raise RuntimeError("MissingMedia")
                path = Path(downloaded)
                if path.stat().st_size > self.cfg.max_media_mb * 1024 * 1024:
                    self.finish(job, state="error", error="media_exceeds_size_limit")
                    return
                if self.stop.is_set():
                    return
                result = await asyncio.to_thread(self.transcriber.run, path)
                self.finish(
                    job,
                    state="done" if result["text"] else "no_speech",
                    transcript=result["text"],
                    language=result["language"],
                    segments=result["segments"],
                )
        except FloodWaitError as exc:
            self.finish(
                job,
                state="retry",
                error="TelegramFloodWait",
                retry_seconds=exc.seconds + 1,
            )
        except Exception as exc:
            err = type(exc).__name__
            final = job["attempts"] >= 5
            self.finish(
                job,
                state="error" if final else "retry",
                error=err,
                retry_seconds=min(3600, 30 * 2 ** min(job["attempts"], 7)),
            )
            log.warning("Transcription failed for message %d: %s", mid, err)
        finally:
            self.current_job = None

    async def transcription_loop(self):
        while not self.stop.is_set():
            job = self.store.next_job(self.cfg.chat_id)
            if job:
                await self.process_job(job)
            else:
                await self.wait(2)

    async def export_loop(self):
        while not self.stop.is_set():
            export_dirty(self.store, self.cfg)
            status_write(
                self.root,
                state="working",
                chat_id=self.cfg.chat_id,
                export_mode=self.cfg.export_mode,
                history_days=self.cfg.history_days,
                last_sync=self.last_sync,
                last_sync_count=self.last_sync_count,
                history_cursor=self.store.cursor(self.cfg.chat_id),
                current_job=self.current_job,
                history_error=self.history_error,
                counts=self.store.counts(self.cfg.chat_id),
            )
            await self.wait(3)

    async def stop_monitor(self, stop_file: Path):
        while not self.stop.is_set():
            if stop_file.exists():
                self.stop.set()
                break
            await self.wait(1)

    async def connect(self):
        await self.client.connect()
        if not await self.client.is_user_authorized():
            status_write(self.root, state="authorization_required")
            raise RuntimeError("AuthorizationRequiredRunSetup")
        me = await self.client.get_me()
        if me.id != self.cfg.own_id:
            raise RuntimeError("SessionAccountMismatchRunSetup")
        self.peer = await self.client.get_input_entity(self.cfg.chat_id)

    async def run(self, stop_file: Path):
        await self.connect()

        async def on_message(event):
            if event.chat_id == self.cfg.chat_id and not self.stop.is_set():
                try:
                    await self.ingest(event.message)
                except Exception as exc:
                    log.warning("Message update failed: %s", type(exc).__name__)

        self.client.add_event_handler(on_message, events.NewMessage(chats=self.peer))
        self.client.add_event_handler(on_message, events.MessageEdited(chats=self.peer))
        tasks = [
            asyncio.create_task(coro)
            for coro in (
                self.history_loop(),
                self.transcription_loop(),
                self.export_loop(),
                self.stop_monitor(stop_file),
                self.client.run_until_disconnected(),
            )
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            self.stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.client.disconnect()
            export_dirty(self.store, self.cfg)
            status_write(
                self.root, state="paused", counts=self.store.counts(self.cfg.chat_id)
            )


async def run_worker(root: Path, stop_file: Path):
    cfg = Config.load(root)
    store = Store(root / "journal.sqlite3")
    client = telegram_client(cfg, root)
    try:
        await Worker(client, cfg, root, store).run(stop_file)
    finally:
        await client.disconnect()
        store.close()


async def sync_now(root: Path, cfg: Config):
    """Explicit catch-up independent of watcher, desktop, or transcription load."""
    store = Store(root / "journal.sqlite3")
    client = telegram_client(cfg, root)
    try:
        worker = Worker(client, cfg, root, store)
        status_write(root, state="syncing", chat_id=cfg.chat_id)
        await worker.connect()
        await worker.sync_once()
        export_dirty(store, cfg)
        status_write(
            root,
            state="synced",
            chat_id=cfg.chat_id,
            last_sync=worker.last_sync,
            last_sync_count=worker.last_sync_count,
            history_cursor=store.cursor(cfg.chat_id),
            counts=store.counts(cfg.chat_id),
        )
        return worker.last_sync_count
    except Exception as exc:
        status_write(root, state="sync_error", error=type(exc).__name__)
        raise
    finally:
        await client.disconnect()
        store.close()

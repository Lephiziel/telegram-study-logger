import asyncio
import os
import time
from contextlib import suppress

from conftest import message
from test_worker import Client, Recognizer

from tgstudy.capture import to_record
from tgstudy.media_cache import MediaCache
from tgstudy.worker import Worker


async def drain_prefetch(worker):
    task = asyncio.create_task(worker.media_loop())
    try:
        await asyncio.wait_for(worker.media_queue.join(), timeout=5)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_prefetch_survives_message_deletion_and_worker_restart(
    store, cfg, tmp_path
):
    m = message(1, kind="voice")
    first = Worker(Client([m]), cfg, tmp_path, store, Recognizer())
    await first.ingest(m)
    await drain_prefetch(first)  # Download using the message seen during ingestion.
    path = first.cache.get(store.get(20, 1))
    assert path.read_bytes() == b"test media"
    second = Worker(Client([]), cfg, tmp_path, store, Recognizer())
    await second.process_job(store.next_job(20))
    assert store.get(20, 1)["transcription_state"] == "done"
    assert second.cache.get(store.get(20, 1)) == path
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


async def test_download_uses_observed_message_without_refetching_metadata(
    store, cfg, tmp_path
):
    class MetadataGone(Client):
        async def get_messages(self, *a, **kw):
            raise AssertionError("Do not fetch metadata we already observed")

    m = message(1, kind="voice")
    worker = Worker(MetadataGone([]), cfg, tmp_path, store, Recognizer())
    await worker.ingest(m)
    await worker.process_job(store.next_job(20))
    assert store.get(20, 1)["transcription_state"] == "done"


async def test_failed_asr_reuses_cached_media_even_without_telegram(
    store, cfg, tmp_path
):
    class Fails:
        def run(self, path):
            raise RuntimeError("bad inference")

    m = message(1, kind="voice")
    first = Worker(Client([m]), cfg, tmp_path, store, Fails())
    await first.ingest(m)
    await first.process_job(store.next_job(20))
    assert store.get(20, 1)["transcription_state"] == "retry"
    store.retry_errors(20)
    resumed = Worker(Client([]), cfg, tmp_path, store, Recognizer())
    await resumed.process_job(store.next_job(20))
    assert store.get(20, 1)["transcription_state"] == "done"


async def test_concurrent_prefetch_and_asr_download_only_once(store, cfg, tmp_path):
    class Counting(Client):
        downloads = 0

        async def download_media(self, m, file):
            self.downloads += 1
            await asyncio.sleep(0)
            return await super().download_media(m, file)

    m = message(1, kind="voice")
    client = Counting([m])
    worker = Worker(client, cfg, tmp_path, store, Recognizer())
    await worker.ingest(m)
    await asyncio.gather(drain_prefetch(worker), worker.process_job(store.next_job(20)))
    assert client.downloads == 1
    assert store.get(20, 1)["transcription_state"] == "done"


async def test_partial_download_never_becomes_cached_media(store, cfg, tmp_path):
    class Interrupted(Client):
        async def download_media(self, m, file):
            from pathlib import Path

            Path(file).write_bytes(b"partial")
            raise ConnectionError()

    m = message(1, kind="voice")
    worker = Worker(Interrupted([m]), cfg, tmp_path, store, Recognizer())
    await worker.ingest(m)
    await worker.process_job(store.next_job(20))
    assert worker.cache.get(store.get(20, 1)) is None
    assert list((tmp_path / "media-cache").rglob("*.part*")) == []
    assert store.get(20, 1)["transcription_state"] == "retry"


async def test_cache_identity_includes_chat_and_edited_media(cfg, tmp_path):
    cache = MediaCache(tmp_path, cfg)
    old = await to_record(message(1, kind="voice", doc_id=100), cfg)
    other = dict(old, chat_id=30)
    edited = await to_record(message(1, kind="voice", doc_id=200), cfg)
    path = cache.path(old)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"original")
    assert cache.get(old) == path
    assert cache.get(edited) is None and cache.get(other) is None


def test_cache_expiry_and_size_limit_protect_active_file(cfg, tmp_path):
    cfg.media_cache_max_mb = 1
    cache = MediaCache(tmp_path, cfg)
    paths = [
        cache.path({"chat_id": 20, "message_id": i, "media_key": i}) for i in range(3)
    ]
    paths[0].parent.mkdir(parents=True)
    for path in paths:
        path.write_bytes(b"x" * 600_000)
    os.utime(paths[0], (time.time() - 8 * 86400,) * 2)
    cache.prune(protected={paths[2]})
    assert not paths[0].exists() and not paths[1].exists() and paths[2].exists()

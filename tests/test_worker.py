from pathlib import Path

from conftest import message

from tgstudy.worker import Worker


class Client:
    def __init__(self, messages):
        self.messages = messages
        self.kwargs = None

    async def iter_messages(self, peer, **kwargs):
        self.kwargs = kwargs
        for m in self.messages:
            if m.id > kwargs.get("min_id", 0):
                yield m

    async def get_messages(self, peer, ids):
        return next((m for m in self.messages if m.id == ids), None)

    async def download_media(self, m, file):
        Path(file).write_bytes(b"test media")
        return file


class Recognizer:
    def run(self, path):
        assert path.read_bytes() == b"test media"
        return {"text": "Hello, how have you been?", "language": "en", "segments": []}


async def test_live_event_cannot_skip_unsynced_history(store, cfg, tmp_path):
    client = Client([message(1), message(2), message(3)])
    worker = Worker(client, cfg, tmp_path, store, Recognizer())
    await worker.ingest(message(100))  # A live event arrives before history.
    assert store.cursor(20) == 0
    await worker.sync_history()
    assert [store.get(20, i)["message_id"] for i in [1, 2, 3, 100]] == [1, 2, 3, 100]
    assert store.cursor(20) == 3
    assert client.kwargs["reverse"] is True


async def test_transcription_pipeline_cleans_temporary_media(store, cfg, tmp_path):
    m = message(1, kind="video_note")
    worker = Worker(Client([m]), cfg, tmp_path, store, Recognizer())
    await worker.ingest(m)
    await worker.process_job(store.next_job(20))
    r = store.get(20, 1)
    assert r["transcription_state"] == "done"
    assert r["transcript"] == "Hello, how have you been?"
    assert list((tmp_path / "temporary-media").iterdir()) == []


async def test_missing_message_is_explicit_error(store, cfg, tmp_path):
    worker = Worker(Client([]), cfg, tmp_path, store, Recognizer())
    await worker.ingest(message(1, kind="voice"))
    await worker.process_job(store.next_job(20))
    assert store.get(20, 1)["error"] == "message_no_longer_available"


async def test_size_limit_before_download(store, cfg, tmp_path):
    m = message(1, kind="voice")
    worker = Worker(Client([m]), cfg, tmp_path, store, Recognizer())
    await worker.ingest(m)
    job = store.next_job(20)
    job["media_bytes"] = 101 * 1024 * 1024
    await worker.process_job(job)
    assert store.get(20, 1)["error"] == "media_exceeds_size_limit"
    assert not (tmp_path / "temporary-media").exists()


async def test_failed_recognition_preserves_job_for_retry(store, cfg, tmp_path):
    class Broken:
        def run(self, path):
            raise RuntimeError("error might contain private data")

    m = message(1, kind="voice")
    worker = Worker(Client([m]), cfg, tmp_path, store, Broken())
    await worker.ingest(m)
    await worker.process_job(store.next_job(20))
    r = store.get(20, 1)
    assert r["transcription_state"] == "retry"
    assert r["error"] == "RuntimeError"
    assert "private" not in r["error"]

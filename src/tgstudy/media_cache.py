from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from .config import Config


class MediaUnavailable(RuntimeError):
    pass


class MessageUnavailable(MediaUnavailable):
    pass


class MediaTooLarge(RuntimeError):
    pass


class ProtectedContent(RuntimeError):
    pass


class MediaCache:
    """Private, atomically committed files keyed by chat, message AND media identity."""

    def __init__(self, root: Path, cfg: Config):
        self.root = root / "media-cache"
        self.cfg = cfg

    def path(self, job: dict) -> Path:
        identity = hashlib.sha256(str(job["media_key"]).encode()).hexdigest()[:24]
        return self.root / str(job["chat_id"]) / f"{job['message_id']}-{identity}.media"

    def get(self, job: dict) -> Path | None:
        path = self.path(job)
        if not path.exists():
            return None
        if not 0 < path.stat().st_size <= self.cfg.max_media_mb * 1024 * 1024:
            path.unlink(missing_ok=True)
            return None
        path.touch()  # Retention is measured from the last use.
        return path

    async def download(self, client, message, job: dict) -> Path:
        if job["media_bytes"] > self.cfg.max_media_mb * 1024 * 1024:
            raise MediaTooLarge()
        target = self.path(job)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        partial = target.with_suffix(".part")
        downloaded = None
        try:
            downloaded = await client.download_media(message, file=str(partial))
            if (
                not downloaded
                or not Path(downloaded).is_file()
                or not Path(downloaded).stat().st_size
            ):
                raise MediaUnavailable()
            source = Path(downloaded)
            if source.stat().st_size > self.cfg.max_media_mb * 1024 * 1024:
                raise MediaTooLarge()
            if os.name != "nt":
                os.chmod(source, 0o600)
            with source.open("r+b") as stream:
                os.fsync(stream.fileno())
            os.replace(source, target)
            return target
        finally:
            partial.unlink(missing_ok=True)
            if downloaded and Path(downloaded) != target:
                Path(downloaded).unlink(missing_ok=True)

    def prune(self, protected=(), *, remove_partials=False) -> None:
        if not self.root.exists():
            return
        protected = set(protected)
        if remove_partials:
            for path in self.root.glob("*/*.part*"):
                path.unlink(missing_ok=True)
        files = list(self.root.glob("*/*.media"))
        cutoff = time.time() - self.cfg.media_cache_days * 86400
        for path in files:
            if path not in protected and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        files = sorted(
            (p for p in files if p.exists()), key=lambda p: p.stat().st_mtime
        )
        size = sum(p.stat().st_size for p in files)
        for path in files:
            if size <= self.cfg.media_cache_max_mb * 1024 * 1024:
                break
            if path not in protected:
                size -= path.stat().st_size
                path.unlink(missing_ok=True)

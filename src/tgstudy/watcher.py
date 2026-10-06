from __future__ import annotations

import logging
import shutil
import signal
import subprocess
import time
from pathlib import Path

import psutil

from .config import Config, status_write
from .control import maintenance_running
from .startup import spawn

log = logging.getLogger(__name__)


def telegram_running(names: list[str], processes=None) -> bool:
    wanted = {name.casefold() for name in names}
    if processes is None:
        processes = psutil.process_iter(["name", "exe"])
    for process in processes:
        try:
            info = process.info
            candidates = {
                str(info.get("name") or "").casefold(),
                Path(info.get("exe") or "").name.casefold(),
            }
            if candidates & wanted:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def watch(root: Path):
    # The CLI holds the watcher lock before entering here. A stop request left
    # by the previous process must not disable a newly started watcher.
    exit_file = root / "watcher-stop"
    exit_file.unlink(missing_ok=True)
    cfg = Config.load(root)
    child = None
    absent_since = None
    next_start = 0.0
    stopping_since = None
    retries = 0
    stop_file = root / "worker-stop"
    stop_requested = False

    def signal_stop(*_):
        # OS shutdown stops this process, not the next login's process.
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, signal_stop)
    signal.signal(signal.SIGINT, signal_stop)
    try:
        while not stop_requested and not exit_file.exists():
            now = time.monotonic()
            paused = maintenance_running(root)
            if not paused:
                updated = Config.load(root)
                if updated != cfg:
                    cfg = updated
                    if child and stopping_since is None:
                        stop_file.touch()
                        stopping_since = now
            running = telegram_running(cfg.process_names)
            if child and child.poll() is not None:
                code = child.returncode
                child = None
                stopping_since = None
                if code:
                    retries += 1
                    next_start = now + min(300, 5 * 2 ** min(retries, 6))
                    status_write(
                        root,
                        state="worker_retry",
                        exit_code=code,
                        retry_in=round(next_start - now),
                    )
                else:
                    retries = 0
                    next_start = now + 3
            if running and not paused:
                absent_since = None
                if child is None and now >= next_start:
                    stop_file.unlink(missing_ok=True)
                    # Clean up a temporary media file left by a forced shutdown.
                    shutil.rmtree(root / "temporary-media", ignore_errors=True)
                    child = spawn("worker", "--stop-file", str(stop_file))
                    status_write(root, state="starting", pid=child.pid)
            else:
                absent_since = absent_since or now
                if (
                    child
                    and (paused or now - absent_since >= 15)
                    and stopping_since is None
                ):
                    stop_file.touch()
                    stopping_since = now
                if not child and now >= next_start:
                    status_write(
                        root, state="maintenance" if paused else "waiting_for_telegram"
                    )
                    next_start = now + 30
            if child and stopping_since is not None and now - stopping_since >= 45:
                child.kill()
                child.wait(timeout=10)
            time.sleep(3)
    finally:
        if child and child.poll() is None:
            stop_file.touch()
            try:
                child.wait(timeout=45)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)
        status_write(root, state="disabled")

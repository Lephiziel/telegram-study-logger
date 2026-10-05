from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import portalocker


def lock(root: Path, name: str, timeout: int = 0):
    return portalocker.Lock(
        str(root / f"{name}.lock"),
        timeout=timeout,
        check_interval=1,
        flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
    )


def maintenance_running(root: Path) -> bool:
    try:
        with lock(root, "maintenance"):
            return False
    except portalocker.exceptions.LockException:
        return True


@contextmanager
def maintenance(root: Path):
    # A lock rather than a persistent pause flag: crashes release it.
    with lock(root, "maintenance"):
        (root / "worker-stop").touch()
        with lock(root, "worker", timeout=70):
            yield

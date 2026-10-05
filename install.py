"""One-time installer. Runtime processes never show an application window."""

from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path


def main():
    if not (3, 11) <= sys.version_info[:2] < (3, 14):
        raise SystemExit("Для этой версии нужен Python 3.11–3.13 (рекомендуется 3.12).")
    source = Path(__file__).resolve().parent
    if os.name == "nt":
        target = Path(os.environ["LOCALAPPDATA"]) / "TelegramStudyLogger/runtime"
        exe = target / "Scripts/python.exe"
    else:
        target = Path.home() / ".local/share/telegram-study-logger/runtime"
        exe = target / "bin/python"
    print(f"Установка локального окружения: {target}", flush=True)
    if exe.exists():
        subprocess.run(
            [str(exe), "-m", "tgstudy", "uninstall"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    venv.EnvBuilder(with_pip=True).create(target)
    subprocess.run([str(exe), "-m", "pip", "install", "--upgrade", "pip"], check=True)
    # A regular install copies the package; no dependency on the extracted ZIP folder.
    subprocess.run(
        [str(exe), "-m", "pip", "install", "--upgrade", str(source)], check=True
    )
    subprocess.run([str(exe), "-m", "tgstudy", "settings"], check=True)


if __name__ == "__main__":
    main()

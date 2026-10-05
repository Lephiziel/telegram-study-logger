from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .config import atomic_text

LABEL = "local.telegram-study-logger"


def python_background() -> str:
    exe = Path(sys.executable)
    if os.name == "nt":
        hidden = exe.with_name("pythonw.exe")
        if hidden.exists():
            return str(hidden)
    return str(exe)


def command(mode: str, *args: str) -> list[str]:
    return [python_background(), "-m", "tgstudy", mode, *args]


def spawn(mode: str, *args: str):
    options = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        options["creationflags"] = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        options["start_new_session"] = True
    return subprocess.Popen(command(mode, *args), **options)


def startup_path(system: str | None = None) -> Path:
    system = system or sys.platform
    if system == "win32":
        return (
            Path(os.environ["APPDATA"])
            / "Microsoft/Windows/Start Menu/Programs/Startup/TelegramStudyLogger.vbs"
        )
    if system == "darwin":
        return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        / "autostart/telegram-study-logger.desktop"
    )


def desktop_quote(part: str) -> str:
    # freedesktop Exec quoting: preserve spaces and escape expansion characters.
    escaped = (
        part.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )
    # Backslashes also have to be escaped in the Desktop Entry string value.
    return '"' + escaped.replace("\\", "\\\\").replace("%", "%%") + '"'


def install_autostart(root: Path) -> Path:
    path = startup_path()
    cmd = command("watch")
    if sys.platform == "win32":
        line = subprocess.list2cmdline(cmd).replace('"', '""')
        content = f'Set sh = CreateObject("WScript.Shell")\nsh.Run "{line}", 0, False\n'
    elif sys.platform == "darwin":
        content = plistlib.dumps(
            {
                "Label": LABEL,
                "ProgramArguments": cmd,
                "RunAtLoad": True,
                "KeepAlive": False,
                "EnvironmentVariables": {"TGSTUDY_DATA_DIR": str(root)},
                "WorkingDirectory": str(root),
            },
            fmt=plistlib.FMT_XML,
        ).decode()
    else:
        # Explicit environment is needed if a custom data directory was used.
        content = (
            "[Desktop Entry]\nType=Application\nName=Telegram Study Logger\n"
            + "Comment=Starts logging only while Telegram is running\n"
            + "Exec="
            + " ".join(
                desktop_quote(p) for p in ["env", f"TGSTUDY_DATA_DIR={root}", *cmd]
            )
            + "\nTerminal=false\nX-GNOME-Autostart-enabled=true\n"
        )
    if sys.platform == "win32":
        # UTF-16 is understood by Windows Script Host, including non-ASCII usernames.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'Set env = CreateObject("WScript.Shell").Environment("PROCESS")\n'
            + 'env("TGSTUDY_DATA_DIR") = "'
            + str(root).replace('"', '""')
            + '"\n'
            + content,
            encoding="utf-16",
        )
    else:
        atomic_text(path, content)
    (root / "watcher-stop").unlink(missing_ok=True)
    spawn("watch")  # Activate now; no logout or Telegram restart needed.
    return path


def remove_autostart(root: Path):
    path = startup_path()
    path.unlink(missing_ok=True)
    (root / "watcher-stop").touch()
    (root / "worker-stop").touch()

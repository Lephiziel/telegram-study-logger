from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from .config import atomic_text

LABEL = "local.telegram-study-logger"
SERVICE = "telegram-study-logger.service"


def systemd_path() -> Path:
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        / "systemd/user"
        / SERVICE
    )


def systemctl(*args) -> bool:
    if not shutil.which("systemctl"):
        return False
    try:
        result = subprocess.run(
            ["systemctl", "--user", *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=70,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def systemd_quote(value: str, *, executable: bool = False) -> str:
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if executable:
        value = value.replace("$", "$$")
    return '"' + value + '"'


def install_systemd(root: Path, cmd: list[str]) -> Path | None:
    # Test availability before writing a unit on non-systemd desktop sessions.
    if not systemctl("show-environment"):
        return None
    path = systemd_path()
    atomic_text(
        path,
        "[Unit]\nDescription=Telegram Study Logger watcher\n\n[Service]\n"
        + "Type=simple\nExecStart="
        + " ".join(systemd_quote(p, executable=True) for p in cmd)
        + "\nEnvironment="
        + systemd_quote(f"TGSTUDY_DATA_DIR={root}")
        + "\nRestart=on-failure\nRestartSec=5\nTimeoutStopSec=60\n"
        + "\n[Install]\nWantedBy=default.target\n",
    )
    if systemctl("daemon-reload") and systemctl("enable", "--now", SERVICE):
        return path
    systemctl("disable", "--now", SERVICE)
    path.unlink(missing_ok=True)
    systemctl("daemon-reload")
    return None


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
    (root / "watcher-stop").unlink(missing_ok=True)
    if sys.platform not in {"win32", "darwin"}:
        service = install_systemd(root, cmd)
        if service:
            path.unlink(missing_ok=True)  # Migrate the old XDG startup entry.
            return service
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
    if sys.platform not in {"win32", "darwin"} and systemd_path().exists():
        systemctl("disable", "--now", SERVICE)
        systemd_path().unlink(missing_ok=True)
        systemctl("daemon-reload")

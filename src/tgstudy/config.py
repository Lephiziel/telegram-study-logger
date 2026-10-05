from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from platformdirs import user_data_path
from tzlocal import get_localzone_name


def default_timezone() -> str:
    """Use the OS timezone when it has an IANA name; otherwise offer UTC."""
    try:
        name = get_localzone_name()
        ZoneInfo(name)
        return name
    except (OSError, ValueError, KeyError, TypeError):
        return "UTC"


def data_dir() -> Path:
    override = os.environ.get("TGSTUDY_DATA_DIR")
    path = (
        Path(override).expanduser()
        if override
        else user_data_path("TelegramStudyLogger", appauthor=False)
    )
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path.resolve()


def atomic_text(path: Path, text: str) -> None:
    """Replace a file in place without leaving a partially written export."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as out:
            if os.name != "nt":
                os.chmod(tmp, 0o600)
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@dataclass
class Config:
    api_id: int
    api_hash: str
    chat_id: int
    chat_title: str
    own_id: int
    output_dir: str
    timezone: str = field(default_factory=default_timezone)
    history_since: str | None = None
    export_mode: str = "history"
    history_days: int = 30
    model: str = "small"
    language: str | None = None
    cpu_threads: int = 2
    transcribe_videos: bool = True
    max_media_mb: int = 100
    poll_seconds: int = 60
    process_names: list[str] = field(
        default_factory=lambda: [
            "Telegram",
            "Telegram.exe",
            "telegram-desktop",
            "Telegram Desktop",
        ]
    )

    def validate(self) -> None:
        if self.api_id <= 0 or len(self.api_hash) != 32:
            raise ValueError("Некорректные Telegram API credentials")
        if not self.chat_id or not self.own_id or not self.output_dir:
            raise ValueError("Нужно выбрать чат и папку экспорта")
        ZoneInfo(self.timezone)
        if self.export_mode not in {"history", "daily"}:
            raise ValueError("export_mode: history или daily")
        if (
            isinstance(self.history_days, bool)
            or not isinstance(self.history_days, int)
            or self.history_days < 0
        ):
            raise ValueError("history_days должен быть целым числом >= 0")
        if self.history_since:
            d = datetime.fromisoformat(self.history_since)
            if d.tzinfo is None:
                raise ValueError("history_since должен включать часовой пояс")
        if self.poll_seconds < 15 or self.max_media_mb <= 0 or self.cpu_threads <= 0:
            raise ValueError("Некорректные ограничения или интервал синхронизации")
        if not self.process_names:
            raise ValueError("Нужен хотя бы один process_names")

    @classmethod
    def load(cls, root: Path) -> Config:
        obj = cls(**json.loads((root / "config.json").read_text(encoding="utf-8")))
        obj.validate()
        return obj

    def save(self, root: Path) -> None:
        self.validate()
        atomic_text(
            root / "config.json",
            json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n",
        )


def status_write(root: Path, **values) -> None:
    values["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_text(
        root / "status.json", json.dumps(values, ensure_ascii=False, indent=2) + "\n"
    )

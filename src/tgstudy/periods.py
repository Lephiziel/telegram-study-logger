from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Config


def day_window(tz: str, days: int, now: datetime | None = None):
    """N local calendar dates including today, not N * 24 UTC hours. 0 = all."""
    if isinstance(days, bool) or not isinstance(days, int) or days < 0:
        raise ValueError("Количество дней должно быть целым числом >= 0")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now должен включать часовой пояс")
    zone = ZoneInfo(tz)
    today = now.astimezone(zone).date()
    start_day = today - timedelta(days=days - 1) if days else None
    start = (
        datetime.combine(start_day, time.min, zone).astimezone(timezone.utc)
        if start_day
        else None
    )
    end = datetime.combine(today + timedelta(days=1), time.min, zone).astimezone(
        timezone.utc
    )
    return start_day, today, start, end


def set_period(
    cfg: Config, mode: str, days: int | None = None, now: datetime | None = None
):
    if mode not in {"history", "daily"}:
        raise ValueError("Неизвестный режим")
    if days is not None:
        if mode != "history":
            raise ValueError("--days используется только с history")
        day_window(cfg.timezone, days, now)
        cfg.history_days = days
    cfg.export_mode = mode
    _, _, start, _ = day_window(
        cfg.timezone, 1 if mode == "daily" else cfg.history_days, now
    )
    # Persist this initial floor so an offline day is backfilled on the next start.
    cfg.history_since = start.isoformat() if start else None
    cfg.validate()


def keep_period(cfg: Config, mode: str, days: int | None = None):
    """Do not lose an offline backlog when an upgrade keeps the same mode."""
    if mode == cfg.export_mode and (days is None or days == cfg.history_days):
        cfg.validate()
        return
    set_period(cfg, mode, days)

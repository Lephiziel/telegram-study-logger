from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config, atomic_text
from .periods import day_window
from .store import Store


def exported_record(row: dict) -> dict:
    # Internal retry/checkpoint state is excluded; transcripts are explicitly tagged.
    return {
        k: row[k]
        for k in (
            "chat_id",
            "message_id",
            "date_utc",
            "date_local",
            "sender_id",
            "sender",
            "outgoing",
            "kind",
            "text",
            "reply_to",
            "forwarded",
            "edited_at",
            "duration_seconds",
            "transcript",
            "transcript_language",
            "transcription_state",
            "error",
        )
    } | {
        "outgoing": bool(row["outgoing"]),
        "forwarded": bool(row["forwarded"]),
        "transcript_source": "local_whisper" if row["transcript"] is not None else None,
        "segments": json.loads(row["segments"]) if row["segments"] else [],
    }


def markdown(title: str, day: str, tz: str, rows: list[dict]) -> str:
    result = [
        f"# {html.escape(title)} — {day}",
        "",
        f"Часовой пояс: `{tz}`.",
        "",
        "Расшифровки созданы автоматически; ошибки распознавания возможны.",
        "",
    ]
    for r in rows:
        stamp = r["date_local"].split("T", 1)[1]
        direction = "я →" if r["outgoing"] else "← собеседник"
        sender = html.escape(r["sender"]).replace("\n", " ")
        result.extend([f"## {stamp} · {sender} · {direction} · #{r['message_id']}", ""])
        if r["reply_to"]:
            result.extend([f"Ответ на #{r['reply_to']}.", ""])
        if r["forwarded"]:
            result.extend(["Пересланное сообщение.", ""])
        if r["edited_at"]:
            result.extend([f"Изменено: {r['edited_at']}.", ""])
        if r["text"]:
            # Blockquotes distinguish actual message content from journal headings.
            result.extend(
                [
                    "> " + html.escape(line, quote=False)
                    for line in r["text"].splitlines()
                ]
                + [""]
            )
        if r["kind"] != "text":
            duration = (
                f" · {r['duration_seconds']:g} сек."
                if r["duration_seconds"] is not None
                else ""
            )
            result.extend([f"Тип: `{r['kind']}`{duration}", ""])
        state = r["transcription_state"]
        if state in ("done", "no_speech"):
            result.extend(["**Автоматическая расшифровка:**", ""])
            if r["transcript"]:
                result.extend(
                    [
                        "> " + html.escape(line, quote=False)
                        for line in r["transcript"].splitlines()
                    ]
                    + [""]
                )
            else:
                result.extend(["Речь не обнаружена.", ""])
        elif state != "not_applicable":
            reason = f" ({r['error']})" if r["error"] else ""
            result.extend([f"Расшифровка: `{state}`{reason}.", ""])
    return "\n".join(result) + "\n"


def write_changed(path: Path, content: str):
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    atomic_text(path, content)


def export_period(
    store: Store, cfg: Config, days: int, *, now=None, destination: Path | None = None
):
    first, today, start, end = day_window(cfg.timezone, days, now)
    rows = store.rows_between(cfg.chat_id, start, end)
    groups = {}
    for row in rows:
        local = datetime.fromisoformat(row["date_utc"]).astimezone(
            ZoneInfo(cfg.timezone)
        )
        row["date_local"] = local.isoformat()
        row["day"] = local.date().isoformat()
        groups.setdefault(row["day"], []).append(row)
    title = f"История: {first or 'начало'} — {today}"
    content = f"# {title}\n\nСообщений: {len(rows)}.\n\n"
    for day, messages in groups.items():
        content += markdown(cfg.chat_title, day, cfg.timezone, messages) + "\n"
    base = destination or (
        Path(cfg.output_dir)
        / str(cfg.chat_id)
        / (f"history_{days}_days" if days else "history_all")
    )
    write_changed(base.with_suffix(".md"), content)
    write_changed(
        base.with_suffix(".jsonl"),
        "".join(
            json.dumps(exported_record(r), ensure_ascii=False) + "\n" for r in rows
        ),
    )
    return base.with_suffix(".md"), len(rows)


def export_daily_view(store: Store, cfg: Config, *, now=None):
    _, today, start, end = day_window(cfg.timezone, 1, now)
    rows = store.rows_between(cfg.chat_id, start, end)
    for row in rows:
        row["date_local"] = (
            datetime.fromisoformat(row["date_utc"])
            .astimezone(ZoneInfo(cfg.timezone))
            .isoformat()
        )
    root = Path(cfg.output_dir) / str(cfg.chat_id)
    md = markdown(cfg.chat_title, str(today), cfg.timezone, rows)
    jsonl = "".join(
        json.dumps(exported_record(r), ensure_ascii=False) + "\n" for r in rows
    )
    # Create an empty dated file at midnight too, while leaving yesterday's archive intact.
    for name, text in (
        (f"{today}.md", md),
        (f"{today}.jsonl", jsonl),
        ("today.md", md),
        ("today.jsonl", jsonl),
    ):
        write_changed(root / name, text)


def export_dirty(store: Store, cfg: Config, *, now=None) -> int:
    root = Path(cfg.output_dir).expanduser() / str(cfg.chat_id)
    days = store.dirty_days(cfg.chat_id)
    for day in days:
        rows = store.rows(cfg.chat_id, day)
        atomic_text(
            root / f"{day}.jsonl",
            "".join(
                json.dumps(exported_record(r), ensure_ascii=False) + "\n" for r in rows
            ),
        )
        atomic_text(
            root / f"{day}.md", markdown(cfg.chat_title, day, cfg.timezone, rows)
        )
        # Both files have been replaced before the dirty marker is cleared.
        store.clean(cfg.chat_id, day)
    if cfg.export_mode == "daily":
        export_daily_view(store, cfg, now=now)
    else:
        export_period(store, cfg, cfg.history_days, now=now)
    write_changed(
        root / "chat.json",
        json.dumps(
            {
                "chat_id": cfg.chat_id,
                "title": cfg.chat_title,
                "own_id": cfg.own_id,
                "timezone": cfg.timezone,
                "schema_version": 1,
                "transcription_source": "local_whisper",
                "history_since": cfg.history_since,
                "export_mode": cfg.export_mode,
                "history_days": cfg.history_days,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    return len(days)

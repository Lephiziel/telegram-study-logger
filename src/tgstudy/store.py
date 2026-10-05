from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


class Store:
    """SQLite is the journal; exports are rebuildable views, not checkpoints."""

    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
            day TEXT NOT NULL, date_utc TEXT NOT NULL, date_local TEXT NOT NULL,
            sender_id INTEGER, sender TEXT NOT NULL, outgoing INTEGER NOT NULL,
            kind TEXT NOT NULL, text TEXT NOT NULL, reply_to INTEGER,
            forwarded INTEGER NOT NULL, edited_at TEXT, media_key TEXT,
            duration_seconds REAL, media_bytes INTEGER NOT NULL DEFAULT 0,
            transcript TEXT, transcript_language TEXT, segments TEXT,
            transcription_state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            retry_at REAL NOT NULL DEFAULT 0, error TEXT,
            PRIMARY KEY(chat_id, message_id)
        );
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS dirty(chat_id INTEGER NOT NULL, day TEXT NOT NULL,
            PRIMARY KEY(chat_id, day));
        CREATE INDEX IF NOT EXISTS messages_day ON messages(chat_id, day, date_utc);
        CREATE INDEX IF NOT EXISTS messages_pending ON messages(chat_id, transcription_state, retry_at);
        """)

    def close(self):
        self.db.close()

    def _dirty(self, chat: int, day: str):
        self.db.execute("INSERT OR IGNORE INTO dirty VALUES (?, ?)", (chat, day))

    def get(self, chat: int, mid: int):
        row = self.db.execute(
            "SELECT * FROM messages WHERE chat_id=? AND message_id=?", (chat, mid)
        ).fetchone()
        return dict(row) if row else None

    def upsert(self, record: dict) -> None:
        chat, mid = record["chat_id"], record["message_id"]
        old = self.get(chat, mid)
        if (
            old
            and old["edited_at"]
            and (not record["edited_at"] or old["edited_at"] > record["edited_at"])
        ):
            return  # A slower history request must not overwrite a newer live edit.
        with self.db:
            if old is None:
                keys = list(record)
                self.db.execute(
                    f"INSERT INTO messages ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                    [record[k] for k in keys],
                )
            else:
                # A caption edit must not discard a completed transcript.
                reset = (
                    old["media_key"] != record["media_key"]
                    or old["kind"] != record["kind"]
                )
                updated = dict(record)
                if not reset:
                    updated.pop("transcription_state", None)
                else:
                    updated.update(
                        transcript=None,
                        transcript_language=None,
                        segments=None,
                        attempts=0,
                        retry_at=0,
                        error=None,
                    )
                changed = any(old[k] != v for k, v in updated.items())
                if not changed:
                    return
                assignments = ",".join(f"{k}=?" for k in updated)
                self.db.execute(
                    f"UPDATE messages SET {assignments} WHERE chat_id=? AND message_id=?",
                    [*updated.values(), chat, mid],
                )
                self._dirty(chat, old["day"])
            self._dirty(chat, record["day"])

    def cursor(self, chat: int) -> int:
        row = self.db.execute(
            "SELECT value FROM meta WHERE key=?", (f"history_cursor:{chat}",)
        ).fetchone()
        return int(row[0]) if row else 0

    def reset_cursor(self, chat: int):
        with self.db:
            self.db.execute("DELETE FROM meta WHERE key=?", (f"history_cursor:{chat}",))

    def rows_between(self, chat: int, start, end):
        query = "SELECT * FROM messages WHERE chat_id=? AND date_utc<?"
        params = [chat, end.isoformat()]
        if start is not None:
            query += " AND date_utc>=?"
            params.append(start.isoformat())
        query += " ORDER BY date_utc, message_id"
        return [dict(row) for row in self.db.execute(query, params)]

    def advance(self, chat: int, mid: int):
        # Only the ordered history scan calls this. Live events cannot advance it.
        with self.db:
            self.db.execute(
                "INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"history_cursor:{chat}", str(max(self.cursor(chat), mid))),
            )

    def next_job(self, chat: int):
        row = self.db.execute(
            """SELECT * FROM messages WHERE chat_id=?
            AND transcription_state IN ('pending','retry') AND retry_at<=?
            ORDER BY message_id LIMIT 1""",
            (chat, time.time()),
        ).fetchone()
        return dict(row) if row else None

    def result(
        self,
        chat: int,
        mid: int,
        *,
        state: str,
        transcript: str | None = None,
        language: str | None = None,
        segments: list | None = None,
        error: str | None = None,
        retry_seconds: int = 0,
        expected_media_key: str | None = None,
    ):
        old = self.get(chat, mid)
        if not old or (
            expected_media_key is not None and old["media_key"] != expected_media_key
        ):
            return  # Media was edited while recognition ran; keep the new job.
        with self.db:
            self.db.execute(
                """UPDATE messages SET transcription_state=?, transcript=?,
                transcript_language=?, segments=?, error=?, attempts=attempts+1, retry_at=?
                WHERE chat_id=? AND message_id=?""",
                (
                    state,
                    transcript,
                    language,
                    json.dumps(segments, ensure_ascii=False)
                    if segments is not None
                    else None,
                    error,
                    time.time() + retry_seconds,
                    chat,
                    mid,
                ),
            )
            self._dirty(chat, old["day"])

    def rows(self, chat: int, day: str):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM messages WHERE chat_id=? AND day=? ORDER BY date_utc, message_id",
                (chat, day),
            )
        ]

    def dirty_days(self, chat: int):
        return [
            r[0]
            for r in self.db.execute(
                "SELECT day FROM dirty WHERE chat_id=? ORDER BY day", (chat,)
            )
        ]

    def clean(self, chat: int, day: str):
        with self.db:
            self.db.execute("DELETE FROM dirty WHERE chat_id=? AND day=?", (chat, day))

    def mark_all_dirty(self, chat: int):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO dirty SELECT DISTINCT chat_id, day FROM messages WHERE chat_id=?",
                (chat,),
            )

    def retry_errors(self, chat: int):
        with self.db:
            self.db.execute(
                """UPDATE messages SET transcription_state='pending', retry_at=0,
                attempts=0, error=NULL WHERE chat_id=? AND transcription_state IN ('retry','error')""",
                (chat,),
            )

    def retranscribe(self, chat: int, start, end) -> int:
        query = (
            "SELECT message_id, day FROM messages WHERE chat_id=? "
            "AND media_key IS NOT NULL AND transcription_state!='not_applicable' "
            "AND date_utc<?"
        )
        params = [chat, end.isoformat()]
        if start is not None:
            query += " AND date_utc>=?"
            params.append(start.isoformat())
        rows = self.db.execute(query, params).fetchall()
        with self.db:
            for row in rows:
                self.db.execute(
                    """UPDATE messages SET transcription_state='pending',
                    transcript=NULL, transcript_language=NULL, segments=NULL,
                    attempts=0, retry_at=0, error=NULL
                    WHERE chat_id=? AND message_id=?""",
                    (chat, row["message_id"]),
                )
                self._dirty(chat, row["day"])
        return len(rows)

    def counts(self, chat: int):
        return dict(
            self.db.execute(
                "SELECT transcription_state, count(*) FROM messages WHERE chat_id=? GROUP BY transcription_state",
                (chat,),
            ).fetchall()
        )

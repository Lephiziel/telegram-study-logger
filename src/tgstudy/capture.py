from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telethon import utils
from telethon.tl.types import MessageMediaWebPage

from .config import Config


def kind_of(message, videos: bool = False) -> str:
    if isinstance(message.media, MessageMediaWebPage):
        return "link"  # A YouTube/Reels preview can expose message.video/document.
    if message.voice:
        return "voice"
    if message.video_note:
        return "video_note"
    if message.video and not message.gif:
        return "video" if videos else "video_ignored"
    if message.photo:
        return "photo"
    if message.sticker:
        return "sticker"
    if message.gif:
        return "animation"
    if message.document:
        return "document"
    if message.action:
        return "service"
    return "text"


def media_key(message) -> str | None:
    if isinstance(message.media, MessageMediaWebPage):
        return None
    doc = message.document
    return str(doc.id) if doc else None


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat() if dt else None


async def to_record(message, cfg: Config) -> dict:
    kind = kind_of(message, cfg.transcribe_videos)
    date = message.date or datetime.now(timezone.utc)
    local = date.astimezone(ZoneInfo(cfg.timezone))
    duration = getattr(message.file, "duration", None) if message.file else None
    size = getattr(message.file, "size", 0) or 0 if message.file else 0
    sender = message.sender
    if sender is None:
        # Most updates/history messages contain sender entities; resolve only when needed.
        try:
            sender = await message.get_sender()
        except (ValueError, TypeError):
            sender = None
    name = (
        utils.get_display_name(sender)
        if sender
        else str(message.sender_id or "Unknown")
    )
    eligible = kind in {"voice", "video_note", "video"}
    return {
        "chat_id": cfg.chat_id,
        "message_id": message.id,
        "day": local.date().isoformat(),
        "date_utc": iso(date),
        "date_local": local.isoformat(),
        "sender_id": message.sender_id,
        "sender": name,
        "outgoing": int(message.sender_id == cfg.own_id),
        "kind": kind,
        "text": message.raw_text or "",
        "reply_to": message.reply_to_msg_id,
        "forwarded": int(message.fwd_from is not None),
        "edited_at": iso(message.edit_date),
        "media_key": media_key(message),
        "duration_seconds": duration,
        "media_bytes": size,
        "transcription_state": "pending" if eligible else "not_applicable",
    }

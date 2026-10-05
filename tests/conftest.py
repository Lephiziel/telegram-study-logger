from datetime import datetime, timezone

import pytest
from telethon.tl.types import (
    Document,
    DocumentAttributeAudio,
    DocumentAttributeVideo,
    Message,
    MessageMediaDocument,
    PeerUser,
    User,
)

from tgstudy.config import Config
from tgstudy.store import Store


@pytest.fixture
def cfg(tmp_path):
    return Config(
        api_id=1,
        api_hash="a" * 32,
        chat_id=20,
        chat_title="Test Chat",
        own_id=10,
        output_dir=str(tmp_path / "exports"),
        timezone="Asia/Yekaterinburg",
    )


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "journal.sqlite3")
    yield db
    db.close()


def message(
    mid, *, kind="text", text="Hello", sender_id=20, doc_id=None, date=None, edited=None
):
    date = date or datetime(2026, 10, 4, 20, 30, tzinfo=timezone.utc)
    m = Message(
        id=mid,
        peer_id=PeerUser(20),
        from_id=PeerUser(sender_id),
        date=date,
        message=text,
        edit_date=edited,
    )
    if kind != "text":
        if kind == "voice":
            attr = DocumentAttributeAudio(duration=12, voice=True)
            mime = "audio/ogg"
        else:
            attr = DocumentAttributeVideo(
                duration=15.5, w=240, h=240, round_message=kind == "video_note"
            )
            mime = "video/mp4"
        doc = Document(
            id=doc_id or mid * 100,
            access_hash=1,
            file_reference=b"x",
            date=date,
            mime_type=mime,
            size=1024,
            dc_id=1,
            attributes=[attr],
        )
        m.media = MessageMediaDocument(document=doc)
    m._sender = User(id=sender_id, first_name="Me" if sender_id == 10 else "Friend")
    return m

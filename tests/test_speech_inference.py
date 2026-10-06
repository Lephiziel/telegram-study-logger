"""Opt-in real ASR regression: synthesized bilingual speech, media, store, export.

Run with TGSTUDY_REAL_SPEECH=1 after installing the speech-test extra.
Downloads test-only Piper voices and Whisper small; needs no Telegram account.
"""

import json
import os
import shutil
import wave
from pathlib import Path

import av
import numpy as np
import pytest
from conftest import message

from tgstudy.exporter import export_dirty
from tgstudy.transcribe import Transcriber
from tgstudy.worker import Worker

pytestmark = pytest.mark.skipif(
    os.environ.get("TGSTUDY_REAL_SPEECH") != "1",
    reason="Opt-in real speech inference downloads test-only models",
)


@pytest.fixture(scope="module")
def speech_models(tmp_path_factory):
    import onnxruntime
    from faster_whisper import WhisperModel
    from faster_whisper.audio import decode_audio
    from piper import PiperVoice, SynthesisConfig
    from piper.download_voices import download_voice

    onnxruntime.disable_telemetry_events()
    root = Path(
        os.environ.get("TGSTUDY_SPEECH_CACHE") or tmp_path_factory.mktemp("speech")
    )
    voices = root / "tts"
    voices.mkdir(parents=True, exist_ok=True)
    samples = []
    for name, text in [
        (
            "en_US-lessac-medium",
            "Hello Aiden. This is just a test voice message. "
            "I am speaking English first so we can check that the beginning is saved.",
        ),
        (
            "ru_RU-irina-medium",
            "И когда я начинаю говорить по-русски, "
            "оно тоже должно как-то поменяться. Теперь я проверяю, "
            "что обе части сообщения остались в расшифровке.",
        ),
    ]:
        download_voice(name, voices)
        voice = PiperVoice.load(voices / (name + ".onnx"))
        path = root / (name + "-fixture.wav")
        with wave.open(str(path), "wb") as out:
            voice.synthesize_wav(
                text, out, syn_config=SynthesisConfig(length_scale=1.5)
            )
        samples.append(decode_audio(str(path), sampling_rate=16000))
    model = WhisperModel(
        "small",
        device="cpu",
        compute_type="int8",
        cpu_threads=2,
        download_root=str(root / "models"),
    )
    return model, samples


@pytest.mark.parametrize(
    "order", [(0, 1), (1, 0)], ids=["english-russian", "russian-english"]
)
@pytest.mark.parametrize(
    "kind,extension,codec",
    [
        ("voice", ".ogg", "libopus"),
        ("video_note", ".mp4", "aac"),
    ],
)
async def test_real_mixed_speech_keeps_both_languages_in_exports(
    speech_models, cfg, store, tmp_path, order, kind, extension, codec
):
    model, samples = speech_models
    # This fixture checks speech coverage; uncertainty marking is tested separately.
    cfg.unclear_word_probability = 0
    audio = np.concatenate(
        [samples[order[0]], np.zeros(2400, np.float32), samples[order[1]]]
    )
    path = tmp_path / ("synthetic" + extension)
    with av.open(str(path), "w") as container:
        stream = container.add_stream(codec, rate=16000)
        if kind == "video_note":
            video = container.add_stream("mpeg4", rate=1)
            video.width = video.height = 32
            video.pix_fmt = "yuv420p"
            frame = av.VideoFrame.from_ndarray(
                np.zeros((32, 32, 3), np.uint8), format="rgb24"
            )
            for packet in video.encode(frame):
                container.mux(packet)
            for packet in video.encode(None):
                container.mux(packet)
        frame = av.AudioFrame.from_ndarray(audio[None, :], format="fltp", layout="mono")
        frame.sample_rate = 16000
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)

    m = message(1, kind=kind)

    class Client:
        async def get_messages(self, peer, ids):
            return m

        async def download_media(self, message, file):
            shutil.copyfile(path, file)
            return file

    transcriber = Transcriber(cfg, tmp_path)
    transcriber.model = model
    worker = Worker(Client(), cfg, tmp_path, store, transcriber)
    await worker.ingest(m)
    await worker.process_job(store.next_job(cfg.chat_id))
    row = store.get(cfg.chat_id, 1)
    assert row["transcription_state"] == "done", row["error"]
    text = row["transcript"].lower()
    for phrase in [
        "hello",
        "aiden",
        "test voice message",
        "beginning is saved",
        "говорить по-русски",
        "обе части сообщения",
        "расшифровке",
    ]:
        assert phrase in text, text
    assert text.count("test voice message") == 1
    assert text.count("говорить по-русски") == 1
    assert (text.index("test voice message") < text.index("говорить по-русски")) == (
        order[0] == 0
    )
    assert {s["language"] for s in json.loads(row["segments"])} == {"en", "ru"}
    export_dirty(store, cfg)
    folder = Path(cfg.output_dir) / str(cfg.chat_id)
    for extension in [".md", ".jsonl"]:
        assert row["transcript"] in (folder / ("2026-10-05" + extension)).read_text(
            encoding="utf-8"
        )
    assert worker.cache.get(row).is_file()
    assert list((tmp_path / "media-cache").rglob("*.part*")) == []


def test_real_long_english_russian_english_keeps_beginning_middle_and_end(
    speech_models, cfg, tmp_path, monkeypatch
):
    model, samples = speech_models
    cfg.unclear_word_probability = 0
    order = [0, 0, 0, 1, 1, 0, 0]
    pieces = []
    for i in order:
        pieces.extend([samples[i], np.zeros(4800, np.float32)])
    audio = np.concatenate(pieces)
    path = tmp_path / "long-mixed.wav"
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes((audio * 32767).astype(np.int16).tobytes())
    original = model.transcribe
    durations = []

    def observed(audio, **kwargs):
        durations.append(len(audio) / 16000)
        return original(audio, **kwargs)

    monkeypatch.setattr(model, "transcribe", observed)
    transcriber = Transcriber(cfg, tmp_path)
    transcriber.model = model
    result = transcriber.run(path)
    languages = [s["language"] for s in result["segments"]]
    changes = [
        language
        for i, language in enumerate(languages)
        if not i or language != languages[i - 1]
    ]
    assert changes == ["en", "ru", "en"]
    assert len(audio) / 16000 > 60
    assert max(durations) <= cfg.speech_chunk_seconds + 1
    text = result["text"].lower()
    assert text.startswith("hello")
    assert "говорить по-русски" in text and "обе части сообщения" in text
    assert text.rfind("beginning is saved") > text.rfind("расшифровке")
    assert text.count("test voice message") == 5

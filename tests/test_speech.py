from types import SimpleNamespace

import av
import numpy as np
import pytest
from faster_whisper.audio import decode_audio
from faster_whisper.vad import get_speech_timestamps

from tgstudy.transcribe import Transcriber


@pytest.mark.parametrize("suffix,codec", [(".ogg", "libopus"), (".mp4", "aac")])
def test_real_audio_and_video_container_decoding(tmp_path, suffix, codec):
    path = tmp_path / ("recording" + suffix)
    rate = 48000
    samples = (np.sin(np.arange(rate) * 2 * np.pi * 440 / rate) * 0.1).astype(
        np.float32
    )[None, :]
    with av.open(str(path), "w") as container:
        stream = container.add_stream(codec, rate=rate)
        if suffix == ".mp4":
            video = container.add_stream("mpeg4", rate=1)
            video.width = video.height = 32
            video.pix_fmt = "yuv420p"
            pixels = np.zeros((32, 32, 3), dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
            for packet in video.encode(frame):
                container.mux(packet)
            for packet in video.encode(None):
                container.mux(packet)
        frame = av.AudioFrame.from_ndarray(samples, format="fltp", layout="mono")
        frame.sample_rate = rate
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    decoded = decode_audio(str(path), sampling_rate=16000)
    assert decoded.dtype == np.float32
    assert 15000 <= len(decoded) <= 18000
    assert float(np.max(np.abs(decoded))) > 0.05


def test_bundled_vad_on_silence():
    assert get_speech_timestamps(np.zeros(32000, dtype=np.float32)) == []


def test_recognizer_consumes_lazy_segments_and_preserves_language(cfg, tmp_path):
    class Model:
        def transcribe(self, path, **kwargs):
            assert kwargs["task"] == "transcribe"
            assert kwargs["vad_filter"] is True
            assert kwargs["condition_on_previous_text"] is False
            assert kwargs["multilingual"] is True
            assert kwargs["language"] is None
            return iter(
                [
                    SimpleNamespace(
                        start=0.0,
                        end=2.0,
                        text=" Hello there. ",
                        avg_logprob=-0.1,
                        no_speech_prob=0.01,
                    )
                ]
            ), SimpleNamespace(language="en")

    transcriber = Transcriber(cfg, tmp_path)
    transcriber.model = Model()
    result = transcriber.run(tmp_path / "unused-by-mock.ogg")
    assert result["text"] == "Hello there."
    assert result["language"] == "en"
    assert result["segments"][0]["end"] == 2


@pytest.mark.parametrize(
    "text",
    [
        "Привет! Сегодня я говорю по-русски.",
        "I want some soda, we call it лимонад in Russia.",
    ],
)
def test_multilingual_does_not_force_saved_english_language(cfg, tmp_path, text):
    cfg.language = "en"  # A configuration saved by version 0.2.

    class Model:
        def transcribe(self, path, **kwargs):
            assert kwargs["language"] is None
            assert kwargs["multilingual"] is True
            assert kwargs["task"] == "transcribe"
            return iter(
                [
                    SimpleNamespace(
                        start=0, end=5, text=text, avg_logprob=0, no_speech_prob=0
                    )
                ]
            ), SimpleNamespace(language="ru")

    transcriber = Transcriber(cfg, tmp_path)
    transcriber.model = Model()
    assert transcriber.run(tmp_path / "speech.ogg")["text"] == text


def test_fixed_language_remains_opt_in(cfg, tmp_path):
    cfg.multilingual = False
    cfg.language = "ru"

    class Model:
        def transcribe(self, path, **kwargs):
            assert kwargs["language"] == "ru" and kwargs["multilingual"] is False
            return iter([]), SimpleNamespace(language="ru")

    transcriber = Transcriber(cfg, tmp_path)
    transcriber.model = Model()
    assert transcriber.run(tmp_path / "speech.ogg")["text"] == ""


def test_english_only_named_model_upgrades_to_multilingual(cfg, tmp_path, monkeypatch):
    import faster_whisper

    calls = []

    def create(name, **kwargs):
        calls.append(name)
        return SimpleNamespace(model=SimpleNamespace(is_multilingual=True))

    monkeypatch.setattr(faster_whisper, "WhisperModel", create)
    cfg.model = "small.en"
    Transcriber(cfg, tmp_path).load()
    assert calls == ["small"]


def test_custom_english_only_model_rejected_for_multilingual(
    cfg, tmp_path, monkeypatch
):
    import faster_whisper

    monkeypatch.setattr(
        faster_whisper,
        "WhisperModel",
        lambda *a, **kw: SimpleNamespace(model=SimpleNamespace(is_multilingual=False)),
    )
    cfg.model = str(tmp_path / "custom-english-only")
    with pytest.raises(ValueError, match="MultilingualModelRequired"):
        Transcriber(cfg, tmp_path).load()

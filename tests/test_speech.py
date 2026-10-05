from types import SimpleNamespace

import av
import numpy as np
import pytest
from faster_whisper.audio import decode_audio
from faster_whisper.vad import get_speech_timestamps

import tgstudy.transcribe as speech_module
from tgstudy.transcribe import SAMPLE_RATE, Transcriber, language_regions


@pytest.fixture
def speech_input(monkeypatch):
    audio = np.zeros(8 * SAMPLE_RATE, dtype=np.float32)
    monkeypatch.setattr(speech_module, "decode_audio", lambda *a, **kw: audio)
    monkeypatch.setattr(
        speech_module,
        "get_speech_timestamps",
        lambda *a, **kw: [{"start": 0, "end": len(audio)}],
    )


def word(text, start, end):
    return SimpleNamespace(word=text, start=start, end=end, probability=0.9)


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


def test_recognizer_consumes_lazy_segments_and_preserves_language(
    cfg, tmp_path, speech_input
):
    class Model:
        def detect_language(self, **kwargs):
            return "en", 0.9, []

        def transcribe(self, path, **kwargs):
            assert kwargs["task"] == "transcribe"
            assert kwargs["vad_filter"] is True
            assert kwargs["condition_on_previous_text"] is False
            assert kwargs["multilingual"] is False
            assert kwargs["language"] == "en"
            assert kwargs["word_timestamps"] is True
            return iter(
                [
                    SimpleNamespace(
                        start=0.0,
                        end=2.0,
                        text=" Hello there. ",
                        words=[word(" Hello there.", 0, 2)],
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
def test_multilingual_does_not_force_saved_english_language(
    cfg, tmp_path, text, speech_input
):
    cfg.language = "en"  # A configuration saved by version 0.2.

    class Model:
        def detect_language(self, **kwargs):
            return "ru", 0.9, []

        def transcribe(self, path, **kwargs):
            assert kwargs["language"] == "ru"
            assert kwargs["multilingual"] is False
            assert kwargs["task"] == "transcribe"
            return iter(
                [
                    SimpleNamespace(
                        start=0,
                        end=5,
                        text=text,
                        avg_logprob=0,
                        no_speech_prob=0,
                        words=[word(text, 0, 5)],
                    )
                ]
            ), SimpleNamespace(language="ru")

    transcriber = Transcriber(cfg, tmp_path)
    transcriber.model = Model()
    assert transcriber.run(tmp_path / "speech.ogg")["text"] == text


@pytest.mark.parametrize("boundary", [3, 7, 8.6, 11.2, 17.4])
@pytest.mark.parametrize("languages", [("en", "ru"), ("ru", "en")])
def test_language_changes_snap_to_pause_and_cover_entire_recording(boundary, languages):
    # The sample values encode original timestamps for this test detector.
    audio = np.arange(24 * SAMPLE_RATE, dtype=np.float32)
    split = int(boundary * SAMPLE_RATE)

    class Model:
        def detect_language(self, audio):
            language = languages[0] if np.mean(audio) < split else languages[1]
            return language, 0.9, []

    speech = [
        {"start": 0, "end": split - 1600},
        {"start": split + 1600, "end": len(audio)},
    ]
    assert language_regions(Model(), audio, speech) == [
        {"start": 0, "end": split, "language": languages[0]},
        {"start": split, "end": len(audio), "language": languages[1]},
    ]


def test_long_silence_is_not_used_for_language_detection():
    audio = np.arange(16 * SAMPLE_RATE, dtype=np.float32)
    calls = []

    class Model:
        def detect_language(self, audio):
            calls.append(audio)
            return "en", 0.9, []

    regions = language_regions(Model(), audio, [{"start": 64000, "end": 96000}])
    assert len(calls) == 1
    assert regions == [{"start": 0, "end": len(audio), "language": "en"}]


def test_next_sentence_pause_cannot_cut_off_new_language_intro():
    audio = np.arange(16 * SAMPLE_RATE, dtype=np.float32)
    boundary = int(9.4 * SAMPLE_RATE)

    class Model:
        def detect_language(self, audio):
            language = "ru" if np.mean(audio) < boundary else "en"
            return language, 0.9, [(language, 0.9)]

    speech = [
        {"start": 0, "end": boundary - 800},
        {"start": boundary + 800, "end": int(10.5 * SAMPLE_RATE)},
        {"start": int(10.7 * SAMPLE_RATE), "end": len(audio)},
    ]
    # The coarse change is at 10 seconds, nearer the pause after "Hello Aiden".
    # Language evidence must select 9.4 seconds and retain that greeting.
    assert language_regions(Model(), audio, speech) == [
        {"start": 0, "end": boundary, "language": "ru"},
        {"start": boundary, "end": len(audio), "language": "en"},
    ]


def test_repeated_language_changes_keep_the_original_order():
    audio = np.arange(24 * SAMPLE_RATE, dtype=np.float32)

    class Model:
        def detect_language(self, audio):
            second = np.mean(audio) / SAMPLE_RATE
            language = "ru" if 6 <= second < 14 else "en"
            return language, 0.9, []

    speech = [
        {"start": 0, "end": int(5.9 * SAMPLE_RATE)},
        {"start": int(6.1 * SAMPLE_RATE), "end": int(13.9 * SAMPLE_RATE)},
        {"start": int(14.1 * SAMPLE_RATE), "end": len(audio)},
    ]
    assert language_regions(Model(), audio, speech) == [
        {"start": 0, "end": 6 * SAMPLE_RATE, "language": "en"},
        {"start": 6 * SAMPLE_RATE, "end": 14 * SAMPLE_RATE, "language": "ru"},
        {"start": 14 * SAMPLE_RATE, "end": len(audio), "language": "en"},
    ]


def test_silent_recording_does_not_invent_a_transcript(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(
        speech_module,
        "decode_audio",
        lambda *a, **kw: np.zeros(32000, dtype=np.float32),
    )
    transcriber = Transcriber(cfg, tmp_path)
    transcriber.model = object()  # A recognizer must not be called on silence.
    assert transcriber.run(tmp_path / "silence.ogg") == {
        "text": "",
        "language": None,
        "segments": [],
    }


def test_overlapping_context_is_joined_once_with_original_timestamps(
    cfg, tmp_path, monkeypatch, speech_input
):
    monkeypatch.setattr(
        speech_module,
        "language_regions",
        lambda *args: [
            {"start": 0, "end": 4 * SAMPLE_RATE, "language": "en"},
            {"start": 4 * SAMPLE_RATE, "end": 8 * SAMPLE_RATE, "language": "ru"},
        ],
    )
    calls = []

    class Model:
        def transcribe(self, audio, **kwargs):
            calls.append((len(audio), kwargs["language"]))
            words = (
                [
                    word(" Hello", 0, 1),
                    word(" English.", 3, 3.5),
                    word(" Привет", 4, 4.4),
                ]
                if kwargs["language"] == "en"
                else [
                    word(" English.", 0, 0),
                    word(" Привет", 0.5, 0.9),
                    word(" русский.", 2, 2.5),
                ]
            )

            def lazy():
                yield SimpleNamespace(words=words, avg_logprob=-0.1, no_speech_prob=0)

            return lazy(), SimpleNamespace(language=kwargs["language"])

    transcriber = Transcriber(cfg, tmp_path)
    transcriber.model = Model()
    result = transcriber.run(tmp_path / "speech.ogg")
    assert result["text"] == "Hello English. Привет русский."
    assert result["language"] == "en"
    assert result["segments"][1]["start"] == 4
    assert result["segments"][1]["end"] == 6
    assert [s["language"] for s in result["segments"]] == ["en", "ru"]
    assert calls == [(72000, "en"), (72000, "ru")]


def test_pause_boundary_does_not_include_previous_language_context(
    cfg, tmp_path, monkeypatch, speech_input
):
    monkeypatch.setattr(
        speech_module,
        "get_speech_timestamps",
        lambda *a, **kw: [
            {"start": 0, "end": 62400},
            {"start": 65600, "end": 128000},
        ],
    )
    monkeypatch.setattr(
        speech_module,
        "language_regions",
        lambda *args: [
            {"start": 0, "end": 64000, "language": "ru"},
            {"start": 64000, "end": 128000, "language": "en"},
        ],
    )

    class Model:
        def transcribe(self, audio, **kwargs):
            assert len(audio) == 64000  # No previous-language padding at a pause.
            text = " Привет." if kwargs["language"] == "ru" else " Hello Aiden."
            return iter(
                [
                    SimpleNamespace(
                        words=[word(text, 0, 0.5)],
                        avg_logprob=-0.1,
                        no_speech_prob=0,
                    )
                ]
            ), SimpleNamespace(language=kwargs["language"])

    transcriber = Transcriber(cfg, tmp_path)
    transcriber.model = Model()
    result = transcriber.run(tmp_path / "speech.ogg")
    assert result["text"] == "Привет. Hello Aiden."
    assert result["segments"][1]["start"] == 4


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

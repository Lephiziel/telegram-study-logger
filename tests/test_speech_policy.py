from types import SimpleNamespace

import numpy as np
import pytest

import tgstudy.transcribe as speech_module
from tgstudy.transcribe import (
    SAMPLE_RATE,
    Transcriber,
    expected_language,
    recognition_chunks,
)


@pytest.mark.parametrize(
    "expected,scores",
    [
        ("en", [("pt", 0.8), ("en", 0.15), ("ru", 0.05)]),
        ("ru", [("ro", 0.7), ("ru", 0.25), ("en", 0.05)]),
    ],
)
def test_third_language_vote_cannot_select_a_third_decoder_language(expected, scores):
    model = SimpleNamespace(
        detect_language=lambda **kw: (scores[0][0], scores[0][1], scores)
    )
    assert expected_language(model, np.zeros(16000), ["en", "ru"]) == expected


def test_decoder_fallback_stays_in_expected_languages():
    model = SimpleNamespace(detect_language=lambda **kw: ("hi", 1.0, [("hi", 1.0)]))
    assert expected_language(model, np.zeros(16000), ["en", "ru"], "ru") == "ru"


@pytest.mark.parametrize("with_pauses", [True, False])
def test_72_second_speech_is_bounded_and_keeps_original_timeline(with_pauses):
    region = {"start": 0, "end": 72 * SAMPLE_RATE, "language": "en"}
    speech = (
        [
            {"start": 0, "end": int(14.9 * SAMPLE_RATE)},
            {"start": int(15.1 * SAMPLE_RATE), "end": int(30.9 * SAMPLE_RATE)},
            {"start": int(31.1 * SAMPLE_RATE), "end": 72 * SAMPLE_RATE},
        ]
        if with_pauses
        else [{"start": 0, "end": 72 * SAMPLE_RATE}]
    )
    chunks = recognition_chunks([region], speech, 18)
    assert chunks[0]["start"] == 0 and chunks[-1]["end"] == 72 * SAMPLE_RATE
    assert all(c["end"] - c["start"] <= 18 * SAMPLE_RATE for c in chunks)
    assert all(a["end"] == b["start"] for a, b in zip(chunks, chunks[1:]))
    if with_pauses:
        assert chunks[0]["end"] == 15 * SAMPLE_RATE


def setup_audio(monkeypatch):
    monkeypatch.setattr(
        speech_module,
        "decode_audio",
        lambda *a, **kw: np.zeros(8 * SAMPLE_RATE, np.float32),
    )
    monkeypatch.setattr(
        speech_module,
        "get_speech_timestamps",
        lambda *a, **kw: [{"start": 0, "end": 8 * SAMPLE_RATE}],
    )


def segment(words):
    return SimpleNamespace(
        words=[
            SimpleNamespace(word=text, start=i, end=i + 0.5, probability=prob)
            for i, (text, prob) in enumerate(words)
        ],
        avg_logprob=-0.1,
        no_speech_prob=0,
    )


def test_verbatim_grammar_slang_and_repetitions_are_not_edited(
    cfg, tmp_path, monkeypatch
):
    setup_audio(monkeypatch)
    original = " I did script, when I Start, much difficult. Fuck, uh, uh."

    class Model:
        def detect_language(self, **kw):
            return "en", 0.9, [("en", 0.9)]

        def transcribe(self, audio, **kw):
            assert kw["task"] == "transcribe" and kw["temperature"] == 0
            assert kw["condition_on_previous_text"] is False
            return iter([segment([(original, 0.9)])]), SimpleNamespace(language="en")

    t = Transcriber(cfg, tmp_path)
    t.model = Model()
    assert t.run(tmp_path / "voice.ogg")["text"] == original.strip()


def test_suspicious_script_is_retried_with_stricter_context(cfg, tmp_path, monkeypatch):
    setup_audio(monkeypatch)
    calls = []

    class Model:
        def detect_language(self, **kw):
            return "pt", 0.8, [("pt", 0.8), ("en", 0.19), ("ru", 0.01)]

        def transcribe(self, audio, **kw):
            calls.append(kw)
            assert kw["language"] == "en"
            words = [(" नमस्ते", 0.9)] if len(calls) == 1 else [(" Hello there.", 0.9)]
            return iter([segment(words)]), SimpleNamespace(language="en")

    t = Transcriber(cfg, tmp_path)
    t.model = Model()
    result = t.run(tmp_path / "voice.ogg")
    assert result["text"] == "Hello there." and len(calls) == 2
    assert calls[1]["beam_size"] > calls[0]["beam_size"]
    assert "Only English and Russian" in calls[1]["initial_prompt"]
    assert result["segments"][0]["retried"] is True


def test_uncertain_words_are_marked_and_raw_hypotheses_retained(
    cfg, tmp_path, monkeypatch
):
    setup_audio(monkeypatch)

    class Model:
        def detect_language(self, **kw):
            return "en", 0.9, []

        def transcribe(self, audio, **kw):
            return iter(
                [segment([(" I did", 0.9), (" script", 0.02), (" yesterday.", 0.9)])]
            ), SimpleNamespace(language="en")

    t = Transcriber(cfg, tmp_path)
    t.model = Model()
    result = t.run(tmp_path / "voice.ogg")
    assert result["text"] == "I did [unclear] yesterday."
    assert result["segments"][0]["raw_text"] == "I did script yesterday."
    assert result["segments"][0]["uncertain_words"][0]["probability"] == 0.02


def test_failed_language_repair_does_not_publish_foreign_script(
    cfg, tmp_path, monkeypatch
):
    setup_audio(monkeypatch)

    class Model:
        def detect_language(self, **kw):
            return "ru", 0.9, []

        def transcribe(self, audio, **kw):
            return iter([segment([(" नमस्ते", 0.9)])]), SimpleNamespace(language="hi")

    t = Transcriber(cfg, tmp_path)
    t.model = Model()
    result = t.run(tmp_path / "voice.ogg")
    assert result["text"] == "[unclear]"
    assert result["segments"][0]["raw_text"] == "नमस्ते"

from __future__ import annotations

import unicodedata
from pathlib import Path

import numpy as np
from faster_whisper.audio import decode_audio
from faster_whisper.vad import VadOptions, get_speech_timestamps

from .config import Config

SAMPLE_RATE = 16000
VERBATIM_PROMPT = (
    "Um, uh, yeah, I mean... Ну, э-э... "
    "English and Russian conversation. Verbatim speech, including grammar mistakes, "
    "slang, profanity, fillers and repetitions. No translation or correction."
)
STRICT_PROMPT = (
    VERBATIM_PROMPT
    + " Only English and Russian are expected. Unclear speech: [unclear]."
)


def expected_language(model, audio, languages, fallback=None) -> str:
    detected, probability, probabilities = model.detect_language(audio=audio)
    scores = dict(probabilities or [(detected, probability)])
    best = max(languages, key=lambda code: scores.get(code, 0))
    if scores.get(best, 0) == 0:
        return fallback if fallback in languages else languages[0]
    return best


def unexpected_script(text: str) -> bool:
    return any(
        ch.isalpha()
        and not any(s in unicodedata.name(ch, "") for s in ["LATIN", "CYRILLIC"])
        for ch in text
    )


def language_regions(
    model, audio: np.ndarray, speech: list[dict], languages=("en", "ru")
) -> list[dict]:
    """Probe overlapping speech windows, then join adjacent windows of one language.

    A decoder's default 30-second window can silently omit the minority language.
    Detecting languages first lets each decoder retain the context of a full
    utterance, instead of cutting every sentence into short recognition windows.
    Sample offsets stay on the original timeline, including silence.
    """
    step = 2 * SAMPLE_RATE
    context = 4 * SAMPLE_RATE
    regions = []
    speech_index = 0
    for start in range(0, len(audio), step):
        end = min(start + step, len(audio))
        while speech_index < len(speech) and speech[speech_index]["end"] <= start:
            speech_index += 1
        if speech_index == len(speech) or speech[speech_index]["start"] >= end:
            continue  # Never detect a language from silence.
        if not regions:
            probe_start, probe_end = start, end
        else:
            probe_start = max(0, min(start - SAMPLE_RATE, len(audio) - context))
            probe_end = min(len(audio), probe_start + context)
        language = expected_language(
            model,
            audio[probe_start:probe_end],
            languages,
            regions[-1]["language"] if regions else None,
        )
        if regions and regions[-1]["language"] == language:
            continue
        regions.append({"start": start if regions else 0, "language": language})

    # A pause may belong to the next sentence in the NEW language. Compare
    # speech on both sides instead of choosing the geometrically nearest pause.
    pauses = [
        ((a["end"] + b["start"]) // 2, a["end"], b["start"])
        for a, b in zip(speech, speech[1:])
    ]
    for i in range(1, len(regions)):
        boundary = regions[i]["start"]
        upper = regions[i + 1]["start"] if i + 1 < len(regions) else len(audio)
        nearby = [
            (p, left, right)
            for p, left, right in pauses
            if abs(p - boundary) <= 1.5 * SAMPLE_RATE
            and regions[i - 1]["start"] < p < upper
        ]
        if nearby:
            old_language = regions[i - 1]["language"]
            new_language = regions[i]["language"]

            def score(pause):
                p, left, right = pause
                scores = []
                for lo, hi, wanted, other in [
                    (max(0, left - step), left, old_language, new_language),
                    (right, min(len(audio), right + step), new_language, old_language),
                ]:
                    language, probability, probabilities = model.detect_language(
                        audio=audio[lo:hi]
                    )
                    probabilities = dict(probabilities or [(language, probability)])
                    a = max(probabilities.get(wanted, 0), 1e-6)
                    b = max(probabilities.get(other, 0), 1e-6)
                    scores.append(a / (a + b))
                return scores[0] * scores[1], -abs(p - boundary)

            regions[i]["start"] = max(nearby, key=score)[0]
    for i, region in enumerate(regions):
        region["end"] = regions[i + 1]["start"] if i + 1 < len(regions) else len(audio)
    return regions


def recognition_chunks(
    regions: list[dict], speech: list[dict], max_seconds: int
) -> list[dict]:
    """Prefer pauses between 10 seconds and the hard duration limit; cover all audio."""
    pauses = [(a["end"] + b["start"]) // 2 for a, b in zip(speech, speech[1:])]
    chunks = []
    for region in regions:
        start = region["start"]
        while start < region["end"]:
            end = min(region["end"], start + max_seconds * SAMPLE_RATE)
            if end < region["end"]:
                candidates = [p for p in pauses if start + 10 * SAMPLE_RATE <= p <= end]
                if candidates:
                    end = max(candidates)
            if any(s["start"] < end and s["end"] > start for s in speech):
                chunks.append({**region, "start": start, "end": end})
            start = end
    return chunks


class MultilingualModelRequired(ValueError):
    pass


class Transcriber:
    def __init__(self, cfg: Config, root: Path):
        self.cfg = cfg
        self.root = root
        self.model = None

    def load(self):
        if self.model is None:
            import onnxruntime
            from faster_whisper import WhisperModel
            from faster_whisper.tokenizer import _LANGUAGE_CODES

            onnxruntime.disable_telemetry_events()

            if any(code not in _LANGUAGE_CODES for code in self.cfg.speech_languages):
                raise ValueError("UnsupportedExpectedSpeechLanguage")

            model_name = self.cfg.model
            if self.cfg.multilingual and model_name in {
                "tiny.en",
                "base.en",
                "small.en",
                "medium.en",
            }:
                model_name = model_name.removesuffix(".en")
            self.model = WhisperModel(
                model_name,
                device="cpu",
                compute_type="int8",
                cpu_threads=self.cfg.cpu_threads,
                num_workers=1,
                download_root=str(self.root / "models"),
            )
            if self.cfg.multilingual and not self.model.model.is_multilingual:
                self.model = None
                raise MultilingualModelRequired("MultilingualModelRequired")
        return self.model

    def run(self, path: Path) -> dict:
        model = self.load()
        if self.cfg.multilingual:
            return self.run_multilingual(model, path)
        segments, info = model.transcribe(
            str(path),
            language=self.cfg.language,
            multilingual=False,
            task="transcribe",
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
            chunk_length=30,
            temperature=0.0,
        )
        parts = []
        for s in segments:  # The actual inference is lazy; consume it in this thread.
            parts.append(
                {
                    "start": round(s.start, 3),
                    "end": round(s.end, 3),
                    "text": s.text.strip(),
                    "avg_logprob": s.avg_logprob,
                    "no_speech_prob": s.no_speech_prob,
                }
            )
        text = " ".join(p["text"] for p in parts).strip()
        return {"text": text, "language": info.language, "segments": parts}

    def run_multilingual(self, model, path: Path) -> dict:
        audio = decode_audio(str(path), sampling_rate=SAMPLE_RATE)
        speech = get_speech_timestamps(
            audio, VadOptions(min_silence_duration_ms=100, speech_pad_ms=0)
        )
        regions = language_regions(model, audio, speech, self.cfg.speech_languages)
        chunks = recognition_chunks(regions, speech, self.cfg.speech_chunk_seconds)
        parts = []
        padding = SAMPLE_RATE // 2
        pause_boundaries = {
            (a["end"] + b["start"]) // 2 for a, b in zip(speech, speech[1:])
        }
        for region in chunks:
            # At a real pause the other language's context can distort alignment
            # of the first word (e.g. "Hello"). Only overlap unpaused boundaries.
            clip_start = (
                region["start"]
                if region["start"] in pause_boundaries
                else max(0, region["start"] - padding)
            )
            clip_end = (
                region["end"]
                if region["end"] in pause_boundaries
                else min(len(audio), region["end"] + padding)
            )
            check_script = set(self.cfg.speech_languages) <= {"en", "ru"}
            prompt = (
                VERBATIM_PROMPT
                if check_script
                else (
                    "Verbatim conversation. Keep mistakes, fillers and repetitions. "
                    "Expected languages: " + ", ".join(self.cfg.speech_languages)
                )
            )
            detected, _, _ = model.detect_language(audio=audio[clip_start:clip_end])
            suspicious = False
            for attempt in range(2):
                segments, info = model.transcribe(
                    audio[clip_start:clip_end],
                    language=region["language"],
                    multilingual=False,
                    task="transcribe",
                    beam_size=5 if attempt == 0 else 10,
                    vad_filter=True,
                    condition_on_previous_text=False,
                    chunk_length=30,
                    word_timestamps=True,
                    temperature=0.0,
                    initial_prompt=prompt
                    if attempt == 0
                    else (
                        STRICT_PROMPT
                        if check_script
                        else prompt + ". No translation or correction."
                    ),
                )
                segments = list(segments)
                suspicious = (
                    (attempt == 0 and detected not in self.cfg.speech_languages)
                    or info.language not in self.cfg.speech_languages
                ) or (
                    check_script
                    and any(
                        unexpected_script(w.word)
                        for s in segments
                        for w in (s.words or [])
                    )
                )
                if not suspicious:
                    break
            for s in segments:  # Consume lazy inference before decoding the next clip.
                words = [
                    w
                    for w in (s.words or [])
                    if region["start"]
                    <= clip_start + (w.start + w.end) / 2 * SAMPLE_RATE
                    < region["end"]
                ]
                if not words:
                    continue
                uncertain = [
                    w
                    for w in words
                    if info.language not in self.cfg.speech_languages
                    or w.probability < self.cfg.unclear_word_probability
                    or (check_script and unexpected_script(w.word))
                ]
                text = "".join(
                    " [unclear]" if w in uncertain else w.word for w in words
                ).strip()
                parts.append(
                    {
                        "start": round(
                            max(0, clip_start / SAMPLE_RATE + words[0].start), 3
                        ),
                        "end": round(
                            min(
                                len(audio) / SAMPLE_RATE,
                                clip_start / SAMPLE_RATE + words[-1].end,
                            ),
                            3,
                        ),
                        "text": text,
                        "raw_text": "".join(w.word for w in words).strip(),
                        "language": region["language"],
                        "retried": attempt > 0,
                        "unexpected_language_vote": detected
                        if detected not in self.cfg.speech_languages
                        else None,
                        "uncertain_words": [
                            {
                                "text": w.word.strip(),
                                "probability": round(w.probability, 4),
                                "start": round(clip_start / SAMPLE_RATE + w.start, 3),
                                "end": round(clip_start / SAMPLE_RATE + w.end, 3),
                            }
                            for w in uncertain
                        ],
                        "avg_logprob": s.avg_logprob,
                        "no_speech_prob": s.no_speech_prob,
                    }
                )
        return {
            "text": " ".join(p["text"] for p in parts).strip(),
            "language": regions[0]["language"] if regions else None,
            "segments": parts,
        }

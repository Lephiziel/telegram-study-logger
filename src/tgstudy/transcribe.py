from __future__ import annotations

from pathlib import Path

import numpy as np
from faster_whisper.audio import decode_audio
from faster_whisper.vad import VadOptions, get_speech_timestamps

from .config import Config

SAMPLE_RATE = 16000


def language_regions(model, audio: np.ndarray, speech: list[dict]) -> list[dict]:
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
        language, _, _ = model.detect_language(audio=audio[probe_start:probe_end])
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


class MultilingualModelRequired(ValueError):
    pass


class Transcriber:
    def __init__(self, cfg: Config, root: Path):
        self.cfg = cfg
        self.root = root
        self.model = None

    def load(self):
        if self.model is None:
            from faster_whisper import WhisperModel

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
        regions = language_regions(model, audio, speech)
        parts = []
        padding = SAMPLE_RATE // 2
        pause_boundaries = {
            (a["end"] + b["start"]) // 2 for a, b in zip(speech, speech[1:])
        }
        for region in regions:
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
            segments, _ = model.transcribe(
                audio[clip_start:clip_end],
                language=region["language"],
                multilingual=False,
                task="transcribe",
                beam_size=5,
                vad_filter=True,
                condition_on_previous_text=False,
                chunk_length=30,
                word_timestamps=True,
            )
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
                        "text": "".join(w.word for w in words).strip(),
                        "language": region["language"],
                        "avg_logprob": s.avg_logprob,
                        "no_speech_prob": s.no_speech_prob,
                    }
                )
        return {
            "text": " ".join(p["text"] for p in parts).strip(),
            "language": regions[0]["language"] if regions else None,
            "segments": parts,
        }

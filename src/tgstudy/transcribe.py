from __future__ import annotations

from pathlib import Path

from .config import Config


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
        segments, info = model.transcribe(
            str(path),
            language=None if self.cfg.multilingual else self.cfg.language,
            multilingual=self.cfg.multilingual,
            task="transcribe",
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
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

from __future__ import annotations

from pathlib import Path

from .config import Config


class Transcriber:
    def __init__(self, cfg: Config, root: Path):
        self.cfg = cfg
        self.root = root
        self.model = None

    def load(self):
        if self.model is None:
            from faster_whisper import WhisperModel

            self.model = WhisperModel(
                self.cfg.model,
                device="cpu",
                compute_type="int8",
                cpu_threads=self.cfg.cpu_threads,
                num_workers=1,
                download_root=str(self.root / "models"),
            )
        return self.model

    def run(self, path: Path) -> dict:
        model = self.load()
        segments, info = model.transcribe(
            str(path),
            language=self.cfg.language,
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

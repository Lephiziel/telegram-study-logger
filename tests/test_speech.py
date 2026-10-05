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

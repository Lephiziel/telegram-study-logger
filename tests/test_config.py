from zoneinfo import ZoneInfoNotFoundError

import pytest

import tgstudy.config as config


def test_detects_system_timezone(monkeypatch):
    monkeypatch.setattr(config, "get_localzone_name", lambda: "Europe/Berlin")
    assert config.default_timezone() == "Europe/Berlin"


@pytest.mark.parametrize("error", [OSError, ValueError, ZoneInfoNotFoundError])
def test_timezone_detection_failure_offers_utc(monkeypatch, error):
    def unavailable():
        raise error("No usable timezone")

    monkeypatch.setattr(config, "get_localzone_name", unavailable)
    assert config.default_timezone() == "UTC"


def test_saved_timezone_is_preserved(cfg, tmp_path, monkeypatch):
    cfg.timezone = "America/New_York"
    cfg.save(tmp_path)
    monkeypatch.setattr(config, "get_localzone_name", lambda: "Europe/Berlin")
    assert config.Config.load(tmp_path).timezone == "America/New_York"


@pytest.mark.parametrize(
    "field,value",
    [
        ("speech_languages", []),
        ("speech_languages", ["en", "en"]),
        ("speech_languages", [1]),
        ("speech_languages", "en,ru"),
        ("speech_chunk_seconds", 30),
        ("speech_chunk_seconds", True),
        ("media_cache_days", 0),
        ("media_cache_max_mb", True),
        ("unclear_word_probability", 1.5),
    ],
)
def test_invalid_speech_and_cache_configuration_rejected(cfg, field, value):
    setattr(cfg, field, value)
    with pytest.raises(ValueError):
        cfg.validate()

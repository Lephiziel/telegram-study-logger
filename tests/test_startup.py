import plistlib
from types import SimpleNamespace

import pytest

from tgstudy import startup, watcher
from tgstudy.watcher import telegram_running


@pytest.mark.parametrize("name", ["Telegram", "Telegram.exe", "telegram-desktop"])
def test_process_names(name):
    process = SimpleNamespace(info={"name": name, "exe": ""})
    assert telegram_running(["Telegram", "Telegram.exe", "telegram-desktop"], [process])


def test_no_substring_false_positive():
    process = SimpleNamespace(info={"name": "mytelegramlogger", "exe": ""})
    assert not telegram_running(["Telegram"], [process])


@pytest.mark.parametrize("system", ["linux", "win32", "darwin"])
def test_autostart_generation(system, tmp_path, monkeypatch):
    root = tmp_path / "private data"
    root.mkdir()
    target = tmp_path / "autostart file"
    monkeypatch.setattr(startup.sys, "platform", system)
    monkeypatch.setattr(startup, "startup_path", lambda: target)
    monkeypatch.setattr(startup, "systemctl", lambda *args: False)
    monkeypatch.setattr(startup, "systemd_path", lambda: tmp_path / "test.service")
    monkeypatch.setattr(
        startup,
        "command",
        lambda mode: ["/path with spaces/python", "-m", "tgstudy", mode],
    )
    calls = []
    monkeypatch.setattr(startup, "spawn", lambda mode: calls.append(mode))
    assert startup.install_autostart(root) == target
    if system == "darwin":
        p = plistlib.loads(target.read_bytes())
        assert p["ProgramArguments"][0] == "/path with spaces/python"
        assert p["EnvironmentVariables"]["TGSTUDY_DATA_DIR"] == str(root)
        assert p["RunAtLoad"] is True
    elif system == "win32":
        text = target.read_text(encoding="utf-16")
        assert '""/path with spaces/python""' in text
        assert ", 0, False" in text
        assert "TGSTUDY_DATA_DIR" in text
    else:
        text = target.read_text(encoding="utf-8")
        assert "Terminal=false" in text
        assert '"/path with spaces/python"' in text
        assert "TGSTUDY_DATA_DIR" in text
    assert calls == ["watch"]
    startup.remove_autostart(root)
    assert not target.exists()
    assert (root / "watcher-stop").exists()


def test_systemd_startup_migrates_xdg_and_stops_on_uninstall(tmp_path, monkeypatch):
    root = tmp_path / "private data"
    root.mkdir()
    xdg = tmp_path / "old.desktop"
    xdg.write_text("old startup", encoding="utf-8")
    service = tmp_path / "unit.service"
    calls = []
    monkeypatch.setattr(startup.sys, "platform", "linux")
    monkeypatch.setattr(startup, "startup_path", lambda: xdg)
    monkeypatch.setattr(startup, "systemd_path", lambda: service)
    monkeypatch.setattr(
        startup, "command", lambda mode: ["/usr/bin/python", "-m", "tgstudy", mode]
    )
    monkeypatch.setattr(startup, "systemctl", lambda *args: calls.append(args) or True)
    monkeypatch.setattr(
        startup, "spawn", lambda *args: pytest.fail("systemd owns the watcher")
    )
    (root / "watcher-stop").touch()
    assert startup.install_autostart(root) == service
    text = service.read_text(encoding="utf-8")
    assert "WantedBy=default.target" in text
    assert "Restart=on-failure" in text
    assert "TGSTUDY_DATA_DIR=" in text
    assert not xdg.exists()
    assert not (root / "watcher-stop").exists()
    assert ("enable", "--now", startup.SERVICE) in calls
    startup.remove_autostart(root)
    assert not service.exists()
    assert ("disable", "--now", startup.SERVICE) in calls


def test_failed_systemd_enable_falls_back_to_xdg(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    xdg = tmp_path / "fallback.desktop"
    service = tmp_path / "failed.service"
    started = []
    monkeypatch.setattr(startup.sys, "platform", "linux")
    monkeypatch.setattr(startup, "startup_path", lambda: xdg)
    monkeypatch.setattr(startup, "systemd_path", lambda: service)
    monkeypatch.setattr(startup, "systemctl", lambda *args: args[0] != "enable")
    monkeypatch.setattr(startup, "spawn", lambda mode: started.append(mode))
    assert startup.install_autostart(root) == xdg
    assert xdg.exists() and not service.exists()
    assert started == ["watch"]


def test_systemd_quotes_literal_expansion_characters():
    assert (
        startup.systemd_quote("/tmp/50%/$chat", executable=True) == '"/tmp/50%%/$$chat"'
    )
    assert (
        startup.systemd_quote("TGSTUDY_DATA_DIR=/tmp/$chat")
        == '"TGSTUDY_DATA_DIR=/tmp/$chat"'
    )


def test_watcher_pauses_for_settings_and_restarts_with_new_mode(
    cfg, tmp_path, monkeypatch
):
    cfg.save(tmp_path)
    state = {"phase": 0}
    started = []

    class Child:
        pid = 123
        returncode = None

        def poll(self):
            if (tmp_path / "worker-stop").exists():
                self.returncode = 0
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = 0

        def kill(self):
            self.returncode = -9

    def spawn(*args):
        started.append(watcher.Config.load(tmp_path).export_mode)
        return Child()

    def sleep(seconds):
        state["phase"] += 1
        if state["phase"] == 1:
            cfg.export_mode = "daily"
            cfg.save(tmp_path)
        if state["phase"] == 4:
            (tmp_path / "watcher-stop").touch()

    monkeypatch.setattr(watcher, "spawn", spawn)
    monkeypatch.setattr(watcher, "telegram_running", lambda names: True)
    monkeypatch.setattr(
        watcher, "maintenance_running", lambda root: state["phase"] in {1, 2}
    )
    monkeypatch.setattr(watcher.time, "monotonic", lambda: 100 + 3 * state["phase"])
    monkeypatch.setattr(watcher.time, "sleep", sleep)
    monkeypatch.setattr(watcher.signal, "signal", lambda *args: None)
    watcher.watch(tmp_path)
    assert started == ["history", "daily"]

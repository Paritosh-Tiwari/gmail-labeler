"""Tests for tray.py pure helpers (icon, URL, command construction,
health probe). The pystray event loop itself is OS-level so we don't
test it -- the helpers here are everything that can break without an
actual tray rendered."""
from __future__ import annotations

import urllib.error
from pathlib import Path

import pytest
from PIL import Image

from quicklabel import tray as tray_mod
from quicklabel.tray import (
    Status,
    _check_health,
    audit_url,
    healthz_url,
    landing_url,
    lifecycle_command,
    make_icon,
    open_in_file_manager,
    queue_url,
    settings_url,
)


# --------------------------- make_icon ---------------------------

@pytest.mark.parametrize("status", [Status.UP, Status.DOWN, Status.DEGRADED])
def test_make_icon_returns_rgba_of_requested_size(status):
    img = make_icon(status, size=64)
    assert isinstance(img, Image.Image)
    assert img.mode == "RGBA"
    assert img.size == (64, 64)


def test_make_icon_center_pixel_is_opaque_and_colored():
    """Sanity check that something actually got drawn (not a blank canvas)."""
    img = make_icon(Status.UP, size=64)
    r, g, b, a = img.getpixel((32, 32))
    assert a == 255  # opaque
    assert (r, g, b) != (0, 0, 0)


def test_make_icon_corner_is_transparent():
    """The disk doesn't fill the square -- corners should be transparent."""
    img = make_icon(Status.UP, size=64)
    _r, _g, _b, a = img.getpixel((0, 0))
    assert a == 0


def test_make_icon_uses_distinct_colors_per_status():
    centers = {s: make_icon(s, size=64).getpixel((32, 32))[:3] for s in Status}
    # All three statuses must be distinguishable at a glance
    assert len({centers[Status.UP], centers[Status.DOWN], centers[Status.DEGRADED]}) == 3


def test_make_icon_respects_smallest_reasonable_size():
    img = make_icon(Status.UP, size=16)
    assert img.size == (16, 16)


# --------------------------- URL helpers ---------------------------

def test_url_helpers_use_localhost_and_given_port():
    assert healthz_url(8765) == "http://127.0.0.1:8765/healthz"
    assert landing_url(8765) == "http://127.0.0.1:8765/"
    assert queue_url(8765) == "http://127.0.0.1:8765/queue"
    assert settings_url(8765) == "http://127.0.0.1:8765/settings"
    assert audit_url(8765) == "http://127.0.0.1:8765/audit"


def test_url_helpers_honor_custom_port():
    assert queue_url(9001) == "http://127.0.0.1:9001/queue"


# --------------------------- lifecycle_command ---------------------------

def test_lifecycle_command_rejects_unknown_action():
    with pytest.raises(ValueError, match="unknown lifecycle action"):
        lifecycle_command("destroy")


def test_lifecycle_command_windows_when_script_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(tray_mod, "platform",
                        type("P", (), {"system": staticmethod(lambda: "Windows")}))
    monkeypatch.setattr(tray_mod, "_PROJECT_ROOT", tmp_path)
    (tmp_path / "restart.ps1").write_text("# stub")
    cmd = lifecycle_command("restart")
    assert cmd is not None
    assert cmd[0] == "powershell.exe"
    assert "-ExecutionPolicy" in cmd
    assert "Bypass" in cmd
    assert cmd[-1] == str(tmp_path / "restart.ps1")


def test_lifecycle_command_mac_when_script_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(tray_mod, "platform",
                        type("P", (), {"system": staticmethod(lambda: "Darwin")}))
    monkeypatch.setattr(tray_mod, "_PROJECT_ROOT", tmp_path)
    (tmp_path / "stop.command").write_text("#!/bin/zsh")
    cmd = lifecycle_command("stop")
    assert cmd == ["/bin/zsh", str(tmp_path / "stop.command")]


def test_lifecycle_command_returns_none_when_script_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(tray_mod, "platform",
                        type("P", (), {"system": staticmethod(lambda: "Windows")}))
    monkeypatch.setattr(tray_mod, "_PROJECT_ROOT", tmp_path)
    # No script written -- expect None so caller handles "not installed" case
    assert lifecycle_command("start") is None


# --------------------------- open_in_file_manager ---------------------------

def test_open_in_file_manager_windows(monkeypatch):
    monkeypatch.setattr(tray_mod, "platform",
                        type("P", (), {"system": staticmethod(lambda: "Windows")}))
    cmd = open_in_file_manager(Path("C:/foo"))
    assert cmd[0] == "explorer.exe"


def test_open_in_file_manager_mac(monkeypatch):
    monkeypatch.setattr(tray_mod, "platform",
                        type("P", (), {"system": staticmethod(lambda: "Darwin")}))
    p = Path("/foo")
    assert open_in_file_manager(p) == ["open", str(p)]


def test_open_in_file_manager_linux_falls_back_to_xdg(monkeypatch):
    monkeypatch.setattr(tray_mod, "platform",
                        type("P", (), {"system": staticmethod(lambda: "Linux")}))
    p = Path("/foo")
    assert open_in_file_manager(p) == ["xdg-open", str(p)]


# --------------------------- _check_health ---------------------------

class _FakeResp:
    def __init__(self, status):
        self.status = status

    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_check_health_up_on_200(monkeypatch):
    monkeypatch.setattr(tray_mod.urllib.request, "urlopen",
                        lambda *a, **kw: _FakeResp(200))
    assert _check_health(8765) == Status.UP


def test_check_health_degraded_on_5xx(monkeypatch):
    monkeypatch.setattr(tray_mod.urllib.request, "urlopen",
                        lambda *a, **kw: _FakeResp(503))
    assert _check_health(8765) == Status.DEGRADED


def test_check_health_down_on_connection_refused(monkeypatch):
    def raise_(*a, **kw):
        raise urllib.error.URLError("Connection refused")
    monkeypatch.setattr(tray_mod.urllib.request, "urlopen", raise_)
    assert _check_health(8765) == Status.DOWN


def test_check_health_down_on_timeout(monkeypatch):
    def raise_(*a, **kw):
        raise TimeoutError("slow")
    monkeypatch.setattr(tray_mod.urllib.request, "urlopen", raise_)
    assert _check_health(8765) == Status.DOWN


def test_check_health_down_on_unexpected_exception(monkeypatch):
    """Even an unrelated exception shouldn't crash the poll loop."""
    def raise_(*a, **kw):
        raise RuntimeError("weird")
    monkeypatch.setattr(tray_mod.urllib.request, "urlopen", raise_)
    assert _check_health(8765) == Status.DOWN

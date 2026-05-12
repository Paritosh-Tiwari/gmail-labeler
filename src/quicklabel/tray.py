"""System tray icon for QuickLabel.

A separate process from the server. Polls /healthz every 10s and shows
a green / yellow / red dot in the OS tray, plus a click-menu with
"Open queue", "Settings", "Restart server", etc.

The tray does NOT manage the server lifecycle directly -- the server
runs as its own auto-start service (Task Scheduler entry on Win, launchd
agent on Mac). The tray is a thin client that pokes the lifecycle
scripts (start.ps1 / stop.ps1 / restart.command) when the user clicks
the menu items. This means killing the tray doesn't kill the server,
and crashing the tray doesn't take the server with it.

Run with:  python -m quicklabel tray
"""
from __future__ import annotations

import logging
import platform
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from PIL import Image, ImageDraw

from .settings import HOST, load_settings


_LOG = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DATA_DIR = _PROJECT_ROOT / "data"
_POLL_INTERVAL_SEC = 10.0
_HEALTH_TIMEOUT_SEC = 2.0


class Status(Enum):
    UP = "up"            # /healthz returned 200
    DOWN = "down"        # connection refused / timeout
    DEGRADED = "degraded"  # got a response but not 200 (e.g. 5xx)


# --------------------------- pure helpers (testable) ---------------------------

@dataclass(frozen=True)
class StatusColor:
    rgb: tuple[int, int, int]
    label: str


_COLORS = {
    Status.UP:       StatusColor((30, 142, 62), "QuickLabel: running"),       # green
    Status.DOWN:     StatusColor((197, 34, 31), "QuickLabel: server is down"), # red
    Status.DEGRADED: StatusColor((217, 119, 6), "QuickLabel: degraded"),       # amber
}


def make_icon(status: Status, size: int = 64) -> Image.Image:
    """Return a square RGBA image with a solid colored disk for the
    given status. Used by both the live tray and the test suite."""
    color = _COLORS[status]
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = max(2, size // 12)
    draw.ellipse((pad, pad, size - pad, size - pad), fill=color.rgb + (255,))
    return img


def healthz_url(port: int) -> str:
    return f"http://{HOST}:{port}/healthz"


def landing_url(port: int) -> str:
    return f"http://{HOST}:{port}/"


def queue_url(port: int) -> str:
    return f"http://{HOST}:{port}/queue"


def settings_url(port: int) -> str:
    return f"http://{HOST}:{port}/settings"


def audit_url(port: int) -> str:
    return f"http://{HOST}:{port}/audit"


def lifecycle_command(action: str) -> list[str] | None:
    """Return the OS-appropriate command to invoke a lifecycle script
    (start / stop / restart). None if the script doesn't exist (which
    happens when the user is running QuickLabel from a checkout that
    skipped the install scripts)."""
    if action not in ("start", "stop", "restart"):
        raise ValueError(f"unknown lifecycle action: {action!r}")
    if platform.system() == "Windows":
        script = _PROJECT_ROOT / f"{action}.ps1"
        if not script.exists():
            return None
        return [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", str(script),
        ]
    else:
        script = _PROJECT_ROOT / f"{action}.command"
        if not script.exists():
            return None
        return ["/bin/zsh", str(script)]


def open_in_file_manager(path: Path) -> list[str]:
    """OS-appropriate command to open a folder in the file manager."""
    if platform.system() == "Windows":
        return ["explorer.exe", str(path)]
    if platform.system() == "Darwin":
        return ["open", str(path)]
    return ["xdg-open", str(path)]


# --------------------------- live tray loop ---------------------------

def _check_health(port: int) -> Status:
    """One-shot HEAD-equivalent against /healthz. Pure function over IO."""
    try:
        req = urllib.request.Request(healthz_url(port), method="GET")
        with urllib.request.urlopen(req, timeout=_HEALTH_TIMEOUT_SEC) as resp:
            return Status.UP if resp.status == 200 else Status.DEGRADED
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return Status.DOWN
    except Exception:
        return Status.DOWN


def _run_lifecycle(action: str) -> None:
    """Invoke a lifecycle script (start/stop/restart) in the background."""
    cmd = lifecycle_command(action)
    if cmd is None:
        _LOG.warning("Lifecycle script for %s not found", action)
        return
    try:
        # Don't wait -- these scripts can take several seconds (esp.
        # restart), and we don't want to freeze the tray menu.
        subprocess.Popen(
            cmd,
            cwd=str(_PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        _LOG.exception("Lifecycle %s failed: %s", action, e)


def _open_data_folder() -> None:
    try:
        subprocess.Popen(open_in_file_manager(_DATA_DIR))
    except Exception as e:
        _LOG.exception("Open data folder failed: %s", e)


def main() -> int:
    """Entry point for `python -m quicklabel tray`."""
    # Local import so installing pystray is only required when running
    # the tray (importing quicklabel for tests / scripts shouldn't pull
    # in a GUI lib that may fail in headless environments).
    import pystray
    from pystray import Menu, MenuItem

    port = load_settings().port

    icon_image = make_icon(Status.DOWN)  # initial "we don't know yet"
    icon = pystray.Icon(
        name="QuickLabel",
        icon=icon_image,
        title=_COLORS[Status.DOWN].label,
    )

    def _menu_open(url: str):
        return lambda _icon, _item: webbrowser.open(url)

    def _menu_lifecycle(action: str):
        return lambda _icon, _item: _run_lifecycle(action)

    icon.menu = Menu(
        MenuItem("Open landing page", _menu_open(landing_url(port))),
        MenuItem("Open queue",        _menu_open(queue_url(port))),
        MenuItem("Settings",          _menu_open(settings_url(port))),
        MenuItem("Audit log",         _menu_open(audit_url(port))),
        Menu.SEPARATOR,
        MenuItem("Open data folder",  lambda _i, _it: _open_data_folder()),
        Menu.SEPARATOR,
        MenuItem("Restart server",    _menu_lifecycle("restart")),
        MenuItem("Stop server",       _menu_lifecycle("stop")),
        Menu.SEPARATOR,
        MenuItem("Quit tray",         lambda _i, _it: icon.stop()),
    )

    stop_event = threading.Event()

    def poll_loop():
        last_status: Status | None = None
        while not stop_event.is_set():
            status = _check_health(port)
            if status != last_status:
                icon.icon = make_icon(status)
                icon.title = _COLORS[status].label
                last_status = status
            stop_event.wait(_POLL_INTERVAL_SEC)

    poller = threading.Thread(target=poll_loop, name="ql-tray-poll", daemon=True)
    poller.start()

    try:
        icon.run()  # blocks on the OS event loop until icon.stop() called
    finally:
        stop_event.set()
        poller.join(timeout=1.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())

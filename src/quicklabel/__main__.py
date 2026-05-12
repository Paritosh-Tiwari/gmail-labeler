"""CLI entry point: `python -m quicklabel <command>`.

Commands:
    serve     Start the local web server (default).
    setup     Run OAuth + print the bookmarklet text.
    bookmark  Just print the bookmarklet text.
    tray      Start the system-tray icon (separate process from serve).
"""
from __future__ import annotations

import os
import sys

# When launched via pythonw.exe (no console — used by Task Scheduler /
# launchd for hidden auto-start), sys.stdout and sys.stderr are None.
# Any library that writes to them (uvicorn does on startup) crashes the
# process with exit code 1 before file logging can attach. Replace them
# with /dev/null sinks so writes succeed silently. File-based logs are
# still captured via _logging.setup_logging in server.main().
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

from .auth import build_service, get_credentials
from .bookmarklet import bookmarklet_url
from .settings import BASE_URL


def cmd_setup() -> int:
    print("[1/2] Running OAuth (browser will open)...")
    creds = get_credentials()
    svc = build_service(creds)
    profile = svc.users().getProfile(userId="me").execute()
    print(f"      Authenticated as: {profile['emailAddress']}")
    print(f"      Total messages:   {profile['messagesTotal']:,}")
    print()
    print("[2/2] Bookmarklet — drag this link to your bookmark bar:")
    print()
    print(f"  >>> {bookmarklet_url()} <<<")
    print()
    print("Setup complete. Now run:  python -m quicklabel serve")
    return 0


def cmd_bookmark() -> int:
    print(bookmarklet_url())
    return 0


def cmd_serve() -> int:
    from .server import main
    main()
    return 0


def cmd_tray() -> int:
    from .tray import main as tray_main
    return tray_main()


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "serve":
        return cmd_serve()
    if cmd == "setup":
        return cmd_setup()
    if cmd == "bookmark":
        return cmd_bookmark()
    if cmd == "tray":
        return cmd_tray()
    print(f"Unknown command: {cmd}\nUsage: python -m quicklabel [serve|setup|bookmark|tray]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

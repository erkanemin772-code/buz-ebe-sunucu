"""Managed Render entrypoint. Public WSS/TLS is provided by Render's edge."""
import os
import sys
from pathlib import Path


def main():
    try:
        port = int(os.environ.get("PORT", "10000"))
        rooms = int(os.environ.get("BUZEBE_MAX_ROOMS", "50"))
    except ValueError:
        raise SystemExit("PORT and BUZEBE_MAX_ROOMS must be integers")
    if not 1 <= port <= 65535 or not 1 <= rooms <= 5000:
        raise SystemExit("PORT must be 1..65535; BUZEBE_MAX_ROOMS must be 1..5000")
    os.execv(sys.executable, [sys.executable, "-u", str(Path(__file__).with_name("relay.py")),
                             "--websocket", "--behind-proxy", "--bind", "0.0.0.0",
                             "--port", str(port), "--max-rooms", str(rooms)])


if __name__ == "__main__":
    main()

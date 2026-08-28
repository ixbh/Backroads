"""Windows desktop entry point for the portable Backroad beta."""

from __future__ import annotations

import logging
import os
import socket
import tempfile
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path


APP_NAME = "Backroad Beta"


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _configure_logging() -> Path:
    local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    log_path = None
    for log_dir in (
        local_app_data / APP_NAME,
        Path(tempfile.gettempdir()) / APP_NAME,
    ):
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "backroad.log"
            with log_path.open("a", encoding="utf-8"):
                pass
            break
        except OSError:
            continue
    if log_path is None:
        # The console still carries startup errors if both conventional
        # writable locations are unavailable.
        log_path = Path("backroad.log")
    logging.basicConfig(
        filename=log_path,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
    return log_path


def main() -> None:
    resource_dir = Path(__file__).resolve().parent
    database_path = resource_dir / "data" / "backroads.sqlite3"
    if not database_path.is_file():
        print(
            f"The routing database is missing:\n{database_path}\n\n"
            "Re-extract the complete beta download and try again."
        )
        input("Press Enter to close...")
        return

    log_path = _configure_logging()
    os.environ["BACKROADS_SQLITE_PATH"] = str(database_path)
    os.environ["BACKROADS_DESKTOP"] = "1"
    os.environ.pop("DATABASE_URL", None)

    import uvicorn
    from api import app

    host = "127.0.0.1"
    port = _available_port()
    url = f"http://{host}:{port}/"
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_config=None, access_log=False)
    )
    server_thread = threading.Thread(
        target=server.run, name="backroad-api", daemon=True
    )
    server_thread.start()
    print("Backroad Beta is starting...")
    ready = False
    for _attempt in range(60):
        try:
            with urllib.request.urlopen(f"{url}health", timeout=0.5) as response:
                if response.status == 200:
                    ready = True
                    break
        except Exception:
            pass
        if not server_thread.is_alive():
            break
        time.sleep(0.25)

    if not ready:
        print(f"The route engine did not start. Log: {log_path}")
        server.should_exit = True
        input("Press Enter to close...")
        return

    print(f"Backroad is ready at {url}")
    print("Keep this window open while planning routes.")
    webbrowser.open(url)
    try:
        input("Press Enter here when you want to stop Backroad...\n")
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        print("Stopping Backroad...")
        server.should_exit = True
        server_thread.join(timeout=5)


if __name__ == "__main__":
    main()

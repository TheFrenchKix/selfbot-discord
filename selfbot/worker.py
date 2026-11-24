"""Worker d'une instance: ``python -m selfbot.worker --instance <id>``.

Lancé par le contrôleur/manager. Le token arrive par ``DISCORD_TOKEN`` (jamais en argument).
Écrit un heartbeat (timestamp) toutes les ``HEARTBEAT_INTERVAL`` secondes et s'arrête
proprement sur SIGTERM / SIGINT / CTRL_BREAK.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

HEARTBEAT_INTERVAL = float(os.environ.get("HEARTBEAT_INTERVAL", "5"))
log = logging.getLogger("selfbot.worker")


class Heartbeat(threading.Thread):
    def __init__(self, path: Path, interval: float = HEARTBEAT_INTERVAL):
        super().__init__(daemon=True, name="heartbeat")
        self.path, self.interval = Path(path), interval
        self._stop_evt = threading.Event()

    def beat(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(str(time.time()), encoding="utf-8")
        os.replace(tmp, self.path)

    def run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self.beat()
            except OSError as exc:  # pragma: no cover
                log.warning("heartbeat: %s", exc)
            self._stop_evt.wait(self.interval)

    def stop(self) -> None:
        self._stop_evt.set()
        try:
            self.path.unlink()
        except OSError:
            pass


def _install_signal_handlers(request_stop) -> None:
    names = ["SIGTERM", "SIGINT"] + (["SIGBREAK"] if sys.platform == "win32" else [])
    for name in names:
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, lambda *_: request_stop())
            except (ValueError, OSError):  # pragma: no cover
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="selfbot.worker")
    parser.add_argument("--instance", required=True, help="identifiant de l'instance")
    args = parser.parse_args(argv)

    os.environ["INSTANCE_ID"] = args.instance
    os.environ.setdefault("DATA_DIR", ".")

    heartbeat = Heartbeat(Path("heartbeat"))
    heartbeat.start()

    # L'import charge la configuration (token depuis l'environnement).
    from . import bot

    def request_stop() -> None:
        loop = getattr(bot.client, "loop", None)
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(bot.client.close(), loop)
        else:
            heartbeat.stop()
            os._exit(0)

    _install_signal_handlers(request_stop)
    try:
        bot.run_bot()
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        heartbeat.stop()


if __name__ == "__main__":
    sys.exit(main())

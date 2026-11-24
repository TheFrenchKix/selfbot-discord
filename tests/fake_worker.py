"""Faux worker pour tester le manager (aucun accès réseau/Discord)."""
import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

from selfbot.worker import Heartbeat

ap = argparse.ArgumentParser()
ap.add_argument("--instance", required=True)
args = ap.parse_args()

Path("info.json").write_text(json.dumps({
    "argv": sys.argv, "has_token_env": bool(os.environ.get("DISCORD_TOKEN")),
    "token_env": os.environ.get("DISCORD_TOKEN"), "instance": os.environ.get("INSTANCE_ID"),
    "cwd": os.getcwd(), "marker": os.environ.get("MARKER"),
    "leaked_controller_token": os.environ.get("CONTROLLER_TOKEN"),
    "pid": os.getpid(),
}))

hb = Heartbeat(Path("heartbeat"), interval=0.2)
hb.start()

stop = {"v": False}


def _stop(*_):
    stop["v"] = True


for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
    if hasattr(signal, name):
        signal.signal(getattr(signal, name), _stop)

if os.environ.get("FAKE_EXIT"):
    time.sleep(0.3)
    hb.stop()
    sys.exit(int(os.environ["FAKE_EXIT"]))

if os.environ.get("FAKE_CRASH") == "1":
    time.sleep(0.4)
    hb.stop()
    sys.exit(3)

while not stop["v"]:
    time.sleep(0.05)
hb.stop()
Path("graceful.txt").write_text("ok")
sys.exit(0)

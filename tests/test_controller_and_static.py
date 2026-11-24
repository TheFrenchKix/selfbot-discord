import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from selfbot.controller import ControllerService
from selfbot.manager import InstanceManager, ManagerError
from selfbot.secrets import TokenStore, generate_master_key
from selfbot.storage.db import Database

from conftest import FAKE_TOKEN_A

ROOT = Path(__file__).resolve().parent.parent
OWNER, STRANGER = 111, 222


@pytest.fixture
def svc(tmp_path):
    m = InstanceManager(
        root=tmp_path / "i",
        store=TokenStore(tmp_path / "i" / "t.json", backend="fernet", key=generate_master_key()),
        db=Database(tmp_path / "m.db"),
        worker_cmd=[sys.executable, str(ROOT / "tests" / "fake_worker.py")],
        extra_env={"PYTHONPATH": str(ROOT)},
    )
    yield ControllerService(m, [OWNER])
    m.stop_all()


def test_non_owner_is_refused_everywhere(svc):
    for call in (
        lambda: svc.addtoken(STRANGER, "a", FAKE_TOKEN_A), lambda: svc.start(STRANGER, "a"),
        lambda: svc.stop(STRANGER, "a"), lambda: svc.restart(STRANGER, "a"),
        lambda: svc.remove(STRANGER, "a"), lambda: svc.status(STRANGER, "a"), lambda: svc.list(STRANGER),
    ):
        with pytest.raises(ManagerError, match="refusé"):
            call()
    assert svc.manager.store.ids() == []


def test_owner_flow_masks_token_and_audits(svc):
    out = svc.addtoken(OWNER, "a", FAKE_TOKEN_A)
    assert FAKE_TOKEN_A not in out and "AAA...CCC" in out
    assert "démarrée" in svc.start(OWNER, "a")
    listing = svc.list(OWNER)
    assert "AAA...CCC" in listing and FAKE_TOKEN_A not in listing
    assert "arrêtée" in svc.stop(OWNER, "a")
    assert "supprimée" in svc.remove(OWNER, "a")
    assert svc.list(OWNER) == "Aucune instance."
    rows = svc.manager.db.audit_log(20)
    assert all(r[1] == str(OWNER) for r in rows if r[2] in ("addtoken", "start", "stop", "remove"))
    assert FAKE_TOKEN_A not in json.dumps([list(r) for r in rows])


# --------------------------------------------------------------------------- #
# Vérifications statiques (équivalent du « git grep » de la checklist)
# --------------------------------------------------------------------------- #
def _sources():
    files = [ROOT / "main.py", ROOT / "litecoin.py", *(ROOT / "selfbot").rglob("*.py")]
    return [f for f in files if f.is_file()]


def test_compileall():
    r = subprocess.run([sys.executable, "-m", "compileall", "-q", str(ROOT / "selfbot"), str(ROOT / "main.py")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_no_ssl_false_no_eval_no_token_print():
    for f in _sources():
        text = f.read_text(encoding="utf-8")
        assert "ssl=False" not in text, f
        assert not re.search(r"\beval\(", text), f
        assert not re.search(r"print\([^)]*\b(token|TOKEN)\b", text), f


SECRET_PATTERNS = [
    re.compile(r"[\w-]{24,28}\.[\w-]{6,7}\.[\w-]{27,}"),     # token Discord
    re.compile(r"\bltc1[a-z0-9]{20,}\b"),                     # adresse LTC
    re.compile(r"://[^/\s:@]+:[^/\s@]+@\d"),                  # proxy user:pass@ip
    re.compile(r"\b[0-9a-f]{32}\b"),                          # clé API hex
]


def test_no_secrets_in_sources_and_configs():
    scanned = _sources() + [ROOT / ".env.example", ROOT / "config.example.ini", ROOT / "config.ini"]
    for f in scanned:
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8")
        for pat in SECRET_PATTERNS:
            assert not pat.search(text), f"secret potentiel dans {f}: {pat.pattern}"


def test_cold_start_without_secrets_fails_readably(tmp_path):
    pytest.importorskip("discord")  # le bot a besoin de discord.py-self
    env = {k: v for k, v in os.environ.items() if k not in ("DISCORD_TOKEN", "CONTROLLER_TOKEN", "OWNER_IDS")}
    env["PYTHONPATH"] = str(ROOT)
    r = subprocess.run([sys.executable, str(ROOT / "main.py")], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "DISCORD_TOKEN" in r.stderr and "Traceback" not in r.stderr


def test_controller_cold_start_fails_readably(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("CONTROLLER_TOKEN", "OWNER_IDS")}
    env["PYTHONPATH"] = str(ROOT)
    r = subprocess.run([sys.executable, "-m", "selfbot.controller"], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2
    assert "CONTROLLER_TOKEN" in r.stderr and "OWNER_IDS" in r.stderr and "Traceback" not in r.stderr

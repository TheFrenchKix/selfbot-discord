import json
import sys
import time
from pathlib import Path

import pytest

from selfbot.manager import InstanceManager, ManagerError, RateLimiter
from selfbot.secrets import TokenStore, generate_master_key
from selfbot.storage.db import Database

from conftest import FAKE_TOKEN_A, FAKE_TOKEN_B

FAKE_WORKER = Path(__file__).with_name("fake_worker.py")


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def mgr(tmp_path):
    key = generate_master_key()
    m = InstanceManager(
        root=tmp_path / "inst",
        store=TokenStore(tmp_path / "inst" / "tokens.json", backend="fernet", key=key),
        db=Database(tmp_path / "m.db"),
        worker_cmd=[sys.executable, str(FAKE_WORKER)],
        backoff_base=0.2, backoff_max=0.8, heartbeat_timeout=30,
        extra_env={"PYTHONPATH": str(Path(__file__).resolve().parent.parent)},
    )
    yield m
    m.stop_all()


def info(m, iid):
    p = m.instance_dir(iid) / "info.json"
    assert wait_for(p.exists), f"{iid} n'a pas démarré"
    time.sleep(0.1)
    return json.loads(p.read_text())


def test_add_token_validation_and_duplicates(mgr):
    assert mgr.add_token("a", FAKE_TOKEN_A) == "AAA...CCC"
    with pytest.raises(ManagerError, match="déjà"):
        mgr.add_token("a", FAKE_TOKEN_B)
    with pytest.raises(ManagerError, match="déjà utilisé"):
        mgr.add_token("b", FAKE_TOKEN_A)
    with pytest.raises(ManagerError, match="Format"):
        mgr.add_token("c", "not-a-token")
    with pytest.raises(ManagerError, match="Identifiant"):
        mgr.add_token("../evil", FAKE_TOKEN_B)
    assert FAKE_TOKEN_A not in (mgr.root / "tokens.json").read_text()


def test_token_via_env_never_argv_and_no_leak(mgr, monkeypatch):
    monkeypatch.setenv("CONTROLLER_TOKEN", "controller-secret-value")
    mgr.add_token("a", FAKE_TOKEN_A)
    mgr.start("a")
    d = info(mgr, "a")
    assert d["token_env"] == FAKE_TOKEN_A and d["instance"] == "a"
    assert FAKE_TOKEN_A not in " ".join(d["argv"])
    assert d["leaked_controller_token"] is None
    assert Path(d["cwd"]).samefile(mgr.instance_dir("a"))
    st = mgr.status("a")
    assert st["state"] == "running" and st["token"] == "AAA...CCC"
    assert FAKE_TOKEN_A not in json.dumps(mgr.list())


def test_graceful_stop(mgr):
    mgr.add_token("a", FAKE_TOKEN_A)
    mgr.start("a")
    info(mgr, "a")
    assert wait_for(lambda: mgr.heartbeat_age("a") is not None)
    assert mgr.stop("a") is True
    assert mgr.status("a")["state"] == "stopped"
    assert (mgr.instance_dir("a") / "graceful.txt").exists(), "le worker doit s'arrêter proprement"
    assert mgr.stop("a") is False


def test_two_instances_parallel_separate_configs(mgr):
    mgr.add_token("a", FAKE_TOKEN_A)
    mgr.add_token("b", FAKE_TOKEN_B)
    (mgr.instance_dir("a") / "instance.json").write_text(json.dumps({"env": {"MARKER": "alpha"}}))
    (mgr.instance_dir("b") / "instance.json").write_text(
        json.dumps({"env": {"MARKER": "beta", "DISCORD_TOKEN": "hack"}}))
    mgr.start("a")
    mgr.start("b")
    da, db_ = info(mgr, "a"), info(mgr, "b")
    assert (da["marker"], db_["marker"]) == ("alpha", "beta")
    assert da["token_env"] == FAKE_TOKEN_A and db_["token_env"] == FAKE_TOKEN_B  # override du token interdit
    assert da["pid"] != db_["pid"]
    assert Path(da["cwd"]) != Path(db_["cwd"])
    assert {s["state"] for s in mgr.list()} == {"running"}


def test_crash_isolated_and_restarted_with_backoff(mgr):
    mgr.add_token("bad", FAKE_TOKEN_A)
    mgr.add_token("good", FAKE_TOKEN_B)
    (mgr.instance_dir("bad") / "instance.json").write_text(json.dumps({"env": {"FAKE_CRASH": "1"}}))
    mgr.start("good")
    info(mgr, "good")
    good_pid = mgr.instances["good"].proc.pid
    mgr.start("bad")
    assert wait_for(lambda: mgr.instances["bad"].proc.poll() is not None)

    t0 = time.monotonic()
    assert mgr.supervise_once(now=t0) == []            # crash détecté, backoff armé
    assert mgr.instances["bad"].backoff == pytest.approx(0.2)
    assert mgr.supervise_once(now=t0 + 0.05) == []     # trop tôt
    assert wait_for(lambda: mgr.supervise_once() == ["bad"], timeout=5)  # relancée après backoff
    assert mgr.instances["bad"].restarts == 1
    # backoff exponentiel
    assert wait_for(lambda: mgr.instances["bad"].proc.poll() is not None)
    mgr.supervise_once()
    assert mgr.instances["bad"].backoff == pytest.approx(0.4)

    # l'instance saine n'a jamais été touchée
    assert mgr.status("good")["state"] == "running"
    assert mgr.instances["good"].proc.pid == good_pid and mgr.instances["good"].restarts == 0
    assert "crash" in [r[2] for r in mgr.db.audit_log(50)]


def test_stopped_instance_is_not_restarted(mgr):
    mgr.add_token("a", FAKE_TOKEN_A)
    mgr.start("a")
    info(mgr, "a")
    mgr.stop("a")
    assert mgr.supervise_once(now=time.monotonic() + 100) == []
    assert mgr.status("a")["state"] == "stopped"


def test_rate_limit_per_account(mgr):
    mgr.rate = RateLimiter(limit=2, window=60)
    mgr.add_token("a", FAKE_TOKEN_A)
    mgr.add_token("b", FAKE_TOKEN_B)
    mgr.start("a")
    mgr.stop("a")
    mgr.start("a")
    mgr.stop("a")
    with pytest.raises(ManagerError, match="Trop de"):
        mgr.start("a")
    mgr.start("b")  # autre compte: indépendant


def test_rate_limiter_window():
    now = [0.0]
    rl = RateLimiter(limit=2, window=10, clock=lambda: now[0])
    assert rl.allow("k") and rl.allow("k") and not rl.allow("k")
    now[0] = 11
    assert rl.allow("k")


def test_remove_and_unknown(mgr):
    mgr.add_token("a", FAKE_TOKEN_A)
    mgr.start("a")
    info(mgr, "a")
    mgr.remove("a")
    assert mgr.store.ids() == []
    with pytest.raises(ManagerError, match="inconnue"):
        mgr.start("a")
    with pytest.raises(ManagerError):
        mgr.status("zzz")


def test_restart(mgr):
    mgr.add_token("a", FAKE_TOKEN_A)
    pid1 = mgr.start("a")
    info(mgr, "a")
    pid2 = mgr.restart("a")
    assert pid1 != pid2 and mgr.status("a")["state"] == "running"
    assert {"addtoken", "start", "restart"} <= {r[2] for r in mgr.db.audit_log(50)}


def test_fatal_exit_code_is_not_restarted(mgr):
    mgr.add_token("a", FAKE_TOKEN_A)
    (mgr.instance_dir("a") / "instance.json").write_text(json.dumps({"env": {"FAKE_EXIT": "2"}}))
    mgr.start("a")
    assert wait_for(lambda: mgr.instances["a"].proc.poll() is not None)
    assert mgr.supervise_once(now=time.monotonic() + 100) == []
    assert mgr.status("a")["state"] == "stopped" and mgr.status("a")["last_exit"] == 2
    assert "fatal" in [r[2] for r in mgr.db.audit_log(20)]

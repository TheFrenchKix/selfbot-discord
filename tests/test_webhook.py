import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest
from aiohttp import web

from selfbot.controller import ControllerService
from selfbot.manager import InstanceManager, ManagerError
from selfbot.secrets import TokenStore, generate_master_key
from selfbot.storage.db import Database
from selfbot.webhook import WebhookError, WebhookNotifier, format_event, mask_url, post_webhook, validate_url

from conftest import FAKE_TOKEN_A, FAKE_TOKEN_B

ROOT = Path(__file__).resolve().parent.parent
URL = "https://discord.com/api/webhooks/123456789012345678/abcDEF_ghiJKL-mnoPQR0123456789xyz"
OWNER = 111


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


# --------------------------------------------------------------------------- #
# URL, stockage, formatage
# --------------------------------------------------------------------------- #
def test_validate_and_mask():
    assert validate_url(URL + "/") == URL
    assert validate_url(URL.replace("discord.com", "canary.discord.com"))
    tail = "/api/webhooks/123456789012345678/abcDEF_ghiJKL-mnoPQR0123456789xyz"
    for bad in ("http://discord.com" + tail, "https://evil.example" + tail,
                "https://discord.com.evil.io" + tail, "", "nope"):
        with pytest.raises(WebhookError):
            validate_url(bad)
    masked = mask_url(URL)
    assert "abcDEF_ghiJKL-mnoPQR0123456789xyz" not in masked and masked.endswith("abc...xyz")
    assert mask_url(None) == "<aucun>"


def test_store_is_encrypted_and_persistent(tmp_path):
    key = generate_master_key()
    n = WebhookNotifier(tmp_path / "w.json", backend="fernet", key=key)
    assert not n.configured
    n.set_url(URL)
    raw = (tmp_path / "w.json").read_text()
    assert "abcDEF" not in raw and "123456789012345678" not in raw
    assert WebhookNotifier(tmp_path / "w.json", backend="fernet", key=key).configured
    assert not WebhookNotifier(tmp_path / "w.json", backend="fernet", key=generate_master_key()).configured
    assert n.clear() and not n.configured and not (tmp_path / "w.json").exists()
    assert n.clear() is False


def test_format_event():
    p = format_event({"type": "crashed", "instance": "a", "ts": 1700000000, "code": 3, "retry_in": 4,
                      "uptime": 3725, "tail": "Traceback...\nValueError ```x```"})
    e = p["embeds"][0]
    assert "crashed" in e["title"] and e["color"] == 0xE74C3C
    names = {f["name"]: f["value"] for f in e["fields"]}
    assert names["Instance"] == "`a`" and names["Exit code"] == "`3`" and names["Retry in"] == "`4s`"
    assert names["Uptime"] == "`1h2m5s`"
    assert names["Last log lines"].count("```") == 2          # le contenu ne casse pas le bloc de code
    assert p["allowed_mentions"] == {"parse": []}
    assert "title" in format_event({"type": "unknown", "instance": "x"})["embeds"][0]


# --------------------------------------------------------------------------- #
# Notifier (sans réseau)
# --------------------------------------------------------------------------- #
def test_notifier_threadsafe_ordered_and_early_buffer(tmp_path):
    sent = []

    async def sender(url, payload):
        sent.append((url, payload["embeds"][0]["title"]))
        return True

    async def run():
        n = WebhookNotifier(tmp_path / "w.json", backend="fernet", key=generate_master_key(), sender=sender)
        n.notify({"type": "started", "instance": "x", "ts": 1})   # pas configuré: ignoré
        n.set_url(URL)
        n.notify({"type": "added", "instance": "early", "ts": 1})  # avant start(): mis en tampon
        n.start()
        t = threading.Thread(target=lambda: [n.notify({"type": "started", "instance": str(i), "ts": 1}) for i in range(3)])
        t.start()
        t.join()
        await n.flush(10)
        await n.close()

    asyncio.run(run())
    assert [t for _, t in sent] == ["➕ Instance added"] + ["▶️ Instance started"] * 3
    assert all(u == URL for u, _ in sent)


def test_notifier_survives_sender_errors(tmp_path):
    calls = []

    async def sender(url, payload):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return True

    async def run():
        n = WebhookNotifier(tmp_path / "w.json", backend="fernet", key=generate_master_key(), sender=sender)
        n.set_url(URL)
        n.start()
        n.notify({"type": "started", "instance": "a", "ts": 1})
        n.notify({"type": "stopped", "instance": "a", "ts": 1})
        await n.flush(10)
        await n.close()

    asyncio.run(run())
    assert len(calls) == 2


# --------------------------------------------------------------------------- #
# Envoi HTTP réel contre un serveur local (429 puis succès, 404)
# --------------------------------------------------------------------------- #
def test_post_webhook_retries_on_429_and_stops_on_404():
    state = {"n": 0, "bodies": []}

    async def handler(request):
        state["n"] += 1
        state["bodies"].append(await request.json())
        if request.path == "/gone":
            return web.Response(status=404)
        if state["n"] == 1:
            return web.json_response({"retry_after": 0.05}, status=429)
        return web.Response(status=204)

    async def run():
        import aiohttp
        app = web.Application()
        app.router.add_post("/hook", handler)
        app.router.add_post("/gone", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        async with aiohttp.ClientSession() as sess:
            ok = await post_webhook(sess, f"http://127.0.0.1:{port}/hook", {"content": "hi"})
            gone = await post_webhook(sess, f"http://127.0.0.1:{port}/gone", {"content": "hi"})
        await runner.cleanup()
        return ok, gone

    ok, gone = asyncio.run(run())
    assert ok is True and gone is False
    assert state["n"] == 3 and state["bodies"][0] == {"content": "hi"}


# --------------------------------------------------------------------------- #
# Événements du manager de bout en bout
# --------------------------------------------------------------------------- #
@pytest.fixture
def env(tmp_path):
    key = generate_master_key()
    events = []
    m = InstanceManager(
        root=tmp_path / "i",
        store=TokenStore(tmp_path / "i" / "t.json", backend="fernet", key=key),
        db=Database(tmp_path / "m.db"),
        worker_cmd=[sys.executable, str(ROOT / "tests" / "fake_worker.py")],
        backoff_base=0.2, backoff_max=0.8, heartbeat_timeout=30,
        extra_env={"PYTHONPATH": str(ROOT)},
    )
    m.add_listener(events.append)
    yield m, events, tmp_path, key
    m.stop_all()


def types(events):
    return [e["type"] for e in events]


def test_lifecycle_events(env):
    m, events, *_ = env
    m.add_token("a", FAKE_TOKEN_A, actor="42")
    m.start("a", actor="42")
    assert wait_for(lambda: (m.instance_dir("a") / "info.json").exists())
    m.restart("a", actor="42")
    m.stop("a", actor="42")
    m.remove("a", actor="42")
    assert types(events) == ["added", "started", "restarted", "stopped", "removed"]
    assert events[0]["actor"] == "42" and events[1]["pid"] and events[3]["code"] is not None
    assert all(e["instance"] == "a" for e in events)


def test_crash_events_with_redacted_tail_then_relaunch(env):
    m, events, *_ = env
    m.add_token("bad", FAKE_TOKEN_A)
    (m.instance_dir("bad") / "instance.json").write_text(json.dumps({"env": {"FAKE_EXIT": "3", "FAKE_PRINT": "1"}}))
    m.start("bad")
    assert wait_for(lambda: m.instances["bad"].proc.poll() is not None)
    m.supervise_once()
    crash = next(e for e in events if e["type"] == "crashed")
    assert crash["code"] == 3 and "boom: token=" in crash["tail"]
    assert FAKE_TOKEN_A not in crash["tail"] and ("***" in crash["tail"] or "[token]" in crash["tail"])
    assert wait_for(lambda: m.supervise_once() == ["bad"], timeout=5)
    assert types(events)[-1] == "relaunched" and events[-1]["restarts"] == 1


def test_fatal_event_and_listener_errors_are_isolated(env):
    m, events, *_ = env
    m.add_listener(lambda e: 1 / 0)            # listener défaillant
    m.add_token("a", FAKE_TOKEN_B)
    (m.instance_dir("a") / "instance.json").write_text(json.dumps({"env": {"FAKE_EXIT": "2"}}))
    m.start("a")
    assert wait_for(lambda: m.instances["a"].proc.poll() is not None)
    m.supervise_once(now=time.monotonic() + 100)
    assert "fatal" in types(events) and "crashed" not in types(events)
    assert next(e for e in events if e["type"] == "fatal")["code"] == 2


# --------------------------------------------------------------------------- #
# Service contrôleur: droits, validation, livraison
# --------------------------------------------------------------------------- #
def test_service_webhook_commands(env):
    m, events, tmp_path, key = env
    sent = []

    async def sender(url, payload):
        sent.append(payload)
        return True

    n = WebhookNotifier(tmp_path / "w.json", backend="fernet", key=key, sender=sender)
    svc = ControllerService(m, [OWNER], n)

    for call in (lambda: svc.setwebhook(222, URL), lambda: svc.clearwebhook(222), lambda: svc.webhook(222)):
        with pytest.raises(ManagerError, match="refusé"):
            call()
    with pytest.raises(ManagerError, match="URL invalide"):
        svc.setwebhook(OWNER, "https://evil.example/x")
    assert "Aucun webhook" in svc.webhook(OWNER)

    out = svc.setwebhook(OWNER, URL)
    assert "abcDEF_ghiJKL-mnoPQR0123456789xyz" not in out and "abc...xyz" in svc.webhook(OWNER)
    assert "abcDEF_ghiJKL" not in json.dumps([list(r) for r in m.db.audit_log(20)])

    async def flow():
        loop = asyncio.get_running_loop()
        n.start()
        assert "test envoyé" in await svc.testwebhook(OWNER)
        svc.addtoken(OWNER, "a", FAKE_TOKEN_A)
        svc.start(OWNER, "a")
        assert await loop.run_in_executor(None, wait_for, lambda: (m.instance_dir("a") / "info.json").exists())
        await loop.run_in_executor(None, svc.stop, OWNER, "a")
        await n.flush(10)
        await n.close()

    asyncio.run(flow())
    titles = [p["embeds"][0]["title"] for p in sent]
    assert titles == ["🧪 Webhook test", "➕ Instance added", "▶️ Instance started", "⏹️ Instance stopped"]
    assert "By" in {f["name"] for f in sent[2]["embeds"][0]["fields"]}

    assert "supprimé" in svc.clearwebhook(OWNER)
    with pytest.raises(ManagerError, match="Aucun webhook"):
        asyncio.run(svc.testwebhook(OWNER))


def test_service_without_notifier(env):
    m, *_ = env
    with pytest.raises(ManagerError, match="non disponible"):
        ControllerService(m, [OWNER]).webhook(OWNER)

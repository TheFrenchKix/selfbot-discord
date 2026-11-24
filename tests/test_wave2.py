import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from selfbot import shop, toolkit
from selfbot.manager import InstanceManager, ManagerError
from selfbot.secrets import TokenStore, generate_master_key
from selfbot.shopwatch import compare_items, diff_watch
from selfbot.storage.db import Database
from selfbot.tools import ToolError

from conftest import FAKE_TOKEN_A, FAKE_TOKEN_B

ROOT = Path(__file__).resolve().parent.parent
LTC = "LdP8Qox1VAhCzLJNqrr74YovaWYyNBUWvL"


# --------------------------------------------------------------------------- #
# Logique pure
# --------------------------------------------------------------------------- #
def test_alias_expansion():
    al = {"p": "price btc", "b": "bal"}
    assert toolkit.expand_alias(".p", ".", al) == ".price btc"
    assert toolkit.expand_alias(".b abc", ".", al) == ".bal abc"
    assert toolkit.expand_alias(".x", ".", al) == ".x" and toolkit.expand_alias("hello", ".", al) == "hello"
    assert toolkit.expand_alias(".p", ".", {"p": "p"}) == ".p"            # pas de récursion
    assert toolkit.check_name("My-Name") == "my-name"
    for bad in ("", "a b", "x" * 30, "é"):
        with pytest.raises(ToolError):
            toolkit.check_name(bad)


class DB:
    def __init__(self):
        self.d, self.audits = {}, []

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v

    def audit(self, *a):
        self.audits.append(a)


def test_stats():
    db = DB()
    s = toolkit.Stats(lambda: db)
    assert s.summary() == {}
    s.record("snipes", 5, 0.5, now=10)
    s.record("snipes", 5, 1.5, now=11)
    s.record("snipes", 6, None, now=12)
    s.record("autobuys", 5)
    s.record("snipes", 7, 99999)                           # latence aberrante ignorée
    sm = s.summary()
    assert sm["snipes"]["count"] == 4 and sm["snipes"]["top"][0] == (5, 2) and sm["snipes"]["avg_latency"] == 1.0
    assert sm["autobuys"]["count"] == 1 and sm["autobuys"]["avg_latency"] is None
    s.reset()
    assert s.summary() == {}
    toolkit.Stats(lambda: None).record("x", 1)               # pas de base: sans effet


def test_crypto_helpers():
    assert toolkit.validate_ltc_address(LTC) == LTC and toolkit.validate_ltc_address("ltc1q" + "a" * 38)
    for bad in ("", "bc1qxyz", "L123", LTC + "!"):
        with pytest.raises(ToolError):
            toolkit.validate_ltc_address(bad)
    assert round(toolkit.estimate_fee(10000), 8) == 0.00002250
    assert toolkit.coin_id("BTC") == "bitcoin" and toolkit.coin_id("pepe") == "pepe"
    with pytest.raises(ToolError):
        toolkit.coin_id("a b!")
    assert toolkit.parse_alert(["btc", "above", "60000"]) == ("bitcoin", "above", 60000.0)
    assert toolkit.parse_alert(["ltc", "<", "30,5"]) == ("litecoin", "below", 30.5)
    for bad in (["btc"], ["btc", "up", "1"], ["btc", "above", "x"], ["btc", "above", "-1"]):
        with pytest.raises(ToolError):
            toolkit.parse_alert(bad)
    alerts = [{"id": 1, "coin": "bitcoin", "dir": "above", "value": 60000}, {"id": 2, "coin": "litecoin", "dir": "below", "value": 30}]
    hits = toolkit.triggered_alerts(alerts, {"bitcoin": 61000, "litecoin": 35})
    assert [h["id"] for h in hits] == [1] and hits[0]["price"] == 61000
    assert toolkit.triggered_alerts(alerts, {}) == []


def test_misc_parsers():
    assert toolkit.check_lang("fr") == "fr" and toolkit.check_lang("zh-CN")
    with pytest.raises(ToolError):
        toolkit.check_lang("french!")
    assert toolkit.parse_translation([[["Bonjour ", "hello ", None], ["monde", "world", None]], None, "en"]) == ("Bonjour monde", "en")
    with pytest.raises(ToolError):
        toolkit.parse_translation(None)
    assert toolkit.parse_gift_code("https://discord.gift/AbCdEfGhIjKlMnOp") == "AbCdEfGhIjKlMnOp"
    with pytest.raises(ToolError):
        toolkit.parse_gift_code("short")
    assert toolkit.parse_poll("Q? | a | b") == ("Q?", ["a", "b"])
    for bad in ("Q | a", "Q | " + " | ".join("x" * 10 for _ in range(10))):
        with pytest.raises(ToolError):
            toolkit.parse_poll(bad)
    assert toolkit.valid_timezone("Europe/Paris")
    with pytest.raises(ToolError):
        toolkit.valid_timezone("Nope/Zone")
    assert toolkit.check_emoji("🔥") and toolkit.check_emoji("<:x_1:123456789012345678>")
    with pytest.raises(ToolError):
        toolkit.check_emoji("abc")


def test_validate_config():
    good = {"version": 1, "stock_channels": [1, 2, 2], "keywords": ["  Nitro "], "max_price": 5, "auto_mode": "dm", "snipe_delay": 1.5}
    c = toolkit.validate_config(good)
    assert c["stock_channels"] == [1, 2] and c["keywords"] == ["nitro"] and c["snipe_channels"] == [] and c["auto_mode"] == "dm"
    for bad in (None, {"version": 2}, {**good, "stock_channels": ["a"]}, {**good, "stock_channels": [True]},
                {**good, "keywords": [1]}, {**good, "max_price": "5"}, {**good, "auto_mode": "x"},
                {**good, "snipe_delay": -1}, {**good, "keywords": ["x" * 200]}):
        with pytest.raises(ToolError):
            toolkit.validate_config(bad)


def item(i, name, price, nitro=None):
    return shop.Item(id=i, name=name, kind="deco", price=price, nitro_price=nitro)


def test_shop_diff_and_compare():
    seen = {"1": 10.0, "2": 5.0}
    new, drops, state = diff_watch(seen, [item("1", "a", 8.0), item("2", "b", 5.0), item("3", "c", 3.0)])
    assert [i.id for i in new] == ["3"] and [(i.id, old) for i, old in drops] == [("1", 10.0)]
    assert state == {"1": 8.0, "2": 5.0, "3": 3.0}
    assert diff_watch({}, [item("1", "a", 1.0)])[0][0].id == "1"
    rows = compare_items(item("1", "A", 8.0, 6.0), item("2", "B", 5.0, 7.0))
    assert "cheaper: B" in rows[0] and "cheaper: A" in rows[1]


# --------------------------------------------------------------------------- #
# Cog avec faux contexte
# --------------------------------------------------------------------------- #
discord = pytest.importorskip("discord")


class FakeMsg:
    def __init__(self):
        self.edits, self.reactions, self.reference, self.deleted = [], [], None, False

    async def edit(self, content=None, **kw):
        self.edits.append(content)

    async def delete(self):
        self.deleted = True

    async def add_reaction(self, e):
        self.reactions.append(e)


class FakeCtx:
    def __init__(self, guild=None):
        self.message, self.sent, self.guild = FakeMsg(), [], guild
        self.channel = SimpleNamespace(id=77)
        self.author = SimpleNamespace(id=1)

    async def send(self, content=None, **kw):
        self.sent.append(content)


@pytest.fixture
def cog(tmp_path):
    from discord.ext import commands

    from selfbot import automation, extras
    bot = commands.Bot(command_prefix=".", self_bot=True)
    bot._connection.user = SimpleNamespace(id=1)
    db = DB()
    flags = {"stockSniper": True, "autoBuy": True, "autoRedeem": False}
    imported = []

    async def ltc_bal(addr):
        return {"final_balance": 200_000_000}

    async def usdt_bal(addr):
        return {"balance": 10}

    async def txs(addr):
        return txs.data

    txs.data = [{"hash": "h2", "amount": 1.5, "time": 1}, {"hash": "h1", "amount": 1, "time": 0}]
    bridge = automation.Bridge(
        token=lambda: "TOK", prefix=".", data_dir=tmp_path, get_flags=lambda: dict(flags), set_flags=lambda **k: flags.update(k),
        export_config=lambda: {"stock_channels": [1], "snipe_channels": [], "ticket_channels": [2], "keywords": ["nitro"],
                               "exclude_keywords": [], "labels_check": [], "max_price": 5.0, "auto_mode": "dm", "snipe_delay": None},
        import_config=imported.append, stats=toolkit.Stats(lambda: db), ltc_address=lambda: LTC, usdt_address=lambda: "0xabc",
        get_ltc_balance=ltc_bal, get_usdt_balance=usdt_bal, last_transactions=txs)

    async def build():
        await bot.add_cog(extras.Extras(bot, lambda: db, "."))
        c = automation.Automation(bot, bridge, lambda: db)
        await bot.add_cog(c)
        return c

    c = asyncio.run(build())
    c.db, c.flags, c.imported, c.txs, c.sent = db, flags, imported, txs, []

    async def notify(channel_id, text):
        c.sent.append((channel_id, text))

    c._notify = notify
    return c


def run(cog, name, ctx, *args, **kw):
    cmd = cog.bot.get_command(name)
    return asyncio.run(cmd.callback(cog, ctx, *args, **kw))


def last(ctx):
    return ctx.message.edits[-1]


def test_no_command_conflicts_and_expected_commands(cog):
    names = {c.qualified_name for c in cog.bot.walk_commands()}
    for n in ("pause", "stats", "export", "import", "gift", "price", "fees", "watch", "portfolio", "alias", "snippet", "react",
              "autoreact", "translate", "tz", "poll", "copyid", "jump", "pins", "purgeuser", "price alert", "watch list", "tz set"):
        assert n in names, n


def test_pause_and_resume(cog):
    ctx = FakeCtx()
    run(cog, "pause", ctx, "10m")
    assert cog.flags == {"stockSniper": False, "autoBuy": False, "autoRedeem": False} and cog.pause_until
    run(cog, "pause", ctx, "5m")                            # 2e pause: ne perd pas l'état d'origine
    run(cog, "pause", ctx, "status")
    assert "Paused" in last(ctx)
    asyncio.run(cog.tick(now=time.time() + 4000))
    assert cog.flags == {"stockSniper": True, "autoBuy": True, "autoRedeem": False} and cog.pause_until is None
    run(cog, "pause", ctx, "off")
    assert "Not paused" in last(ctx)
    run(cog, "pause", ctx, "bad")
    assert "Durée invalide" in last(ctx)
    run(cog, "pause", ctx, "1h")
    run(cog, "pause", ctx, "off")
    assert cog.flags["stockSniper"] is True and "Resumed" in last(ctx)


def test_stats_command(cog):
    ctx = FakeCtx()
    run(cog, "stats", ctx)
    assert "No statistics" in last(ctx)
    cog.bridge.stats.record("snipes", 5, 0.4)
    run(cog, "stats", ctx)
    assert "Snipes" in last(ctx) and "<#5>" in last(ctx)
    run(cog, "stats", ctx, "reset")
    assert "cleared" in last(ctx)


def test_export_import_roundtrip_and_validation(cog, tmp_path):
    ctx = FakeCtx()
    run(cog, "export", ctx)
    files = list((tmp_path / "exports").glob("config-*.json"))
    assert len(files) == 1 and "TOK" not in files[0].read_text()
    data = json.loads(files[0].read_text())
    assert data["version"] == 1 and data["keywords"] == ["nitro"]
    run(cog, "import", ctx)
    assert files[0].name in last(ctx)
    run(cog, "import", ctx, files[0].name)
    assert "imported" in last(ctx) and cog.imported[0]["stock_channels"] == [1]
    run(cog, "import", ctx, "../../etc/passwd")
    assert "Invalid file name" in last(ctx)
    run(cog, "import", ctx, "missing.json")
    assert "not found" in last(ctx)
    (tmp_path / "exports" / "bad.json").write_text('{"version": 9}')
    run(cog, "import", ctx, "bad.json")
    assert "Import refused" in last(ctx) and len(cog.imported) == 1


def test_price_alert_flow(cog):
    ctx = FakeCtx()
    run(cog, "price alert", ctx, "btc", "above", "60000")
    assert cog.db.d["price_alerts"][0]["coin"] == "bitcoin"
    run(cog, "price alert", ctx, "btc", "wat", "1")
    assert "Format" in last(ctx)
    run(cog, "price alerts", ctx)
    assert "bitcoin" in last(ctx)

    async def prices(coins, *cur):
        return {"bitcoin": {"eur": 61000}}

    cog._prices = prices
    asyncio.run(cog._check_alerts())
    assert cog.db.d["price_alerts"] == [] and "61000" in cog.sent[0][1] and cog.sent[0][0] == 77
    run(cog, "price del", ctx, 5)
    assert "No alert" in last(ctx)


def test_watch_address_flow(cog):
    ctx = FakeCtx()
    run(cog, "watch", ctx, "bad")
    assert "invalide" in last(ctx)
    run(cog, "watch", ctx, LTC)
    assert cog.db.d["tx_watch"][0]["last"] == "h2"
    run(cog, "watch", ctx, LTC)
    assert "déjà" in last(ctx)
    asyncio.run(cog._check_watches())
    assert cog.sent == []                                   # rien de nouveau
    cog.txs.data = [{"hash": "h3", "amount": 2, "time": 3}] + cog.txs.data
    asyncio.run(cog._check_watches())
    assert len(cog.sent) == 1 and "2 LTC" in cog.sent[0][1] and cog.db.d["tx_watch"][0]["last"] == "h3"
    run(cog, "watch list", ctx)
    assert LTC in last(ctx)
    run(cog, "watch del", ctx, 1)
    assert cog.db.d["tx_watch"] == []


def test_portfolio_and_fees(cog):
    ctx = FakeCtx()

    async def prices(coins, *cur):
        return {"litecoin": {"eur": 50.0}, "tether": {"eur": 0.9}}

    cog._prices = prices
    run(cog, "portfolio", ctx)
    assert "100.00€" in last(ctx) and "9.00€" in last(ctx) and "109.00€" in last(ctx)

    async def http(url, headers=None, **p):
        return {"low_fee_per_kb": 1000, "medium_fee_per_kb": 2000, "high_fee_per_kb": 4000}

    cog._http_json = http
    run(cog, "fees", ctx)
    assert "Low" in last(ctx) and "0.00000225 LTC" in last(ctx)


def test_gift_info(cog):
    ctx = FakeCtx()
    seen = {}

    async def http(url, headers=None, **p):
        seen.update(url=url, headers=headers)
        return {"uses": 0, "max_uses": 1, "redeemed": False, "subscription_plan": {"name": "Nitro Month"}, "expires_at": "2027-01-01T00:00:00"}

    cog._http_json = http
    run(cog, "gift", ctx, "info", code="https://discord.gift/AbCdEfGhIjKlMnOp")
    assert "Nitro Month" in last(ctx) and "Valid" in last(ctx) and seen["url"].endswith("AbCdEfGhIjKlMnOp")
    assert seen["headers"] == {"authorization": "TOK"}

    async def gone(url, headers=None, **p):
        raise ToolError("Service indisponible (HTTP 404)")

    cog._http_json = gone
    run(cog, "gift", ctx, "AbCdEfGhIjKlMnOp")
    assert "Unknown or expired" in last(ctx)
    run(cog, "gift", ctx, "info", code="x")
    assert "introuvable" in last(ctx)


def test_alias_and_snippet(cog):
    ctx = FakeCtx()
    run(cog, "alias", ctx, "add", "pp", command=".ping")
    assert cog.expand(".pp now") == ".ping now"
    run(cog, "alias", ctx, "add", "ping", command="uptime")
    assert "déjà une commande" in last(ctx)
    run(cog, "alias", ctx, "add", "zz", command="nonexistent")
    assert "commande existante" in last(ctx)
    run(cog, "alias", ctx, "list")
    assert "pp" in last(ctx)
    run(cog, "alias", ctx, "del", "pp")
    assert cog.expand(".pp") == ".pp"

    run(cog, "snippet", ctx, "add", text="thx Thanks for the deal!")
    run(cog, "snippet", ctx, "thx")
    assert last(ctx) == "Thanks for the deal!"
    run(cog, "snippet", ctx, "add", text="list x")
    assert "réservé" in last(ctx)
    run(cog, "snippet", ctx, "nope")
    assert "introuvable" in last(ctx)
    run(cog, "snippet", ctx, "list")
    assert "thx" in last(ctx)
    run(cog, "snippet", ctx, "del", text="thx")
    assert cog.db.d["snippets"] == {}


def test_autoreact_rules_and_listener(cog):
    ctx = FakeCtx()
    run(cog, "autoreact add", ctx, "🔥", "user", "222222222222222222")
    run(cog, "autoreact add", ctx, "bad", "user", "1")
    assert "Emoji invalide" in last(ctx)
    run(cog, "autoreact add", ctx, "👀", "thing", "222222222222222222")
    assert "user" in last(ctx)

    reacted = []

    class Msg:
        def __init__(self, uid, cid=5):
            self.author, self.channel = SimpleNamespace(id=uid), SimpleNamespace(id=cid)

        async def add_reaction(self, e):
            reacted.append(e)

    asyncio.run(cog.on_message(Msg(222222222222222222)))
    asyncio.run(cog.on_message(Msg(222222222222222222)))      # 2 s de délai par règle
    asyncio.run(cog.on_message(Msg(333)))
    asyncio.run(cog.on_message(Msg(1)))                       # mes propres messages ignorés
    assert reacted == ["🔥"]
    run(cog, "autoreact off", ctx)
    cog._rule_hits.clear()
    asyncio.run(cog.on_message(Msg(222222222222222222)))
    assert reacted == ["🔥"]
    run(cog, "autoreact del", ctx, 1)
    assert cog.db.d["autoreacts"] == []


def test_poll_tz_translate_and_message_tools(cog):
    ctx = FakeCtx()
    run(cog, "poll", ctx, text="Pizza? | Yes | No")
    assert "1️⃣" in last(ctx) and ctx.message.reactions == ["1️⃣", "2️⃣"]
    run(cog, "poll", ctx, text="Only")
    assert "Format" in last(ctx)

    run(cog, "tz set", ctx, "me", "Europe/Paris")
    run(cog, "tz", ctx)
    assert "Europe/Paris" in last(ctx)
    run(cog, "tz set", ctx, "me", "Mars/Base")
    assert "Fuseau inconnu" in last(ctx)
    run(cog, "tz list", ctx)
    assert "Europe/Paris" in last(ctx)
    run(cog, "tz del", ctx, "me")
    run(cog, "tz", ctx)
    assert "No timezone" in last(ctx)

    async def http(url, headers=None, **p):
        assert p["tl"] == "fr" and p["q"] == "hello"
        return [[["Bonjour", "hello", None]], None, "en"]

    cog._http_json = http
    run(cog, "translate", ctx, "fr", text="hello")
    assert "en → fr" in last(ctx) and "Bonjour" in last(ctx)
    run(cog, "translate", ctx, "french!", text="x")
    assert "Code langue" in last(ctx)

    run(cog, "jump", ctx, "123456789012345678")
    assert "channels/@me/77/123456789012345678" in last(ctx)
    run(cog, "copyid", ctx)
    assert "77" in last(ctx)


def test_purgeuser(cog):
    ctx = FakeCtx(SimpleNamespace(id=9))

    async def purge(limit=None, check=None):
        assert limit == 200 and check(SimpleNamespace(author=SimpleNamespace(id=222222222222222222)))
        return [1, 2, 3]

    ctx.channel.purge = purge
    run(cog, "purgeuser", ctx, "222222222222222222", 9999)
    assert "Deleted 3" in last(ctx) and cog.db.audits[-1][1] == "purgeuser"

    async def denied(limit=None, check=None):
        raise discord.Forbidden(SimpleNamespace(status=403, reason="no"), "nope")

    ctx.channel.purge = denied
    run(cog, "purgeuser", ctx, "222222222222222222")
    assert "Missing permission" in last(ctx)


def test_shop_watch_and_compare(cog, monkeypatch):
    ctx = FakeCtx()
    catalog = [item("1", "Venom", 8.99, 6.99), item("2", "Moon", 5.99)]

    async def fetch(query):
        return list(catalog)

    cog._fetch_shop = fetch
    assert asyncio.run(cog.shop_extra(ctx, "deco")) is False       # pas une sous-commande
    assert asyncio.run(cog.shop_extra(ctx, "watch deco max=9")) is True
    assert "Shop watch #1" in last(ctx) and set(cog.db.d["shop_watches"][0]["seen"]) == {"1", "2"}
    asyncio.run(cog._check_shop())
    assert cog.sent == []
    catalog.append(item("3", "New", 1.0))
    catalog[0] = item("1", "Venom", 4.0, 3.0)
    asyncio.run(cog._check_shop())
    assert len(cog.sent) == 1 and "New" in cog.sent[0][1] and "was" in cog.sent[0][1]
    asyncio.run(cog.shop_extra(ctx, "watches"))
    assert "deco max=9" in last(ctx)
    asyncio.run(cog.shop_extra(ctx, "compare venom | moon"))
    assert "Venom vs Moon" in last(ctx) and "cheaper" in last(ctx)
    asyncio.run(cog.shop_extra(ctx, "compare only"))
    assert "Format" in last(ctx)
    asyncio.run(cog.shop_extra(ctx, "unwatch 1"))
    assert cog.db.d["shop_watches"] == []


def test_wiring_in_selfbot(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DISCORD_TOKEN", "AAAAAAAAAAAAAAAAAAAAAAAA.BBBBBB.CCCCCCCCCCCCCCCCCCCCCCCCCCC")
    import importlib

    from selfbot import bot as b
    b = importlib.reload(b)
    asyncio.run(b.client.setup_hook())                     # aucun conflit de nom avec les 150+ commandes existantes
    cog = b.client.get_cog("Automation")
    assert cog is not None and b.client.get_command("pause") and b.client.get_command("shop")

    # alias: expansion des propres messages uniquement
    cog._save("aliases", {"pp": "ping"})
    b.client._connection.user = SimpleNamespace(id=1)
    seen = []

    async def fake_super(self, message):
        seen.append(message.content)

    monkeypatch.setattr(b.commands.Bot, "process_commands", fake_super)
    asyncio.run(b.client.process_commands(SimpleNamespace(content=".pp", author=SimpleNamespace(id=1))))
    asyncio.run(b.client.process_commands(SimpleNamespace(content=".pp", author=SimpleNamespace(id=2))))
    assert seen == [".ping", ".pp"]

    # pause <-> flags réels du module
    b._set_flags(stockSniper=False)
    assert b._get_flags()["stockSniper"] is False
    b._set_flags(stockSniper=True)

    # export / import: aller-retour sans secret
    b.keywords.update({"nitro"})
    b.snipeChannels.update({42})
    exp = b._export_config()
    assert exp["snipe_channels"] == [42] and "nitro" in exp["keywords"]
    assert "token" not in json.dumps(exp).lower() and b.TOKEN not in json.dumps(exp)
    clean = toolkit.validate_config({"version": 1, **exp})
    b.keywords.clear()
    b.snipeChannels.clear()
    b._import_config(clean)
    assert "nitro" in b.keywords and 42 in b.snipeChannels and "nitro" in b._db().get("keywords")

    # stats branchées sur le sniper
    b.STATS.record("snipes", 1, 0.1)
    assert b.STATS.summary()["snipes"]["count"] == 1
    assert "pause" in b.build_help("automation")
    if b.STATE.db is not None:
        b.STATE.db.close()
        b.STATE.db = None


# --------------------------------------------------------------------------- #
# Contrôleur: logs, uptime, restartall, stopall, rename
# --------------------------------------------------------------------------- #
@pytest.fixture
def mgr(tmp_path):
    key = generate_master_key()
    events = []
    m = InstanceManager(
        root=tmp_path / "i", store=TokenStore(tmp_path / "i" / "t.json", backend="fernet", key=key),
        db=Database(tmp_path / "m.db"), worker_cmd=[sys.executable, str(ROOT / "tests" / "fake_worker.py")],
        backoff_base=0.2, backoff_max=0.8, heartbeat_timeout=30, extra_env={"PYTHONPATH": str(ROOT)})
    m.add_listener(events.append)
    yield m, events
    m.stop_all()


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_controller_new_commands(mgr):
    from selfbot.controller import ControllerService
    m, events = mgr
    svc = ControllerService(m, [111])
    for call in (lambda: svc.logs(2, "a"), lambda: svc.uptime(2), lambda: svc.stopall(2), lambda: svc.restartall(2),
                 lambda: svc.rename(2, "a", "b")):
        with pytest.raises(ManagerError, match="refusé"):
            call()
    svc.addtoken(111, "a", FAKE_TOKEN_A)
    svc.addtoken(111, "b", FAKE_TOKEN_B)
    assert "Aucun log" in svc.logs(111, "a")
    with pytest.raises(ManagerError, match="inconnue"):
        svc.logs(111, "zzz")

    svc.start(111, "a")
    assert wait_for(lambda: (m.instance_dir("a") / "info.json").exists())
    out = svc.uptime(111)
    assert "Contrôleur" in out and "`a` · running" in out and "`b` · stopped" in out

    log = m.log_path("a")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("line1\nline ``` with " + FAKE_TOKEN_A + "\nline3\n")
    m_out = svc.logs(111, "a", 2)
    assert "line3" in m_out and "line1" not in m_out and FAKE_TOKEN_A not in m_out and m_out.count("```") == 2

    with pytest.raises(ManagerError, match="Arrêtez"):
        svc.rename(111, "a", "c")
    with pytest.raises(ManagerError, match="existe déjà"):
        svc.rename(111, "b", "a")
    with pytest.raises(ManagerError, match="invalide"):
        svc.rename(111, "b", "bad name!")
    assert "renommée" in svc.rename(111, "b", "c")
    assert m.store.ids() == ["a", "c"] and m.store.get("c") == FAKE_TOKEN_B and m.instance_dir("c").exists()
    assert not m.instance_dir("b").exists()
    ev = [e for e in events if e["type"] == "renamed"][0]
    assert ev["instance"] == "c" and ev["old"] == "b"

    svc.start(111, "c")
    assert wait_for(lambda: (m.instance_dir("c") / "info.json").exists())
    pid_a = m.instances["a"].proc.pid
    assert "2 instance(s) redémarrée(s)" in svc.restartall(111)
    assert m.instances["a"].proc.pid != pid_a and m.instances["a"].running and m.instances["c"].running
    assert "2 instance(s) arrêtée(s)" in svc.stopall(111)
    assert not m.instances["a"].running and not m.instances["c"].running
    assert "0 instance(s) arrêtée(s)" in svc.stopall(111)
    assert "0 instance(s) redémarrée(s)" in svc.restartall(111)


def test_rename_event_formatting():
    from selfbot.webhook import format_event
    e = format_event({"type": "renamed", "instance": "new", "old": "old", "ts": 1, "actor": "42"})["embeds"][0]
    names = {f["name"]: f["value"] for f in e["fields"]}
    assert "renamed" in e["title"] and names["Previous name"] == "`old`"

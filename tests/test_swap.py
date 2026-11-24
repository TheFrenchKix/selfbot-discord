import asyncio
import hashlib
import hmac
import json
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web

from selfbot import swap as sw
from selfbot.config import ConfigError, load_settings
from selfbot.tools import ToolError

LTC = "LdP8Qox1VAhCzLJNqrr74YovaWYyNBUWvL"
EVM = "0x" + "ab" * 20
EVM2 = "0x" + "cd" * 20
LTC2 = "MPQ8Qox1VAhCzLJNqrr74YovaWYyNBUWvL"
PRICES = {"ltc": 40.0, "usdt": 1.0}


# --------------------------------------------------------------------------- #
# Logique pure
# --------------------------------------------------------------------------- #
def test_addresses_and_direction():
    assert sw.validate_evm_address(EVM) == EVM
    for bad in ("", "0x123", "0x" + "zz" * 20, LTC):
        with pytest.raises(ToolError):
            sw.validate_evm_address(bad)
    src, dst = sw.direction("LTC")
    assert (src.key, dst.key) == ("ltc", "usdt") and sw.direction("usdt")[1].key == "ltc"
    with pytest.raises(ToolError):
        sw.direction("btc")
    assert sw.COINS["ltc"].validate(LTC) and sw.COINS["usdt"].validate(EVM)
    with pytest.raises(ToolError):
        sw.COINS["ltc"].validate(EVM)


def test_spread_best_and_fee_guard():
    ltc, usdt = sw.COINS["ltc"], sw.COINS["usdt"]
    assert sw.market_out(ltc, usdt, 2, PRICES) == 80 and sw.market_out(usdt, ltc, 40, PRICES) == 1
    q1 = sw.with_spread(sw.Quote("a", 1, 39.0, True), ltc, usdt, PRICES)          # 2.5 % sous le marché
    q2 = sw.with_spread(sw.Quote("b", 1, 39.6, True), ltc, usdt, PRICES)          # 1 %
    q3 = sw.with_spread(sw.Quote("c", 1, 39.9, False), ltc, usdt, PRICES)         # meilleur mais non exécutable
    q4 = sw.Quote("d", 1, None, True)
    assert q1.spread == 2.5 and q2.spread == 1.0
    assert sw.pick_best([q1, q2, q3, q4]) is q2 and sw.pick_best([q3, q4]) is None
    sw.check_fee(q2, 3)
    with pytest.raises(sw.SwapError, match="au-delà"):
        sw.check_fee(q1, 2)
    with pytest.raises(sw.SwapError, match="inconnu"):
        sw.check_fee(q4, 3)
    usdt_q = sw.with_spread(sw.Quote("x", 40, 0.99, True), usdt, ltc, PRICES)     # 40 USDT -> 0.99 LTC (idéal 1)
    assert usdt_q.spread == 1.0


def test_check_order_blocks_bad_deposit():
    ltc = sw.COINS["ltc"]
    sw.check_order(sw.Order("p", "1", LTC2, 0.5), ltc, 0.5)
    with pytest.raises(sw.SwapError, match="adresse de dépôt"):
        sw.check_order(sw.Order("p", "1", "<script>", 0.5), ltc, 0.5)
    with pytest.raises(sw.SwapError, match="adresse de dépôt"):
        sw.check_order(sw.Order("p", "1", EVM, 0.5), ltc, 0.5)                    # mauvaise chaîne
    with pytest.raises(sw.SwapError, match="incohérent"):
        sw.check_order(sw.Order("p", "1", LTC2, 0.6), ltc, 0.5)


def test_trailing_trigger():
    sell = sw.TrailingTrigger("sell", 45)
    assert [sell.update(p) for p in (40, 44, 45, 47, 48, 47.9)] == [False, False, False, False, False, True]
    buy = sw.TrailingTrigger("buy", 35)
    assert [buy.update(p) for p in (40, 36, 34, 33, 33.5)] == [False, False, False, False, True]
    flat = sw.TrailingTrigger("sell", 10)
    assert [flat.update(p) for p in (11, 11, 11)] == [False, False, False]         # égal: pas de déclenchement
    for bad in (("x", 1), ("sell", 0)):
        with pytest.raises(ToolError):
            sw.TrailingTrigger(*bad)


def test_config_swap_settings():
    s = load_settings(env={"DISCORD_TOKEN": "x", "SWAPZONE_API_KEY": "k", "FIXEDFLOAT_API_KEY": "a", "FIXEDFLOAT_API_SECRET": "b",
                           "SWAP_PROVIDER": "FixedFloat", "SWAP_MAX_FEE_PCT": "1.5"}, ini_path="none.ini")
    assert (s.api.swapzone, s.api.fixedfloat_key, s.api.fixedfloat_secret) == ("k", "a", "b")
    assert s.swap_provider == "fixedfloat" and s.swap_max_fee == 1.5
    d = load_settings(env={"DISCORD_TOKEN": "x"}, ini_path="none.ini")
    assert d.swap_provider == "auto" and d.swap_max_fee == 3.0
    for env in ({"SWAP_PROVIDER": "evil"}, {"SWAP_MAX_FEE_PCT": "0"}, {"SWAP_MAX_FEE_PCT": "99"}):
        with pytest.raises(ConfigError):
            load_settings(env={"DISCORD_TOKEN": "x", **env}, ini_path="none.ini")


# --------------------------------------------------------------------------- #
# Fournisseurs contre un serveur local
# --------------------------------------------------------------------------- #
def serve(routes):
    """Lance un serveur aiohttp local pendant la durée de l'appel; renvoie (runner, base_url)."""
    async def start():
        app = web.Application()
        for method, path, handler in routes:
            app.router.add_route(method, path, handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    return start


def test_swapzone_quote_create_status():
    seen = {}
    ltc, usdt = sw.COINS["ltc"], sw.COINS["usdt"]

    async def rate(req):
        seen["rate"] = (dict(req.query), req.headers.get("x-api-key"))
        return web.json_response([{"quotaId": "q1", "amountTo": 38.0, "adapter": "a"}, {"quotaId": "q2", "amountTo": 39.5, "adapter": "b"},
                                  {"adapter": "no-quota", "amountTo": 99}])

    async def create(req):
        seen["create"] = await req.json()
        return web.json_response({"transaction": {"id": "tx1", "addressDeposit": LTC2}})

    async def tx(req):
        return web.json_response({"transaction": {"status": "finished", "addressReceive": EVM, "amountReceive": 39.5}})

    async def run():
        runner, base = await serve([("GET", "/exchange/get-rate", rate), ("POST", "/exchange/create", create), ("GET", "/exchange/tx", tx)])()
        p = sw.Swapzone("KEY", base=base)
        async with aiohttp.ClientSession() as s:
            q = await p.quote(s, ltc, usdt, 1.0)
            order = await p.create(s, ltc, usdt, 1.0, EVM, LTC, q)
            st = await p.status(s, "tx1")
        await runner.cleanup()
        return q, order, st

    q, order, st = asyncio.run(run())
    assert q.quote_id == "q2" and q.amount_out == 39.5 and q.can_execute and q.note == "b"        # meilleur montant, ignore l'entrée sans quotaId
    assert seen["rate"][1] == "KEY" and seen["rate"][0]["from"] == "ltc" and seen["rate"][0]["to"] == "usdtmatic"
    assert seen["create"] == {"to": "usdtmatic", "from": "ltc", "quotaId": "q2", "email": "", "amountDeposit": 1.0,
                              "addressReceive": EVM, "refundAddress": LTC}
    assert (order.order_id, order.deposit_address) == ("tx1", LTC2) and st["status"] == "finished"
    assert not sw.Swapzone("").can_execute


def test_fixedfloat_signature_and_flow():
    ltc, usdt = sw.COINS["ltc"], sw.COINS["usdt"]
    calls = []

    async def handler(req):
        body = await req.read()
        expected = hmac.new(b"SECRET", body, hashlib.sha256).hexdigest()
        calls.append((req.path, json.loads(body), req.headers["X-API-SIGN"] == expected, req.headers["X-API-KEY"]))
        if req.path.endswith("price"):
            return web.json_response({"code": 0, "data": {"to": {"amount": "39.7"}}})
        if req.path.endswith("create"):
            return web.json_response({"code": 0, "data": {"id": "ABC", "token": "TK", "from": {"address": LTC2}, "to": {"amount": "39.7"}}})
        if req.path.endswith("order"):
            return web.json_response({"code": 0, "data": {"status": "DONE", "to": {"address": EVM, "amount": "39.7"}}})
        return web.json_response({"code": 301, "msg": "nope"})

    async def run():
        runner, base = await serve([("POST", "/price", handler), ("POST", "/create", handler), ("POST", "/order", handler), ("POST", "/bad", handler)])()
        p = sw.FixedFloat("PUB", "SECRET", base=base)
        async with aiohttp.ClientSession() as s:
            q = await p.quote(s, ltc, usdt, 1.0)
            o = await p.create(s, ltc, usdt, 1.0, EVM, LTC, q)
            st = await p.status(s, "ABC", "TK")
            with pytest.raises(sw.SwapError, match="token"):
                await p.status(s, "ABC")
            with pytest.raises(sw.SwapError, match="nope"):
                await p._post(s, "bad", {})
        await runner.cleanup()
        return q, o, st

    q, o, st = asyncio.run(run())
    assert q.amount_out == 39.7 and (o.order_id, o.token, o.deposit_address) == ("ABC", "TK", LTC2) and st["status"] == "DONE"
    assert all(c[2] and c[3] == "PUB" for c in calls)                                              # signature valide partout
    assert calls[1][1]["toAddress"] == EVM and calls[1][1]["refundAddress"] == LTC and calls[0][1]["toCcy"] == "USDTMATIC"


def test_sideshift_quote_and_errors():
    ltc, usdt = sw.COINS["ltc"], sw.COINS["usdt"]

    async def pair(req):
        if req.match_info["a"] == "ltc-mainnet":
            return web.json_response({"min": "0.075", "max": "482", "rate": "39.29"})
        return web.Response(status=500, text="boom")

    async def run():
        runner, base = await serve([("GET", "/pair/{a}/{b}", pair)])()
        p = sw.SideShift(base=base)
        async with aiohttp.ClientSession() as s:
            q = await p.quote(s, ltc, usdt, 10)
            small = await p.quote(s, ltc, usdt, 0.01)
            with pytest.raises(sw.SwapError, match="HTTP 500"):
                await p.quote(s, usdt, ltc, 10)
        await runner.cleanup()
        return q, small

    q, small = asyncio.run(run())
    assert round(q.amount_out, 2) == 392.9 and not q.can_execute and small.note == "min 0.075"
    assert [p.name for p in sw.build_providers("k", "a", "b")] == ["swapzone", "fixedfloat", "sideshift"]
    assert [p.name for p in sw.build_providers()] == ["sideshift"]
    assert [p.name for p in sw.build_providers("", "a", "")] == ["sideshift"]                      # clé FF sans secret: ignorée


# --------------------------------------------------------------------------- #
# Cog avec faux fournisseurs et faux Discord
# --------------------------------------------------------------------------- #
discord = pytest.importorskip("discord")


class FakeProvider:
    can_execute = True

    def __init__(self, name, out, deposit=None, fail_create=False):
        self.name, self.out, self.deposit, self.fail_create = name, out, deposit, fail_create
        self.created = []

    async def quote(self, session, src, dst, amount):
        return sw.Quote(self.name, amount, self.out * amount, True, "qid")

    async def create(self, session, src, dst, amount, to, refund, quote):
        if self.fail_create:
            raise sw.SwapError("provider down")
        self.created.append((amount, to, refund))
        deposit = self.deposit or (LTC2 if src.key == "ltc" else EVM2)
        return sw.Order(self.name, "ORD1", deposit, amount, quote.amount_out, "TOK")

    async def status(self, session, order_id, token=None):
        return {"status": "waiting", "to": EVM, "amount": 1.0}


class Msg:
    def __init__(self, content, uid=1, cid=500):
        self.content, self.author, self.channel, self.deleted = content, SimpleNamespace(id=uid), SimpleNamespace(id=cid), False

    async def delete(self):
        self.deleted = True


class FakeMsg:
    def __init__(self):
        self.edits = []

    async def edit(self, content=None, **kw):
        self.edits.append(content)

    async def delete(self):
        pass


class FakeCtx:
    def __init__(self):
        self.message, self.sent = FakeMsg(), []
        self.channel = SimpleNamespace(id=77)
        self.author = SimpleNamespace(id=1)

    async def send(self, content=None, **kw):
        self.sent.append(content)


class DB:
    def __init__(self):
        self.audits = []

    def audit(self, *a):
        self.audits.append(a)

    def get(self, k, d=None):
        return d

    def set(self, k, v):
        pass


@pytest.fixture
def env():
    from discord.ext import commands

    from selfbot import swapcog
    bot = commands.Bot(command_prefix=".", self_bot=True)
    bot._connection.user = SimpleNamespace(id=1)
    db, sent_funds, announced = DB(), [], []
    providers = [FakeProvider("alpha", 39.0), FakeProvider("beta", 39.6), sw.SideShift()]

    async def balance(coin):
        return {"ltc": 2.0, "usdt": 100.0}[coin]

    async def price(coin):
        return {"litecoin": 40.0, "tether": 1.0}[coin]

    async def send_funds(coin, deposit, amount):
        sent_funds.append((coin, deposit, amount))
        return state["send_result"]

    async def send_message(ch, text):
        announced.append((ch, text))

    async def quote_sideshift(session, src, dst, amount):
        return sw.Quote("sideshift", amount, 39.2 * amount, False)

    providers[2].quote = quote_sideshift
    state = {"pass": "s3cret", "keys": {"ltc": True, "usdt": True}, "maxfee": 3.0, "pref": "auto"}
    bridge = swapcog.SwapBridge(
        ltc_address=lambda: LTC, usdt_address=lambda: EVM, privatepass=lambda: state["pass"], has_key=lambda c: state["keys"][c],
        transaction_channel=lambda: 500, send_funds=send_funds, balance=balance, price_usd=price, providers=lambda: providers,
        provider_pref=lambda: state["pref"], max_fee=lambda: state["maxfee"], send_message=send_message)
    state["send_result"] = "txhash"

    async def build():
        c = swapcog.SwapCog(bot, bridge, lambda: db, ".")
        await bot.add_cog(c)
        return c

    cog = asyncio.run(build())
    bot.get_channel = lambda i: SimpleNamespace(id=i, mention="<#500>", send=None) if i in (500, 77) else None
    return SimpleNamespace(cog=cog, bot=bot, db=db, funds=sent_funds, announced=announced, providers=providers, state=state)


def run(env, name, ctx, *args, **kw):
    return asyncio.run(env.bot.get_command(name).callback(env.cog, ctx, *args, **kw))


def run_with_password(env, name, ctx, password, *args, **kw):
    """Exécute la commande en répondant à `wait_for` avec ``password``."""
    async def go():
        async def fake_wait_for(event, timeout=None, check=None):
            m = Msg(password)
            assert check(m) and not check(Msg(password, uid=2)) and not check(Msg(password, cid=1))
            if password is None:
                raise asyncio.TimeoutError
            env.last_pw_msg = m
            return m
        env.bot.wait_for = fake_wait_for
        return await env.bot.get_command(name).callback(env.cog, ctx, *args, **kw)
    return asyncio.run(go())


def test_swap_commands_registered(env):
    for n in ("swapltc", "swapusdt", "swapstatus", "swaprates", "autosell", "autobuy", "stopautosell", "stopautobuy", "swapauto"):
        assert env.bot.get_command(n), n


def test_swapltc_full_flow_picks_best_provider(env):
    ctx = FakeCtx()
    run_with_password(env, "swapltc", ctx, "s3cret", "0.5")
    assert env.funds == [("ltc", LTC2, 0.5)]
    assert env.providers[1].created == [(0.5, EVM, LTC)] and env.providers[0].created == []        # beta: meilleur devis
    assert "Confirm swap" in ctx.message.edits[0] and "1.00%" in ctx.message.edits[0]
    assert "ORD1" in ctx.message.edits[-1] and env.last_pw_msg.deleted
    assert env.announced and "Swap sent" in env.announced[0][1] and env.announced[0][0] == 500
    assert [a[1] for a in env.db.audits] == ["swap_order", "swap_sent"]


def test_wrong_password_or_timeout_moves_no_funds(env):
    ctx = FakeCtx()
    run_with_password(env, "swapltc", ctx, "wrong", "0.5")
    assert env.funds == [] and "cancelled" in ctx.message.edits[-1]
    run_with_password(env, "swapltc", ctx, None, "0.5")
    assert env.funds == [] and "cancelled" in ctx.message.edits[-1]
    assert not any(p.created for p in env.providers[:2])


def test_fee_guard_force_and_validation(env):
    ctx = FakeCtx()
    env.state["maxfee"] = 0.5                                       # meilleur devis: 1 % > 0.5 %
    run_with_password(env, "swapltc", ctx, "s3cret", "1")
    assert env.funds == [] and "au-delà" in ctx.message.edits[-1]
    run_with_password(env, "swapltc", ctx, "s3cret", "1", "", options="force=yes")
    assert len(env.funds) == 1
    env.funds.clear()
    env.state["maxfee"] = 3.0
    for args, msg in ((("abc",), "Montant invalide"), (("0",), "positif"), (("5",), "Solde insuffisant"),
                      (("1", "notanaddress"), "Adresse Polygon invalide")):
        run_with_password(env, "swapltc", ctx, "s3cret", *args)
        assert msg in ctx.message.edits[-1], (args, ctx.message.edits[-1])
    run_with_password(env, "swapusdt", ctx, "s3cret", "10", LTC)                                      # LTC -> adresse de réception valide
    assert env.funds == [("usdt", EVM2, 10.0)]


def test_all_keeps_fee_reserve_and_floors(env):
    ctx = FakeCtx()
    run_with_password(env, "swapltc", ctx, "s3cret", "all")
    assert env.funds == [("ltc", LTC2, 1.9999)]
    env.funds.clear()
    run_with_password(env, "swapusdt", ctx, "s3cret", "all")
    assert env.funds == [("usdt", EVM2, 100.0)]


def test_missing_keys_password_and_providers(env):
    ctx = FakeCtx()
    env.state["keys"]["ltc"] = False
    run_with_password(env, "swapltc", ctx, "s3cret", "1")
    assert "Clé privée" in ctx.message.edits[-1] and env.funds == []
    env.state["keys"]["ltc"] = True
    env.state["pass"] = ""
    run_with_password(env, "swapltc", ctx, "", "1")
    assert env.funds == []
    env.state["pass"] = "s3cret"
    env.state["pref"] = "fixedfloat"
    run_with_password(env, "swapltc", ctx, "s3cret", "1")
    assert "Aucun fournisseur" in ctx.message.edits[-1] and env.funds == []


def test_bad_deposit_address_and_failures_never_lose_funds_silently(env):
    ctx = FakeCtx()
    env.providers[1].deposit = "0xEVIL"                              # réponse API suspecte
    run_with_password(env, "swapltc", ctx, "s3cret", "1")
    assert env.funds == [] and "adresse de dépôt" in ctx.message.edits[-1]
    env.providers[1].deposit = None
    env.providers[1].fail_create = True
    run_with_password(env, "swapltc", ctx, "s3cret", "1")
    assert env.funds == [] and "provider down" in ctx.message.edits[-1]
    env.providers[1].fail_create = False
    env.state["send_result"] = None                                           # l'envoi échoue après création de l'ordre
    run_with_password(env, "swapltc", ctx, "s3cret", "1")
    assert "ne relancez pas" in ctx.message.edits[-1] and "swap_send_failed" in [a[1] for a in env.db.audits]


def test_swaprates_and_status(env):
    ctx = FakeCtx()
    run(env, "swaprates", ctx, "10", "ltc")
    out = ctx.message.edits[-1]
    assert "beta" in out and "alpha" in out and "sideshift" in out and "compare-only" in out
    assert out.index("beta") < out.index("sideshift") < out.index("alpha")                           # trié par montant reçu
    run(env, "swaprates", ctx, "x", "ltc")
    assert "Usage" in ctx.message.edits[-1]
    run(env, "swapstatus", ctx, "ORD1")                                                               # swapzone non configuré dans le test
    assert "not configured" in ctx.message.edits[-1]
    env.providers.append(FakeProvider("swapzone", 1))
    run(env, "swapstatus", ctx, "ORD1")
    assert "waiting" in ctx.message.edits[-1]


def test_auto_sell_executes_once_then_stops(env, monkeypatch):
    from selfbot import swapcog
    monkeypatch.setattr(swapcog, "POLL", 0.005)
    ctx = FakeCtx()
    said = []
    cur = {"p": 40.0}

    async def _say(t):
        said.append(t)

    env.bot.get_channel = lambda i: SimpleNamespace(id=i, mention="<#500>", send=_say)

    async def price(coin):
        return cur["p"] if coin == "litecoin" else 1.0

    async def go():
        async def fake_wait_for(event, timeout=None, check=None):
            return Msg("s3cret")
        env.bot.wait_for = fake_wait_for
        env.cog.bridge.price_usd = price
        await env.bot.get_command("autosell").callback(env.cog, ctx, "40.2", "0.5")
        for p in (40.0, 40.3, 40.6, 40.5):               # dépasse le seuil puis recule: déclenchement (frais ok: < 3 %)
            cur["p"] = p
            await asyncio.sleep(0.05)
        await asyncio.wait_for(env.cog.autos["autosell"], 5)
        await env.bot.get_command("swapauto").callback(env.cog, ctx)

    asyncio.run(go())
    assert env.funds == [("ltc", LTC2, 0.5)] and "Autosell started" in ctx.message.edits[1]
    assert any("executed at `40.5`" in t for t in said) and "none running" in ctx.message.edits[-1]
    assert [a[1] for a in env.db.audits if a[1].startswith("autosell")] == ["autosell_start", "autosell_end"]


def test_auto_validation_duplicates_and_stop(env):
    ctx = FakeCtx()
    run_with_password(env, "autosell", ctx, "wrong", "45", "1")
    assert "cancelled" in ctx.message.edits[-1] and "autosell" not in env.cog.autos
    run(env, "autosell", ctx, "abc", "1")
    assert "Prix invalide" in ctx.message.edits[-1]
    run(env, "stopautosell", ctx)
    assert "not running" in ctx.message.edits[-1]

    async def go():
        async def fake_wait_for(event, timeout=None, check=None):
            return Msg("s3cret")
        env.bot.wait_for = fake_wait_for
        env.cog.bridge.price_usd = lambda coin: _price(coin)
        await env.bot.get_command("autobuy").callback(env.cog, ctx, "30", "10")
        await env.bot.get_command("autobuy").callback(env.cog, ctx, "30", "10")
        dup = ctx.message.edits[-1]
        await env.bot.get_command("stopautobuy").callback(env.cog, ctx)
        await asyncio.sleep(0)
        return dup, env.cog.autos

    async def _price(coin):
        return 50.0 if coin == "litecoin" else 1.0

    dup, autos = asyncio.run(go())
    assert "already running" in dup and "stopped" in ctx.message.edits[-1] and "autobuy" not in autos

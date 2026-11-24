"""2e vague de commandes: pause, stats, export/import, crypto (price/fees/watch/portfolio), shop watch,
alias, snippets, réactions, traduction, fuseaux, sondages, outils de messages.

La logique pure est dans :mod:`selfbot.toolkit` / :mod:`selfbot.shopwatch` (testée sans Discord).
Les fonctions du selfbot (flags, config, crypto) sont injectées via :class:`Bridge`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord.ext import commands

from . import shop as shop_api
from . import tools, toolkit, ui
from .cogbase import CogHelpers
from .shopwatch import compare_items, diff_watch
from .tools import ToolError

log = logging.getLogger("selfbot.automation")

MAX_WATCHES = 5
MAX_ALERTS = 20
MAX_RULES = 20
TICK = 15                                   # secondes entre deux passes de la boucle de fond
EXPORT_NAME = re.compile(r"^[\w.-]{1,64}\.json$")
COINGECKO = "https://api.coingecko.com/api/v3/simple/price"


@dataclass
class Bridge:
    """Pont vers les globals de selfbot.bot (injecté pour garder le cog testable)."""
    token: Callable[[], str]
    prefix: str
    data_dir: Path
    get_flags: Callable[[], dict]
    set_flags: Callable[..., None]
    export_config: Callable[[], dict]
    import_config: Callable[[dict], None]
    stats: toolkit.Stats
    ltc_address: Callable[[], str] = lambda: ""
    usdt_address: Callable[[], str] = lambda: ""
    get_ltc_balance: Optional[Callable[[str], Awaitable[Any]]] = None
    get_usdt_balance: Optional[Callable[[str], Awaitable[Any]]] = None
    last_transactions: Optional[Callable[[str], Awaitable[Any]]] = None
    extra: dict = field(default_factory=dict)


class Automation(CogHelpers, commands.Cog, name="Automation"):
    def __init__(self, bot: commands.Bot, bridge: Bridge, db_getter=lambda: None):
        self.bot, self.bridge, self._db = bot, bridge, db_getter
        self.prefix = bridge.prefix
        self._task: asyncio.Task | None = None
        self._last_run: dict[str, float] = {}
        self.pause_until: Optional[float] = None
        self._saved_flags: Optional[dict] = None
        self._rule_hits: dict[int, float] = {}
        self.autoreact_enabled = True

    # ---- cycle de vie -----------------------------------------------------
    async def cog_load(self) -> None:
        self._task = asyncio.ensure_future(self._loop())

    async def cog_unload(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            return
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Automation loop error")
            await asyncio.sleep(TICK)

    def _due(self, name: str, every: float, now: float) -> bool:
        if now - self._last_run.get(name, 0) >= every:
            self._last_run[name] = now
            return True
        return False

    async def tick(self, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        if self.pause_until and now >= self.pause_until:
            self._resume()
            await self._notify_any("▶️ Pause ended: sniper and auto-buy restored")
        for name, every, fn in (("alerts", 60, self._check_alerts), ("tx", 45, self._check_watches),
                                ("shop", 600, self._check_shop)):
            if self._due(name, every, now):
                try:
                    await fn()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Automation task %s failed", name)

    async def _notify(self, channel_id: int, text: str) -> None:
        try:
            ch = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            await ch.send(text)
        except (discord.HTTPException, AttributeError):
            log.warning("Notification impossible (salon %s indisponible)", channel_id)

    async def _notify_any(self, text: str) -> None:
        ch = self._kv("notify_channel", None)
        if ch:
            await self._notify(ch, ui.info(text))

    # ======================================================================= #
    # Pause
    # ======================================================================= #
    def _resume(self) -> None:
        if self._saved_flags is not None:
            self.bridge.set_flags(**self._saved_flags)
        self._saved_flags = None
        self.pause_until = None

    @commands.command()
    async def pause(self, ctx, duration: str = "30m"):
        """Pause the sniper + auto-buy for a while: `pause 1h` · `pause off` · `pause status`."""
        d = duration.lower()
        if d in ("off", "stop", "resume"):
            if self.pause_until is None:
                return await self.out(ctx, ui.warn("Not paused"))
            self._resume()
            return await self.out(ctx, ui.ok("Resumed", "Sniper and auto-buy restored"))
        if d == "status":
            if self.pause_until is None:
                return await self.out(ctx, ui.info("Not paused"))
            return await self.out(ctx, ui.warn("Paused", f"Resumes {tools.ts(self.pause_until, 'R')}"))
        try:
            secs = tools.parse_duration(duration)
        except ToolError as e:
            return await self.fail(ctx, e)
        if self._saved_flags is None:
            self._saved_flags = dict(self.bridge.get_flags())
        self.bridge.set_flags(stockSniper=False, autoBuy=False, autoRedeem=False)
        self.pause_until = time.time() + secs
        self._save("notify_channel", ctx.channel.id)
        self._audit("pause", f"{secs}s")
        await self.out(ctx, ui.warn("Paused", f"Sniper, auto-buy and redeemer are off · back {tools.ts(self.pause_until, 'R')}\n"
                                              f"`{self.prefix}pause off` to resume now"))

    # ======================================================================= #
    # Stats
    # ======================================================================= #
    @commands.command()
    async def stats(self, ctx, action: str = ""):
        """Sniper statistics (`stats reset` to clear)."""
        if action.lower() == "reset":
            self.bridge.stats.reset()
            return await self.out(ctx, ui.ok("Statistics cleared"))
        summary = self.bridge.stats.summary()
        if not summary:
            return await self.out(ctx, ui.warn("No statistics yet", "They are recorded as the sniper works"))
        fields = []
        for kind, k in summary.items():
            lat = f" · avg delay {k['avg_latency']:.2f}s" if k["avg_latency"] is not None else ""
            fields.append((kind.capitalize(), f"{k['count']} · last {tools.ts(k['last'], 'R')}{lat}"))
            if k["top"]:
                fields.append(("↳ top channels", ", ".join(f"<#{c}> ({n})" for c, n in k["top"])))
        await self.out(ctx, ui.card("Sniper stats", fields, icon="📊"))

    # ======================================================================= #
    # Export / import de configuration
    # ======================================================================= #
    def _exports_dir(self) -> Path:
        d = self.bridge.data_dir / "exports"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @commands.command(name="export")
    async def export_cmd(self, ctx):
        """Save channels / keywords / sniper settings to a local JSON file (no secrets)."""
        data = {"version": toolkit.CONFIG_VERSION, **self.bridge.export_config()}
        path = self._exports_dir() / f"config-{datetime.now():%Y%m%d-%H%M%S}.json"
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self._audit("export", path.name)
        await self.out(ctx, ui.ok("Configuration exported", f"`{path.name}` in `exports/`\n`{self.prefix}import {path.name}` to restore it"))

    @commands.command(name="import")
    async def import_cmd(self, ctx, filename: str = ""):
        """Restore a configuration exported with `export` (from the `exports/` folder)."""
        d = self._exports_dir()
        if not filename:
            files = sorted(f.name for f in d.glob("*.json"))[-10:]
            return await self.out(ctx, ui.list_card("Exports", [f"`{f}`" for f in files], icon="📦", empty="no export yet"))
        if not EXPORT_NAME.match(filename):
            return await self.fail(ctx, "Invalid file name")
        path = d / filename
        try:
            if path.stat().st_size > 1_000_000:
                raise ToolError("File too large")
            clean = toolkit.validate_config(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return await self.fail(ctx, "File not found", f"`{self.prefix}import` lists the available exports")
        except (json.JSONDecodeError, ToolError, OSError) as e:
            return await self.fail(ctx, "Import refused", str(e))
        self.bridge.import_config(clean)
        self._audit("import", filename)
        await self.out(ctx, ui.ok("Configuration imported", f"{len(clean['stock_channels'])} stock · {len(clean['snipe_channels'])} snipe · "
                                                            f"{len(clean['ticket_channels'])} ticket channels · {len(clean['keywords'])} keywords"))

    # ======================================================================= #
    # Cadeaux Nitro (lecture seule)
    # ======================================================================= #
    @commands.command()
    async def gift(self, ctx, action: str = "info", *, code: str = ""):
        """Check a Nitro gift link/code WITHOUT redeeming it: `gift info <code>`."""
        if action.lower() != "info":
            code = f"{action} {code}".strip()
        try:
            gid = toolkit.parse_gift_code(code)
            data = await self._http_json(
                f"https://discord.com/api/v9/entitlements/gift-codes/{gid}",
                headers={"authorization": self.bridge.token()}, with_application="false", with_subscription_plan="true")
        except ToolError as e:
            if "HTTP 404" in str(e):
                return await self.out(ctx, ui.warn("Unknown or expired gift code"))
            return await self.fail(ctx, e)
        plan = (data.get("subscription_plan") or {}).get("name") or "Unknown"
        uses, max_uses = data.get("uses", 0), data.get("max_uses", 1)
        valid = not data.get("redeemed") and uses < max_uses
        fields = [("Product", plan), ("Status", "🟢 Valid" if valid else "🔴 Already redeemed"), ("Uses", f"{uses}/{max_uses}")]
        if data.get("expires_at"):
            fields.append(("Expires", data["expires_at"][:10]))
        await self.out(ctx, ui.card("Gift code", fields, icon="🎁", footer="not redeemed"))

    # ======================================================================= #
    # Crypto
    # ======================================================================= #
    async def _prices(self, coins: list[str], *currencies: str) -> dict:
        data = await self._http_json(COINGECKO, ids=",".join(coins), vs_currencies=",".join(currencies or ("eur",)),
                                     include_24hr_change="true")
        if not isinstance(data, dict):
            raise ToolError("Prix indisponibles")
        return data

    @commands.group(invoke_without_command=True)
    async def price(self, ctx, symbol: str = "ltc"):
        """Crypto price (€ / $ / 24h): `price btc` · alerts: `price alert btc above 60000`."""
        try:
            coin = toolkit.coin_id(symbol)
            data = (await self._prices([coin], "eur", "usd")).get(coin)
        except ToolError as e:
            return await self.fail(ctx, e)
        if not data:
            return await self.fail(ctx, f"Unknown coin `{tools.trunc(symbol, 30)}`")
        ch = data.get("eur_24h_change")
        await self.out(ctx, ui.card(coin.capitalize(), [("EUR", f"`{data['eur']}€`"), ("USD", f"`{data['usd']}$`")] +
                                    ([("24h", f"{'🟢' if ch >= 0 else '🔴'} `{ch:+.2f}%`")] if ch is not None else []), icon="📈"))

    @price.command(name="alert")
    async def price_alert(self, ctx, *args: str):
        alerts = self._kv("price_alerts", [])
        try:
            coin, direction, value = toolkit.parse_alert(list(args))
            if len(alerts) >= MAX_ALERTS:
                raise ToolError(f"Maximum {MAX_ALERTS} alertes")
        except ToolError as e:
            return await self.fail(ctx, e)
        aid = max((a["id"] for a in alerts), default=0) + 1
        alerts.append({"id": aid, "coin": coin, "dir": direction, "value": value, "channel": ctx.channel.id})
        self._save("price_alerts", alerts)
        await self.out(ctx, ui.ok(f"Alert #{aid} set", f"{coin} {direction} `{value}€`"))

    @price.command(name="alerts")
    async def price_alerts(self, ctx):
        rows = [f"`#{a['id']}` {a['coin']} {a['dir']} `{a['value']}€`" for a in self._kv("price_alerts", [])]
        await self.out(ctx, ui.list_card("Price alerts", rows, icon="🔔", empty="no alert"))

    @price.command(name="del")
    async def price_del(self, ctx, alert_id: int):
        alerts = self._kv("price_alerts", [])
        kept = [a for a in alerts if a["id"] != alert_id]
        if len(kept) == len(alerts):
            return await self.fail(ctx, f"No alert #{alert_id}")
        self._save("price_alerts", kept)
        await self.out(ctx, ui.ok(f"Alert #{alert_id} deleted"))

    async def _check_alerts(self) -> None:
        alerts = self._kv("price_alerts", [])
        if not alerts:
            return
        coins = sorted({a["coin"] for a in alerts})
        data = await self._prices(coins, "eur")
        prices = {c: v["eur"] for c, v in data.items() if isinstance(v, dict) and "eur" in v}
        hits = toolkit.triggered_alerts(alerts, prices)
        if hits:
            done = {a["id"] for a in hits}
            self._save("price_alerts", [a for a in alerts if a["id"] not in done])
            for a in hits:
                await self._notify(a["channel"], ui.info(f"🔔 {a['coin']} is {a['dir']} `{a['value']}€`", f"Now `{a['price']}€`"))

    @commands.command()
    async def fees(self, ctx):
        """Estimated Litecoin network fees for a standard transaction."""
        try:
            net = await self._http_json("https://api.blockcypher.com/v1/ltc/main")
            eur = (await self._prices(["litecoin"], "eur")).get("litecoin", {}).get("eur")
            rows = []
            for label, key in (("Low", "low_fee_per_kb"), ("Medium", "medium_fee_per_kb"), ("High", "high_fee_per_kb")):
                fee = toolkit.estimate_fee(net[key])
                rows.append((label, f"`{fee:.8f} LTC`" + (f" ≈ `{fee * eur:.4f}€`" if eur else "")))
        except (ToolError, KeyError) as e:
            return await self.fail(ctx, "Fees unavailable", str(e))
        await self.out(ctx, ui.card("LTC network fees", rows, icon="⛽", footer="standard tx: 1 input, 2 outputs (~225 bytes)"))

    @commands.group(invoke_without_command=True)
    async def watch(self, ctx, address: str = ""):
        """Notify when a Litecoin address receives a payment: `watch <address>` · `watch list` · `watch del <n>`."""
        if not address:
            return await self.fail(ctx, "Address missing", f"`{self.prefix}watch <address>`")
        watches = self._kv("tx_watch", [])
        try:
            addr = toolkit.validate_ltc_address(address)
            if len(watches) >= MAX_WATCHES:
                raise ToolError(f"Maximum {MAX_WATCHES} adresses surveillées")
            if any(w["address"] == addr for w in watches):
                raise ToolError("Adresse déjà surveillée")
        except ToolError as e:
            return await self.fail(ctx, e)
        last = None
        if self.bridge.last_transactions:
            try:
                txs = await self.bridge.last_transactions(addr)
                last = txs[0]["hash"] if txs else ""
            except Exception:
                log.warning("Initial transaction fetch failed for a watched address")
        wid = max((w["id"] for w in watches), default=0) + 1
        watches.append({"id": wid, "address": addr, "channel": ctx.channel.id, "last": last})
        self._save("tx_watch", watches)
        await self.out(ctx, ui.ok(f"Watching #{wid}", f"`{addr}`"))

    @watch.command(name="list")
    async def watch_list(self, ctx):
        rows = [f"`#{w['id']}` `{w['address']}`" for w in self._kv("tx_watch", [])]
        await self.out(ctx, ui.list_card("Watched addresses", rows, icon="👀", empty="none"))

    @watch.command(name="del")
    async def watch_del(self, ctx, watch_id: int):
        ws = self._kv("tx_watch", [])
        kept = [w for w in ws if w["id"] != watch_id]
        if len(kept) == len(ws):
            return await self.fail(ctx, f"No watch #{watch_id}")
        self._save("tx_watch", kept)
        await self.out(ctx, ui.ok(f"Watch #{watch_id} deleted"))

    async def _check_watches(self) -> None:
        watches = self._kv("tx_watch", [])
        if not watches or not self.bridge.last_transactions:
            return
        changed = False
        for w in watches:
            txs = await self.bridge.last_transactions(w["address"])
            if not txs:
                continue
            if w.get("last") is None:                      # première passe: on mémorise sans notifier
                w["last"], changed = txs[0]["hash"], True
                continue
            new = []
            for tx in txs:
                if tx["hash"] == w["last"]:
                    break
                new.append(tx)
            if new:
                w["last"], changed = txs[0]["hash"], True
                for tx in reversed(new[:5]):
                    await self._notify(w["channel"], ui.info("💰 Payment received",
                                                             f"`{tx['amount']} LTC` on `{w['address'][:8]}…`\n[`{tx['hash'][:10]}…`](https://live.blockcypher.com/ltc/tx/{tx['hash']})"))
        if changed:
            self._save("tx_watch", watches)

    @commands.command()
    async def portfolio(self, ctx):
        """Total of your LTC + USDT balances in EUR."""
        rows, total = [], 0.0
        try:
            prices = await self._prices(["litecoin", "tether"], "eur")
            ltc_addr, usdt_addr = self.bridge.ltc_address(), self.bridge.usdt_address()
            if ltc_addr and self.bridge.get_ltc_balance:
                bal = await self.bridge.get_ltc_balance(ltc_addr)
                ltc = bal["final_balance"] / 1e8 if bal else 0.0
                val = ltc * prices["litecoin"]["eur"]
                total += val
                rows.append(("Litecoin", f"`{ltc:.6f} LTC` ≈ `{val:.2f}€`"))
            if usdt_addr and self.bridge.get_usdt_balance:
                bal = await self.bridge.get_usdt_balance(usdt_addr)
                usdt = float(bal["balance"]) if bal else 0.0
                val = usdt * prices["tether"]["eur"]
                total += val
                rows.append(("USDT", f"`{usdt:.2f} USDT` ≈ `{val:.2f}€`"))
        except (ToolError, KeyError, TypeError, ValueError) as e:
            return await self.fail(ctx, "Portfolio unavailable", str(e))
        if not rows:
            return await self.fail(ctx, "No wallet address configured", "Set LTC_ADDRESS / USDT_ADDRESS in `.env`")
        await self.out(ctx, ui.card("Portfolio", rows + [("Total", f"**{total:.2f}€**")], icon="💼"))

    # ======================================================================= #
    # Shop: watch / compare (appelé par la commande `shop`)
    # ======================================================================= #
    async def shop_extra(self, ctx, args: str) -> bool:
        """Gère `shop watch|watches|unwatch|compare`. Renvoie True si la commande a été traitée."""
        sub, _, rest = args.strip().partition(" ")
        sub = sub.lower()
        if sub not in ("watch", "watches", "unwatch", "compare"):
            return False
        try:
            if sub == "watch":
                await self._shop_watch_add(ctx, rest)
            elif sub == "watches":
                rows = [f"`#{w['id']}` `{w['query']}`" for w in self._kv("shop_watches", [])]
                await self.out(ctx, ui.list_card("Shop watches", rows, icon="👀", empty="none"))
            elif sub == "unwatch":
                ws = self._kv("shop_watches", [])
                kept = [w for w in ws if str(w["id"]) != rest.strip().lstrip("#")]
                if len(kept) == len(ws):
                    raise ToolError("Watch introuvable")
                self._save("shop_watches", kept)
                await self.out(ctx, ui.ok("Shop watch deleted"))
            else:
                await self._shop_compare(ctx, rest)
        except (ToolError, shop_api.ShopError) as e:
            await self.fail(ctx, e)
        return True

    async def _fetch_shop(self, query: shop_api.Query) -> list:
        async with aiohttp.ClientSession() as s:
            return await shop_api.fetch_items(s, self.bridge.token(), query)

    async def _shop_watch_add(self, ctx, text: str) -> None:
        q = shop_api.parse_query(text)
        ws = self._kv("shop_watches", [])
        if len(ws) >= MAX_WATCHES:
            raise ToolError(f"Maximum {MAX_WATCHES} surveillances")
        items = shop_api.apply_filters(await self._fetch_shop(q), q)
        _, _, state = diff_watch({}, items)
        wid = max((w["id"] for w in ws), default=0) + 1
        ws.append({"id": wid, "query": text.strip() or "all", "channel": ctx.channel.id, "seen": dict(list(state.items())[:2000])})
        self._save("shop_watches", ws)
        await self.out(ctx, ui.ok(f"Shop watch #{wid} set", f"`{text.strip() or 'all'}` · {len(items)} matching item(s) now, "
                                                           f"you'll be told about new ones and price drops (every 10 min)"))

    async def _check_shop(self) -> None:
        ws = self._kv("shop_watches", [])
        if not ws:
            return
        for w in ws:
            q = shop_api.parse_query("" if w["query"] == "all" else w["query"])
            shop_api.clear_cache()
            items = shop_api.apply_filters(await self._fetch_shop(q), q)
            new, drops, state = diff_watch(w.get("seen", {}), items)
            w["seen"] = dict(list(state.items())[:2000])
            lines = [f"🆕 {shop_api.format_item(i)}" for i in new[:8]] + \
                    [f"📉 {shop_api.format_item(i)} (was {shop_api.fmt_price(old, i.currency)})" for i, old in drops[:8]]
            if lines:
                await self._notify(w["channel"], ui.card(f"Shop watch #{w['id']}", icon="🛍️", desc="\n".join(lines)))
        self._save("shop_watches", ws)

    async def _shop_compare(self, ctx, text: str) -> None:
        names = [n.strip() for n in text.split("|") if n.strip()]
        if len(names) != 2:
            raise ToolError("Format: `shop compare nom 1 | nom 2`")
        items = await self._fetch_shop(shop_api.Query(scan=shop_api.MAX_FETCH))
        found = []
        for n in names:
            m = shop_api.apply_filters(items, shop_api.Query(name=n))
            if not m:
                raise ToolError(f"Aucun item trouvé pour `{tools.trunc(n, 40)}`")
            found.append(m[0])
        a, b = found
        await self.out(ctx, ui.card(f"{a.name} vs {b.name}", icon="⚖️", desc="\n".join(compare_items(a, b))))

    # ======================================================================= #
    # Alias et snippets
    # ======================================================================= #
    def expand(self, content: str) -> str:
        return toolkit.expand_alias(content, self.prefix, self._kv("aliases", {}))

    @commands.command()
    async def alias(self, ctx, action: str = "list", name: str = "", *, command: str = ""):
        """Personal shortcuts: `alias add p price btc` · `alias list` · `alias del p`."""
        aliases = self._kv("aliases", {})
        action = action.lower()
        try:
            if action == "add":
                name = toolkit.check_name(name)
                target = command.strip().lstrip(self.prefix)
                if self.bot.get_command(name):
                    raise ToolError(f"`{name}` est déjà une commande")
                if not target or self.bot.get_command(target.split()[0]) is None:
                    raise ToolError("La cible doit commencer par une commande existante")
                if len(aliases) >= 50:
                    raise ToolError("Maximum 50 alias")
                aliases[name] = tools.trunc(target, 200)
                self._save("aliases", aliases)
                return await self.out(ctx, ui.ok(f"Alias `{self.prefix}{name}` created", f"→ `{self.prefix}{aliases[name]}`"))
            if action in ("del", "rm"):
                if aliases.pop(name.lower(), None) is None:
                    raise ToolError("Alias introuvable")
                self._save("aliases", aliases)
                return await self.out(ctx, ui.ok("Alias deleted"))
            if action == "list":
                return await self.out(ctx, ui.list_card("Aliases", [f"`{self.prefix}{k}` → `{self.prefix}{v}`" for k, v in aliases.items()],
                                                        icon="🔀", empty="none"))
            raise ToolError("Action inconnue (add|list|del)")
        except ToolError as e:
            await self.fail(ctx, e)

    @commands.command(aliases=["snip"])
    async def snippet(self, ctx, name: str = "list", *, text: str = ""):
        """Saved texts: `snippet add vouch Thanks!` · `snippet vouch` sends it · `snippet list|del <name>`."""
        snips = self._kv("snippets", {})
        key = name.lower()
        try:
            if key == "add":
                sname, _, body = text.partition(" ")
                sname = toolkit.check_name(sname)
                if sname in ("add", "list", "del"):
                    raise ToolError("Nom réservé")
                if not body.strip():
                    raise ToolError("Texte manquant")
                if len(snips) >= 50 and sname not in snips:
                    raise ToolError("Maximum 50 snippets")
                snips[sname] = body.strip()[:1500]
                self._save("snippets", snips)
                return await self.out(ctx, ui.ok(f"Snippet `{sname}` saved"))
            if key in ("del", "rm"):
                if snips.pop(text.strip().lower(), None) is None:
                    raise ToolError("Snippet introuvable")
                self._save("snippets", snips)
                return await self.out(ctx, ui.ok("Snippet deleted"))
            if key == "list":
                return await self.out(ctx, ui.list_card("Snippets", [f"`{k}` · {tools.trunc(v, 60)}" for k, v in snips.items()],
                                                        icon="📋", empty="none"))
            if key not in snips:
                raise ToolError("Snippet introuvable", )
            await ctx.message.edit(content=snips[key])
        except ToolError as e:
            await self.fail(ctx, e)

    # ======================================================================= #
    # Réactions
    # ======================================================================= #
    async def _target_message(self, ctx, message_id: str = ""):
        ref = ctx.message.reference
        if ref and ref.message_id:
            return await ctx.channel.fetch_message(ref.message_id)
        if message_id:
            return await ctx.channel.fetch_message(tools.parse_snowflake(message_id))
        async for m in ctx.channel.history(limit=2):
            if m.id != ctx.message.id:
                return m
        raise ToolError("Aucun message cible")

    @commands.command()
    async def react(self, ctx, emoji: str, message_id: str = ""):
        """React to the replied / previous / given message."""
        try:
            toolkit.check_emoji(emoji)
            msg = await self._target_message(ctx, message_id)
            await msg.add_reaction(emoji)
        except ToolError as e:
            return await self.fail(ctx, e)
        except discord.HTTPException as e:
            return await self.fail(ctx, "Reaction failed", str(e.text)[:150])
        await ctx.message.delete()

    @commands.group(invoke_without_command=True)
    async def autoreact(self, ctx):
        """Automatic reactions: `autoreact add 🔥 user <id>` · `autoreact add 👀 channel <id>` · `list` · `del <n>` · `on|off`."""
        rules = self._kv("autoreacts", [])
        rows = [f"`#{r['id']}` {r['emoji']} · {r['kind']} `{r['target']}`" for r in rules]
        await self.out(ctx, ui.list_card("Auto-reactions", rows, icon="😎", empty="none",
                                         footer="enabled" if self.autoreact_enabled else "currently OFF"))

    @autoreact.command(name="add")
    async def autoreact_add(self, ctx, emoji: str, kind: str, target: str):
        rules = self._kv("autoreacts", [])
        try:
            toolkit.check_emoji(emoji)
            if kind.lower() not in ("user", "channel"):
                raise ToolError("Type attendu: `user` ou `channel`")
            if len(rules) >= MAX_RULES:
                raise ToolError(f"Maximum {MAX_RULES} règles")
            tid = tools.parse_snowflake(target)
        except ToolError as e:
            return await self.fail(ctx, e)
        rid = max((r["id"] for r in rules), default=0) + 1
        rules.append({"id": rid, "emoji": emoji, "kind": kind.lower(), "target": tid})
        self._save("autoreacts", rules)
        await self.out(ctx, ui.ok(f"Rule #{rid} added"))

    @autoreact.command(name="del")
    async def autoreact_del(self, ctx, rule_id: int):
        rules = self._kv("autoreacts", [])
        kept = [r for r in rules if r["id"] != rule_id]
        if len(kept) == len(rules):
            return await self.fail(ctx, f"No rule #{rule_id}")
        self._save("autoreacts", kept)
        await self.out(ctx, ui.ok(f"Rule #{rule_id} deleted"))

    @autoreact.command(name="on")
    async def autoreact_on(self, ctx):
        self.autoreact_enabled = True
        await self.out(ctx, ui.ok("Auto-reactions enabled"))

    @autoreact.command(name="off")
    async def autoreact_off(self, ctx):
        self.autoreact_enabled = False
        await self.out(ctx, ui.ok("Auto-reactions disabled"))

    @commands.Cog.listener()
    async def on_message(self, message):
        if not self.autoreact_enabled or message.author.id == self.bot.user.id:
            return
        now = time.time()
        for r in self._kv("autoreacts", []):
            hit = (r["kind"] == "user" and message.author.id == r["target"]) or \
                  (r["kind"] == "channel" and message.channel.id == r["target"])
            if hit and now - self._rule_hits.get(r["id"], 0) >= 2:       # au plus une réaction / 2 s par règle
                self._rule_hits[r["id"]] = now
                try:
                    await message.add_reaction(r["emoji"])
                except discord.HTTPException:
                    log.debug("Auto-reaction refusée")

    # ======================================================================= #
    # Traduction, fuseaux, sondages
    # ======================================================================= #
    @commands.command(aliases=["tr"])
    async def translate(self, ctx, lang: str, *, text: str = ""):
        """Translate (Google, unofficial endpoint): `translate fr hello` or reply to a message."""
        try:
            lang = toolkit.check_lang(lang)
            if not text and ctx.message.reference and ctx.message.reference.message_id:
                text = (await ctx.channel.fetch_message(ctx.message.reference.message_id)).content
            if not text.strip():
                raise ToolError("Texte manquant")
            data = await self._http_json("https://translate.googleapis.com/translate_a/single",
                                         client="gtx", sl="auto", tl=lang, dt="t", q=text[:1500])
            translated, src = toolkit.parse_translation(data)
        except ToolError as e:
            return await self.fail(ctx, e)
        await self.out(ctx, ui.card(f"{src} → {lang}", icon="🌍", desc=tools.neutralize_mentions(tools.trunc(translated, 1500)),
                                    footer="the text was sent to Google Translate"))

    @commands.group(invoke_without_command=True)
    async def tz(self, ctx, user: str = ""):
        """Local time of someone: `tz set <user> Europe/Paris` · `tz <user>` · `tz list` · `tz del <user>`."""
        zones = self._kv("timezones", {})
        try:
            uid = tools.parse_snowflake(user) if user else ctx.author.id
        except ToolError as e:
            return await self.fail(ctx, e)
        zone = zones.get(str(uid))
        if not zone:
            return await self.fail(ctx, "No timezone saved", f"`{self.prefix}tz set <user> Europe/Paris`")
        now = datetime.now(ZoneInfo(zone))
        await self.out(ctx, ui.card(f"<@{uid}>", [("Zone", zone), ("Local time", now.strftime("%H:%M · %A %d %B")),
                                                  ("Offset", now.strftime("UTC%z"))], icon="🕒"))

    @tz.command(name="set")
    async def tz_set(self, ctx, user: str, zone: str):
        try:
            uid = ctx.author.id if user.lower() == "me" else tools.parse_snowflake(user)
            toolkit.valid_timezone(zone)
        except ToolError as e:
            return await self.fail(ctx, e)
        zones = self._kv("timezones", {})
        if len(zones) >= 200 and str(uid) not in zones:
            return await self.fail(ctx, "Maximum 200 fuseaux")
        zones[str(uid)] = zone
        self._save("timezones", zones)
        await self.out(ctx, ui.ok("Timezone saved", f"<@{uid}> · {zone}"))

    @tz.command(name="list")
    async def tz_list(self, ctx):
        rows = [f"<@{u}> · {z} · {datetime.now(ZoneInfo(z)):%H:%M}" for u, z in self._kv("timezones", {}).items()]
        await self.out(ctx, ui.list_card("Timezones", rows, icon="🌐", empty="none"))

    @tz.command(name="del")
    async def tz_del(self, ctx, user: str):
        zones = self._kv("timezones", {})
        try:
            uid = str(ctx.author.id if user.lower() == "me" else tools.parse_snowflake(user))
        except ToolError as e:
            return await self.fail(ctx, e)
        if zones.pop(uid, None) is None:
            return await self.fail(ctx, "No timezone for this user")
        self._save("timezones", zones)
        await self.out(ctx, ui.ok("Timezone deleted"))

    @commands.command()
    async def poll(self, ctx, *, text: str):
        """Poll with reactions: `poll Pizza tonight? | Yes | No | Maybe`."""
        try:
            question, options = toolkit.parse_poll(text)
        except ToolError as e:
            return await self.fail(ctx, e)
        lines = [f"> {toolkit.NUM_EMOJIS[i]} {tools.neutralize_mentions(o)}" for i, o in enumerate(options)]
        await self.out(ctx, ui.card(tools.neutralize_mentions(question), icon="📊", desc="\n".join(lines), footer="react to vote"))
        for i in range(len(options)):
            try:
                await ctx.message.add_reaction(toolkit.NUM_EMOJIS[i])
            except discord.HTTPException:
                break
            await asyncio.sleep(0.4)

    # ======================================================================= #
    # Outils de messages
    # ======================================================================= #
    @commands.command()
    async def copyid(self, ctx):
        """IDs of the replied message / its author / the channel / the server."""
        rows = [("Channel", f"`{ctx.channel.id}`")]
        if ctx.guild:
            rows.append(("Server", f"`{ctx.guild.id}`"))
        ref = ctx.message.reference
        if ref and ref.message_id:
            msg = await ctx.channel.fetch_message(ref.message_id)
            rows = [("Message", f"`{msg.id}`"), ("Author", f"`{msg.author.id}`")] + rows
        await self.out(ctx, ui.card("IDs", rows, icon="🆔"))

    @commands.command()
    async def jump(self, ctx, message_id: str):
        """Link to a message of this channel by ID."""
        try:
            mid = tools.parse_snowflake(message_id)
        except ToolError as e:
            return await self.fail(ctx, e)
        gid = ctx.guild.id if ctx.guild else "@me"
        await self.out(ctx, ui.card("Jump", icon="🔗", desc=f"https://discord.com/channels/{gid}/{ctx.channel.id}/{mid}"))

    @commands.command()
    async def pins(self, ctx):
        """List the pinned messages of the channel."""
        try:
            pinned = await ctx.channel.pins()
        except discord.HTTPException as e:
            return await self.fail(ctx, "Could not read pins", str(e.text)[:150])
        rows = [f"[{tools.trunc((m.content or '(attachment)').replace(chr(10), ' '), 70)}]({m.jump_url}) · {m.author}" for m in pinned[:15]]
        await self.out(ctx, ui.list_card("Pinned messages", [tools.neutralize_mentions(r) for r in rows], icon="📌", empty="none"))

    @commands.command()
    async def purgeuser(self, ctx, user: str, limit: int = 50):
        """Delete the recent messages of one user in this channel (needs Manage Messages): `purgeuser <user> [n≤200]`."""
        try:
            uid = tools.parse_snowflake(user)
        except ToolError as e:
            return await self.fail(ctx, e)
        limit = max(1, min(limit, 200))
        try:
            deleted = await ctx.channel.purge(limit=limit, check=lambda m: m.author.id == uid)
        except discord.Forbidden:
            return await self.fail(ctx, "Missing permission", "Manage Messages is required to delete other people's messages")
        except discord.HTTPException as e:
            return await self.fail(ctx, "Discord refused the action", str(e.text)[:150])
        self._audit("purgeuser", f"channel={ctx.channel.id} target={uid} n={len(deleted)}")
        await self.out(ctx, ui.ok(f"Deleted {len(deleted)} message(s)"))


async def setup(bot, bridge: Bridge, db_getter=lambda: None) -> Automation:
    cog = Automation(bot, bridge, db_getter)
    await bot.add_cog(cog)
    return cog

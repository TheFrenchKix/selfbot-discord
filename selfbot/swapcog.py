"""Commandes de swap LTC <-> USDT (Polygon): swapltc, swapusdt, swapstatus, swaprates, autosell, autobuy.

Reprend les commandes de l'ancien bot (main_ancien.py) avec les garde-fous suivants:
confirmation par mot de passe dans le salon de transactions, frais maximum vs marché, adresses validées
(destination et dépôt), un seul swap à la fois, journal d'audit et arrêt après le premier déclenchement
des automatismes (pas de relance après un échec: les fonds ont pu partir).
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import math
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import aiohttp
import discord
from discord.ext import commands

from . import swap as sw
from . import ui
from .cogbase import CogHelpers
from .tools import ToolError

log = logging.getLogger("selfbot.swap")
CONFIRM_TIMEOUT = 30
POLL = 30                                   # secondes entre deux relevés de prix des automatismes
LTC_FEE_RESERVE = 0.0001                    # laissé sur le compte pour les frais réseau lors d'un « all »


@dataclass
class SwapBridge:
    ltc_address: Callable[[], str]
    usdt_address: Callable[[], str]
    privatepass: Callable[[], str]
    has_key: Callable[[str], bool]                                  # 'ltc' / 'usdt': clé privée définie ?
    transaction_channel: Callable[[], int]
    send_funds: Callable[[str, str, float], Awaitable[Any]]         # (coin, deposit_address, amount)
    balance: Callable[[str], Awaitable[Optional[float]]]            # coin -> solde (None si indisponible)
    price_usd: Callable[[str], Awaitable[Optional[float]]]          # 'litecoin' / 'tether'
    providers: Callable[[], list]
    provider_pref: Callable[[], str] = lambda: "auto"
    max_fee: Callable[[], float] = lambda: 3.0
    send_message: Optional[Callable[[int, str], Awaitable[Any]]] = None


class SwapCog(CogHelpers, commands.Cog, name="Swap"):
    def __init__(self, bot: commands.Bot, bridge: SwapBridge, db_getter=lambda: None, prefix: str = "."):
        self.bot, self.bridge, self._db, self.prefix = bot, bridge, db_getter, prefix
        self.lock = asyncio.Lock()
        self.autos: dict[str, asyncio.Task] = {}

    async def cog_unload(self) -> None:
        for t in self.autos.values():
            t.cancel()

    # ---- données ------------------------------------------------------------
    async def _prices(self) -> dict:
        ltc, usdt = await self.bridge.price_usd("litecoin"), await self.bridge.price_usd("tether")
        if not ltc:
            raise ToolError("Prix du LTC indisponible")
        return {"ltc": float(ltc), "usdt": float(usdt or 1.0)}

    async def _quotes(self, src, dst, amount: float, only_exec: bool = False) -> list:
        prices = await self._prices()
        providers = [p for p in self.bridge.providers() if p.can_execute or not only_exec]
        out: list = []
        async with aiohttp.ClientSession() as session:
            results = await asyncio.gather(*(p.quote(session, src, dst, amount) for p in providers), return_exceptions=True)
        for p, r in zip(providers, results):
            if isinstance(r, Exception):
                out.append(sw.Quote(p.name, amount, None, p.can_execute, note=str(r)[:80] if isinstance(r, sw.SwapError) else type(r).__name__))
            else:
                out.append(sw.with_spread(r, src, dst, prices))
        return out

    def _parse_amount(self, text: str, balance: float, coin: sw.Coin) -> float:
        t = (text or "").strip().lower()
        if t == "all":
            amount = balance - (LTC_FEE_RESERVE if coin.key == "ltc" else 0.0)
        else:
            try:
                amount = float(t.replace(",", "."))
            except ValueError:
                raise ToolError("Montant invalide (nombre ou `all`)") from None
        factor = 10 ** coin.decimals
        amount = math.floor(amount * factor + 1e-9) / factor if t == "all" else round(amount, coin.decimals)
        if amount <= 0:
            raise ToolError("Le montant doit être positif (solde insuffisant ?)")
        if amount > balance + 1e-12:
            raise ToolError(f"Solde insuffisant: {balance} {coin.label} disponible")
        return amount

    def _choose(self, quotes: list, pref: str):
        pref = (pref or "auto").lower()
        if pref == "auto":
            best = sw.pick_best(quotes)
        else:
            best = next((q for q in quotes if q.provider == pref and q.can_execute and q.amount_out), None)
        if best is None:
            raise sw.SwapError("Aucun fournisseur disponible: définissez SWAPZONE_API_KEY ou FIXEDFLOAT_API_KEY / FIXEDFLOAT_API_SECRET dans .env")
        return best

    # ---- préparation / exécution -------------------------------------------
    async def prepare(self, src_key: str, amount_text: str, address: str, force: bool = False) -> dict:
        src, dst = sw.direction(src_key)
        if not self.bridge.has_key(src.key) or not self.bridge.privatepass():
            raise ToolError(f"Clé privée {src.label} / mot de passe non définis (`{self.prefix}setprivate{'ltc' if src.key == 'ltc' else 'usdt'}`)")
        mine = self.bridge.ltc_address() if src.key == "ltc" else self.bridge.usdt_address()
        refund = mine
        if not mine:
            raise ToolError(f"Adresse {src.label} non configurée")
        to = dst.validate(address or (self.bridge.ltc_address() if dst.key == "ltc" else self.bridge.usdt_address()))
        balance = await self.bridge.balance(src.key)
        if balance is None:
            raise ToolError("Solde indisponible")
        amount = self._parse_amount(amount_text, balance, src)
        quotes = await self._quotes(src, dst, amount, only_exec=True)
        best = self._choose(quotes, self.bridge.provider_pref())
        if not force:
            sw.check_fee(best, self.bridge.max_fee())
        provider = next(p for p in self.bridge.providers() if p.name == best.provider)
        return {"src": src, "dst": dst, "amount": amount, "to": to, "refund": refund, "quote": best, "provider": provider, "balance": balance}

    def plan_card(self, plan: dict) -> str:
        q = plan["quote"]
        fields = [("Swap", f"`{plan['amount']} {plan['src'].label}` → {plan['dst'].label}"),
                  ("Estimated", f"`{q.amount_out} {plan['dst'].label}`" if q.amount_out else "unknown"),
                  ("Cost vs market", f"`{q.spread:.2f}%`" if q.spread is not None else "unknown"),
                  ("Provider", q.provider), ("Destination", f"`{plan['to']}`")]
        return ui.card("Confirm swap", fields, icon="💱")

    async def execute(self, plan: dict, actor: str = "owner") -> sw.Order:
        """Crée l'ordre, vérifie la réponse, envoie les fonds. Un seul swap à la fois."""
        async with self.lock:
            src, dst, amount = plan["src"], plan["dst"], plan["amount"]
            async with aiohttp.ClientSession() as session:
                order = await plan["provider"].create(session, src, dst, amount, plan["to"], plan["refund"], plan["quote"])
            sw.check_order(order, src, amount)
            self._audit("swap_order", f"{order.provider} {order.order_id} {amount} {src.key}->{dst.key}")
            sent = await self.bridge.send_funds(src.key, order.deposit_address, amount)
            if not sent:
                self._audit("swap_send_failed", f"{order.provider} {order.order_id}")
                raise sw.SwapError(f"Ordre {order.order_id} créé mais l'envoi des fonds a échoué: ne relancez pas sans vérifier ({order.provider})")
            self._audit("swap_sent", f"{order.provider} {order.order_id}")
            await self._announce(plan, order)
            return order

    async def _announce(self, plan: dict, order: sw.Order) -> None:
        if not self.bridge.send_message:
            return
        q = plan["quote"]
        fields = [("Order", f"`{order.order_id}`"), ("Provider", order.provider),
                  ("Sent", f"`{plan['amount']} {plan['src'].label}`"), ("Expected", f"`{order.amount_out or q.amount_out} {plan['dst'].label}`"),
                  ("To", f"`{plan['to']}`")]
        if order.token:
            fields.append(("Token", f"`{order.token}`"))
        try:
            await self.bridge.send_message(self.bridge.transaction_channel(), ui.card("Swap sent", fields, icon="💱",
                                                                                      footer=f"{self.prefix}swapstatus {order.order_id}"))
        except Exception:
            log.warning("Annonce du swap impossible")

    async def _confirm(self, ctx) -> bool:
        """Attend le mot de passe dans le salon de transactions."""
        ch_id = self.bridge.transaction_channel()
        if not ch_id or self.bot.get_channel(ch_id) is None:
            raise ToolError("Salon de transactions non configuré")
        try:
            msg = await self.bot.wait_for("message", timeout=CONFIRM_TIMEOUT,
                                          check=lambda m: m.author.id == self.bot.user.id and m.channel.id == ch_id)
        except asyncio.TimeoutError:
            return False
        try:
            await msg.delete()
        except discord.HTTPException:
            pass
        return hmac.compare_digest(msg.content.encode(), self.bridge.privatepass().encode())

    # ---- commandes ----------------------------------------------------------
    async def _swap_command(self, ctx, src: str, amount: str, address: str, options: str) -> None:
        force = "force=yes" in options.lower()
        try:
            plan = await self.prepare(src, amount, address, force)
        except (ToolError, sw.SwapError) as e:
            return await self.fail(ctx, e)
        ch = self.bot.get_channel(self.bridge.transaction_channel())
        await self.out(ctx, self.plan_card(plan) + "\n" + ui.wait(
            f"Type your private password in {ch.mention if ch else 'the transaction channel'} ({CONFIRM_TIMEOUT}s)"))
        try:
            confirmed = await self._confirm(ctx)
        except ToolError as e:
            return await self.fail(ctx, e)
        if not confirmed:
            return await self.out(ctx, ui.err("Swap cancelled", "Wrong password or timeout"))
        try:
            order = await self.execute(plan, str(ctx.author.id))
        except (sw.SwapError, ToolError) as e:
            return await self.fail(ctx, "Swap failed", str(e))
        await self.out(ctx, ui.ok(f"Swap sent · order `{order.order_id}`", f"{order.provider} · `{self.prefix}swapstatus {order.order_id}"
                                                                       + (f" {order.token}" if order.token else "") + "`"))

    @commands.command()
    async def swapltc(self, ctx, amount: str, address: str = "", *, options: str = ""):
        """Swap LTC → USDT (Polygon): `swapltc 0.5 [0x…address] [force=yes]` · `swapltc all`."""
        await self._swap_command(ctx, "ltc", amount, address, options)

    @commands.command()
    async def swapusdt(self, ctx, amount: str, address: str = "", *, options: str = ""):
        """Swap USDT (Polygon) → LTC: `swapusdt 20 [ltc address] [force=yes]` · `swapusdt all`."""
        await self._swap_command(ctx, "usdt", amount, address, options)

    @commands.command()
    async def swaprates(self, ctx, amount: str = "1", coin: str = "ltc"):
        """Compare swap quotes and their cost vs the market price: `swaprates 1 ltc` · `swaprates 50 usdt`."""
        try:
            src, dst = sw.direction(coin)
            value = float(amount.replace(",", "."))
            if value <= 0:
                raise ValueError
        except (ToolError, ValueError):
            return await self.fail(ctx, "Usage", f"`{self.prefix}swaprates <amount> <ltc|usdt>`")
        try:
            quotes = await self._quotes(src, dst, value)
            prices = await self._prices()
        except ToolError as e:
            return await self.fail(ctx, e)
        ideal = sw.market_out(src, dst, value, prices)
        rows = []
        for q in sorted(quotes, key=lambda q: -(q.amount_out or 0)):
            if q.amount_out is None:
                rows.append(f"> ⚠️ **{q.provider}** · unavailable ({q.note})")
                continue
            flag = "" if q.can_execute else " · compare-only"
            note = f" · {q.note}" if q.note and q.can_execute is False else ""
            rows.append(f"> **{q.provider}** · `{q.amount_out:.6f} {dst.label}` · `{q.spread:.2f}%` below market{flag}{note}")
        await self.out(ctx, ui.card(f"{value} {src.label} → {dst.label}", icon="💱", desc="\n".join(rows) or "No quote",
                                    footer=f"market ≈ {ideal:.6f} {dst.label} · instant quotes, they move"))

    @commands.command()
    async def swapstatus(self, ctx, order_id: str, token: str = ""):
        """Status of a swap order: `swapstatus <order id>` (Swapzone) or `swapstatus <id> <token>` (FixedFloat)."""
        name = "fixedfloat" if token else "swapzone"
        provider = next((p for p in self.bridge.providers() if p.name == name and p.can_execute), None)
        if provider is None:
            return await self.fail(ctx, f"{name} is not configured")
        try:
            async with aiohttp.ClientSession() as session:
                st = await provider.status(session, order_id, token or None)
        except sw.SwapError as e:
            return await self.fail(ctx, "Status unavailable", str(e))
        await self.out(ctx, ui.card("Swap status", [("Order", f"`{order_id}`"), ("Provider", name), ("Status", f"**{st['status']}**")]
                                    + ([("Receiving", f"`{st['amount']}` → `{st['to']}`")] if st.get("to") else []), icon="📦"))

    # ---- automatismes -------------------------------------------------------
    async def _auto(self, ctx, mode: str, price: str, amount: str, address: str) -> None:
        src_key = "ltc" if mode == "sell" else "usdt"
        key = f"auto{mode}"
        if key in self.autos and not self.autos[key].done():
            return await self.fail(ctx, f"{key} is already running", f"`{self.prefix}stop{key}` first")
        try:
            threshold = float(price.replace(",", "."))
            trig = sw.TrailingTrigger(mode, threshold)
            plan = await self.prepare(src_key, amount, address)            # valide tout dès maintenant
        except (ToolError, ValueError, sw.SwapError) as e:
            return await self.fail(ctx, e if not isinstance(e, ValueError) else "Prix invalide")
        ch = self.bot.get_channel(self.bridge.transaction_channel())
        await self.out(ctx, self.plan_card(plan) + "\n" + ui.info(f"Auto{mode}: threshold `{threshold}` USD",
                                                                  f"Type your password in {ch.mention if ch else 'the transaction channel'} ({CONFIRM_TIMEOUT}s)"))
        try:
            if not await self._confirm(ctx):
                return await self.out(ctx, ui.err(f"Auto{mode} cancelled", "Wrong password or timeout"))
        except ToolError as e:
            return await self.fail(ctx, e)
        self.autos[key] = asyncio.ensure_future(self._auto_loop(key, mode, trig, src_key, amount, address, ctx.channel.id))
        self._audit(f"auto{mode}_start", f"threshold={threshold} amount={amount}")
        await self.out(ctx, ui.ok(f"Auto{mode} started", f"Threshold `{threshold}` USD · checks every {POLL}s · stops after one swap"))

    async def _auto_loop(self, key: str, mode: str, trig: sw.TrailingTrigger, src_key: str, amount: str, address: str, channel_id: int) -> None:
        async def say(text: str) -> None:
            channel = self.bot.get_channel(channel_id)
            if channel is not None:
                try:
                    await channel.send(text)
                except discord.HTTPException:
                    pass
        try:
            while True:
                price = await self.bridge.price_usd("litecoin")
                if price and trig.update(float(price)):
                    try:
                        plan = await self.prepare(src_key, amount, address)       # revalide solde / frais maintenant
                        order = await self.execute(plan, "auto")
                        await say(ui.ok(f"Auto{mode} executed at `{price}` USD", f"Order `{order.order_id}` · {order.provider}"))
                    except (ToolError, sw.SwapError) as e:
                        await say(ui.err(f"Auto{mode} failed and stopped", str(e)))
                    finally:
                        self._audit(f"auto{mode}_end", f"price={price}")
                    return
                await asyncio.sleep(POLL)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Auto loop crashed")
            await say(ui.err(f"Auto{mode} crashed and stopped", "Details are in the logs"))

    @commands.command(name="autosell")
    async def autosell_cmd(self, ctx, price: str, amount: str, address: str = ""):
        """Sell LTC → USDT after the price (USD) passes the threshold and starts to fall: `autosell 45 all`."""
        await self._auto(ctx, "sell", price, amount, address)

    @commands.command(name="autobuy")
    async def autobuy_cmd(self, ctx, price: str, amount: str, address: str = ""):
        """Buy LTC with USDT after the price (USD) drops under the threshold and starts to rise: `autobuy 35 all`."""
        await self._auto(ctx, "buy", price, amount, address)

    async def _stop(self, ctx, key: str) -> None:
        task = self.autos.pop(key, None)
        if task and not task.done():
            task.cancel()
            self._audit(f"{key}_stop", "")
            return await self.out(ctx, ui.ok(f"{key.capitalize()} stopped"))
        await self.out(ctx, ui.warn(f"{key.capitalize()} is not running"))

    @commands.command()
    async def stopautosell(self, ctx):
        """Stop the automatic LTC sale."""
        await self._stop(ctx, "autosell")

    @commands.command()
    async def stopautobuy(self, ctx):
        """Stop the automatic LTC purchase."""
        await self._stop(ctx, "autobuy")

    @commands.command()
    async def swapauto(self, ctx):
        """Show the running autosell / autobuy."""
        rows = [f"`{k}` · running" for k, t in self.autos.items() if not t.done()]
        await self.out(ctx, ui.list_card("Auto swaps", rows, icon="🤖", empty="none running"))


async def setup(bot, bridge: SwapBridge, db_getter=lambda: None, prefix: str = ".") -> SwapCog:
    cog = SwapCog(bot, bridge, db_getter, prefix)
    await bot.add_cog(cog)
    return cog


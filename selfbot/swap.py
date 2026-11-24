"""Swap LTC <-> USDT (Polygon) via des services d'échange instantané.

Fournisseurs:
* Swapzone   : agrégateur (clé API) - création d'ordres, comme dans l'ancien bot
* FixedFloat : clé + secret API (signature HMAC-SHA256) - création d'ordres, comme dans l'ancien bot
* SideShift  : API publique de cotation (sans clé) - sert uniquement à comparer les frais

Tout ce qui touche à l'argent est volontairement strict: adresses validées (destination ET adresse de
dépôt renvoyée par l'API), frais maximum par rapport au prix du marché, montant jamais deviné.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import aiohttp

from .toolkit import validate_ltc_address
from .tools import ToolError

TIMEOUT = aiohttp.ClientTimeout(total=20)
EVM_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


class SwapError(Exception):
    """Erreur de swap: le message est affichable tel quel (jamais de clé ni de secret)."""


def validate_evm_address(addr: str) -> str:
    addr = (addr or "").strip()
    if not EVM_RE.match(addr):
        raise ToolError("Adresse Polygon invalide (0x + 40 caractères hexadécimaux)")
    return addr


@dataclass(frozen=True)
class Coin:
    key: str
    label: str
    swapzone: str
    fixedfloat: str
    sideshift: str
    decimals: int

    def validate(self, addr: str) -> str:
        return validate_ltc_address(addr) if self.key == "ltc" else validate_evm_address(addr)


COINS = {
    "ltc": Coin("ltc", "LTC", "ltc", "LTC", "ltc-mainnet", 8),
    "usdt": Coin("usdt", "USDT (Polygon)", "usdtmatic", "USDTMATIC", "usdt-polygon", 6),
}


def direction(text: str) -> tuple:
    """``ltc`` -> (ltc, usdt) ; ``usdt`` -> (usdt, ltc)."""
    key = (text or "").lower()
    if key not in COINS:
        raise ToolError("Monnaie inconnue (ltc ou usdt)")
    other = "usdt" if key == "ltc" else "ltc"
    return COINS[key], COINS[other]


@dataclass
class Quote:
    provider: str
    amount_in: float
    amount_out: Optional[float]
    can_execute: bool = False
    quote_id: Optional[str] = None
    note: str = ""
    spread: Optional[float] = None       # % sous le prix du marché (renseigné par ``with_spread``)


@dataclass
class Order:
    provider: str
    order_id: str
    deposit_address: str
    amount_in: float
    amount_out: Optional[float] = None
    token: Optional[str] = None


def market_out(src: Coin, dst: Coin, amount_in: float, prices: dict) -> float:
    """Montant théorique reçu au prix du marché (``prices``: {'ltc': usd, 'usdt': usd})."""
    return amount_in * prices[src.key] / prices[dst.key]


def with_spread(q: Quote, src: Coin, dst: Coin, prices: dict) -> Quote:
    if q.amount_out is not None:
        ideal = market_out(src, dst, q.amount_in, prices)
        q.spread = round((1 - q.amount_out / ideal) * 100, 3) if ideal > 0 else None
    return q


def pick_best(quotes: list) -> Optional[Quote]:
    """Meilleur devis parmi ceux exécutables dont le montant reçu est connu."""
    ok = [q for q in quotes if q.can_execute and q.amount_out]
    return max(ok, key=lambda q: q.amount_out) if ok else None


def check_fee(q: Quote, max_fee_pct: float) -> None:
    """Refuse un swap plus cher que ``max_fee_pct`` % (ou dont le coût est inconnu)."""
    if q.spread is None:
        raise SwapError(f"{q.provider}: coût du swap inconnu, ajoutez `force=yes` pour continuer malgré tout")
    if q.spread > max_fee_pct:
        raise SwapError(f"{q.provider}: {q.spread:.2f}% sous le marché, au-delà de la limite de {max_fee_pct:.2f}%")


def check_order(order: Order, src: Coin, expected_in: float) -> None:
    """Vérifie la réponse de création AVANT d'envoyer des fonds."""
    try:
        src.validate(order.deposit_address)
    except ToolError:
        raise SwapError(f"{order.provider}: adresse de dépôt invalide dans la réponse, aucun fonds envoyé") from None
    if abs(order.amount_in - expected_in) > 1e-9:
        raise SwapError(f"{order.provider}: montant incohérent, aucun fonds envoyé")


# --------------------------------------------------------------------------- #
# Déclencheur « trailing » des autosell / autobuy
# --------------------------------------------------------------------------- #
class TrailingTrigger:
    """Vente (``sell``): armé quand prix >= seuil, déclenche à la première baisse depuis le plus haut.
    Achat (``buy``): armé quand prix <= seuil, déclenche à la première hausse depuis le plus bas."""

    def __init__(self, mode: str, threshold: float):
        if mode not in ("sell", "buy") or threshold <= 0:
            raise ToolError("Paramètres invalides")
        self.mode, self.threshold = mode, threshold
        self.extreme: Optional[float] = None

    def update(self, price: float) -> bool:
        if self.extreme is None:
            armed = price >= self.threshold if self.mode == "sell" else price <= self.threshold
            if armed:
                self.extreme = price
            return False
        if self.mode == "sell":
            if price < self.extreme:
                return True
            self.extreme = price
        else:
            if price > self.extreme:
                return True
            self.extreme = price
        return False


# --------------------------------------------------------------------------- #
# Fournisseurs
# --------------------------------------------------------------------------- #
def _num(value: Any) -> Optional[float]:
    try:
        v = float(value)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


async def _call(session: aiohttp.ClientSession, method: str, url: str, **kw) -> Any:
    try:
        async with session.request(method, url, timeout=TIMEOUT, **kw) as r:
            text = await r.text()
            if r.status != 200:
                raise SwapError(f"HTTP {r.status} ({url.split('?')[0].rsplit('/', 1)[-1]}): {text[:150]}")
            try:
                return json.loads(text)
            except ValueError:
                raise SwapError("Réponse non JSON du fournisseur") from None
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise SwapError(f"Fournisseur injoignable ({type(exc).__name__})") from None


@dataclass
class Swapzone:
    api_key: str
    base: str = "https://api.swapzone.io/v1"
    name: str = field(default="swapzone", init=False)

    @property
    def can_execute(self) -> bool:
        return bool(self.api_key)

    def _h(self) -> dict:
        return {"x-api-key": self.api_key}

    async def quote(self, session, src: Coin, dst: Coin, amount: float) -> Quote:
        data = await _call(session, "GET", f"{self.base}/exchange/get-rate", headers=self._h(), params={
            "chooseRate": "all", "timeout": "1600", "from": src.swapzone, "to": dst.swapzone, "rateType": "all", "amount": amount})
        if not isinstance(data, list) or not data:
            raise SwapError("swapzone: aucun devis")
        best = None
        for d in data:
            if not isinstance(d, dict) or not d.get("quotaId"):
                continue
            out = next((_num(d[k]) for k in ("amountTo", "amount_to", "toAmount", "receiveAmount") if k in d), None)
            if best is None or (out or 0) > (best[1] or 0):
                best = (d, out)
        if best is None:
            raise SwapError("swapzone: devis inexploitable")
        d, out = best
        return Quote(self.name, amount, out, True, str(d["quotaId"]), note=str(d.get("adapter", "")))

    async def create(self, session, src: Coin, dst: Coin, amount: float, to_address: str, refund_address: str,
                     quote: Quote) -> Order:
        data = await _call(session, "POST", f"{self.base}/exchange/create", headers=self._h(), json={
            "to": dst.swapzone, "from": src.swapzone, "quotaId": quote.quote_id, "email": "",
            "amountDeposit": amount, "addressReceive": to_address, "refundAddress": refund_address})
        try:
            tx = data["transaction"]
            return Order(self.name, str(tx["id"]), str(tx["addressDeposit"]), amount, quote.amount_out)
        except (KeyError, TypeError):
            raise SwapError("swapzone: réponse de création inattendue") from None

    async def status(self, session, order_id: str, token: Optional[str] = None) -> dict:
        data = await _call(session, "GET", f"{self.base}/exchange/tx", headers=self._h(), params={"id": order_id})
        tx = data.get("transaction", data) if isinstance(data, dict) else {}
        return {"status": str(tx.get("status", "unknown")), "to": tx.get("addressReceive"), "amount": tx.get("amountReceive")}


@dataclass
class FixedFloat:
    api_key: str
    api_secret: str
    base: str = "https://ff.io/api/v2"
    name: str = field(default="fixedfloat", init=False)

    @property
    def can_execute(self) -> bool:
        return bool(self.api_key and self.api_secret)

    async def _post(self, session, path: str, payload: dict) -> dict:
        body = json.dumps(payload)                         # les octets signés sont exactement ceux envoyés
        sig = hmac.new(self.api_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        data = await _call(session, "POST", f"{self.base}/{path}", data=body, headers={
            "Accept": "application/json", "X-API-KEY": self.api_key, "X-API-SIGN": sig,
            "Content-Type": "application/json; charset=UTF-8"})
        if not isinstance(data, dict) or data.get("code") != 0:
            raise SwapError(f"fixedfloat: {str(data.get('msg') if isinstance(data, dict) else data)[:150]}")
        return data["data"]

    async def quote(self, session, src: Coin, dst: Coin, amount: float) -> Quote:
        d = await self._post(session, "price", {"fromCcy": src.fixedfloat, "toCcy": dst.fixedfloat, "amount": amount,
                                                "direction": "from", "type": "float"})
        return Quote(self.name, amount, _num(d.get("to", {}).get("amount")), True)

    async def create(self, session, src: Coin, dst: Coin, amount: float, to_address: str, refund_address: str,
                     quote: Quote) -> Order:
        d = await self._post(session, "create", {"fromCcy": src.fixedfloat, "toCcy": dst.fixedfloat, "amount": amount,
                                                 "direction": "from", "type": "float", "refundAddress": refund_address,
                                                 "toAddress": to_address})
        try:
            return Order(self.name, str(d["id"]), str(d["from"]["address"]), amount, _num(d.get("to", {}).get("amount")), str(d["token"]))
        except (KeyError, TypeError):
            raise SwapError("fixedfloat: réponse de création inattendue") from None

    async def status(self, session, order_id: str, token: Optional[str] = None) -> dict:
        if not token:
            raise SwapError("fixedfloat: le token de l'ordre est requis")
        d = await self._post(session, "order", {"id": order_id, "token": token})
        return {"status": str(d.get("status", "unknown")), "to": (d.get("to") or {}).get("address"), "amount": (d.get("to") or {}).get("amount")}


@dataclass
class SideShift:
    """Cotation publique uniquement (aucun ordre): permet de comparer les frais sans clé."""
    base: str = "https://sideshift.ai/api/v2"
    name: str = field(default="sideshift", init=False)
    can_execute: bool = field(default=False, init=False)

    async def quote(self, session, src: Coin, dst: Coin, amount: float) -> Quote:
        data = await _call(session, "GET", f"{self.base}/pair/{src.sideshift}/{dst.sideshift}", params={"amount": amount})
        rate = _num(data.get("rate")) if isinstance(data, dict) else None
        if rate is None:
            raise SwapError("sideshift: taux indisponible")
        lo, hi = _num(data.get("min")), _num(data.get("max"))
        note = ""
        if lo and amount < lo:
            note = f"min {lo}"
        elif hi and amount > hi:
            note = f"max {hi}"
        return Quote(self.name, amount, amount * rate, False, note=note)


def build_providers(swapzone_key: str = "", ff_key: str = "", ff_secret: str = "") -> list:
    providers: list = []
    if swapzone_key:
        providers.append(Swapzone(swapzone_key))
    if ff_key and ff_secret:
        providers.append(FixedFloat(ff_key, ff_secret))
    providers.append(SideShift())
    return providers

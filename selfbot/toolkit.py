"""Fonctions pures de la 2e vague de commandes (alias, stats, crypto, config...). Sans Discord ni réseau."""
from __future__ import annotations

import re
import time
from typing import Any, Optional

from .tools import ToolError, trunc

# --------------------------------------------------------------------------- #
# Alias / snippets
# --------------------------------------------------------------------------- #
NAME_RE = re.compile(r"^[a-z0-9_-]{1,24}$")


def check_name(name: str) -> str:
    name = (name or "").lower()
    if not NAME_RE.match(name):
        raise ToolError("Nom invalide (1-24 caractères: a-z, 0-9, _ et -)")
    return name


def expand_alias(content: str, prefix: str, aliases: dict) -> str:
    """``.p btc`` avec l'alias ``p -> price`` donne ``.price btc``. Une seule expansion (pas de récursion)."""
    if not aliases or not content.startswith(prefix):
        return content
    head, _, rest = content[len(prefix):].partition(" ")
    target = aliases.get(head.lower())
    if target is None:
        return content
    return f"{prefix}{target}{(' ' + rest) if rest else ''}"


# --------------------------------------------------------------------------- #
# Statistiques du sniper
# --------------------------------------------------------------------------- #
class Stats:
    """Compteurs (snipes, auto-buys...) par salon, persistés dans le kv SQLite."""
    KEY = "stats"

    def __init__(self, db_getter):
        self._db = db_getter

    def _load(self) -> dict:
        db = self._db()
        return db.get(self.KEY, {}) if db else {}

    def record(self, kind: str, channel_id: int, latency: Optional[float] = None, now: Optional[float] = None) -> None:
        db = self._db()
        if not db:
            return
        data = self._load()
        k = data.setdefault(kind, {"count": 0, "lat_sum": 0.0, "lat_n": 0, "channels": {}, "last": 0})
        k["count"] += 1
        k["last"] = now if now is not None else time.time()
        k["channels"][str(channel_id)] = k["channels"].get(str(channel_id), 0) + 1
        if latency is not None and 0 <= latency < 3600:
            k["lat_sum"] += latency
            k["lat_n"] += 1
        db.set(self.KEY, data)

    def summary(self) -> dict:
        out = {}
        for kind, k in self._load().items():
            top = sorted(k["channels"].items(), key=lambda kv: -kv[1])[:5]
            out[kind] = {"count": k["count"], "last": k["last"], "top": [(int(c), n) for c, n in top],
                         "avg_latency": (k["lat_sum"] / k["lat_n"]) if k["lat_n"] else None}
        return out

    def reset(self) -> None:
        db = self._db()
        if db:
            db.set(self.KEY, {})


# --------------------------------------------------------------------------- #
# Crypto
# --------------------------------------------------------------------------- #
LTC_RE = re.compile(r"^(?:[LM3][a-km-zA-HJ-NP-Z1-9]{25,34}|ltc1[02-9ac-hj-np-z]{20,90})$")


def validate_ltc_address(addr: str) -> str:
    addr = (addr or "").strip()
    if not LTC_RE.match(addr):
        raise ToolError("Adresse Litecoin invalide")
    return addr


def estimate_fee(fee_per_kb: float, size_bytes: int = 225) -> float:
    """Frais en LTC pour une transaction de ``size_bytes`` (225 octets: 1 entrée, 2 sorties)."""
    return fee_per_kb * size_bytes / 1000 / 1e8


SYMBOLS = {"btc": "bitcoin", "ltc": "litecoin", "eth": "ethereum", "usdt": "tether", "sol": "solana",
           "xrp": "ripple", "doge": "dogecoin", "bnb": "binancecoin", "ada": "cardano", "trx": "tron",
           "matic": "matic-network", "dot": "polkadot", "xmr": "monero"}


def coin_id(symbol: str) -> str:
    s = (symbol or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9-]{2,40}", s):
        raise ToolError("Symbole invalide")
    return SYMBOLS.get(s, s)


def parse_alert(args: list) -> tuple:
    """``btc above 60000`` -> (id coingecko, 'above', 60000.0)."""
    if len(args) != 3 or args[1].lower() not in ("above", "below", ">", "<"):
        raise ToolError("Format: `price alert <symbole> above|below <prix en €>`")
    try:
        value = float(args[2].replace(",", "."))
    except ValueError:
        raise ToolError("Prix invalide") from None
    if value <= 0:
        raise ToolError("Le prix doit être positif")
    return coin_id(args[0]), "above" if args[1] in ("above", ">") else "below", value


def triggered_alerts(alerts: list, prices: dict) -> list:
    out = []
    for a in alerts:
        p = prices.get(a["coin"])
        if p is not None and ((a["dir"] == "above" and p >= a["value"]) or (a["dir"] == "below" and p <= a["value"])):
            out.append({**a, "price": p})
    return out


# --------------------------------------------------------------------------- #
# Divers
# --------------------------------------------------------------------------- #
LANG_RE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z]{2,4})?$")


def check_lang(code: str) -> str:
    if not LANG_RE.match(code or ""):
        raise ToolError("Code langue invalide (ex: `fr`, `en`, `de`, `zh-CN`)")
    return code


def parse_translation(data: Any) -> tuple:
    """Réponse translate_a/single -> (texte traduit, langue source détectée)."""
    try:
        return "".join(seg[0] for seg in data[0] if seg and seg[0]), str(data[2])
    except (TypeError, IndexError, KeyError):
        raise ToolError("Réponse de traduction illisible") from None


def parse_gift_code(text: str) -> str:
    m = re.search(r"(?:discord\.gift/|discord(?:app)?\.com/gifts/)?([A-Za-z0-9]{16,24})\b", text or "")
    if not m:
        raise ToolError("Code cadeau introuvable (16 à 24 caractères)")
    return m.group(1)


NUM_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]


def parse_poll(text: str) -> tuple:
    parts = [p.strip() for p in (text or "").split("|") if p.strip()]
    if len(parts) < 3:
        raise ToolError("Format: `poll question | choix 1 | choix 2 [| …]` (2 à 9 choix)")
    if len(parts) - 1 > 9:
        raise ToolError("9 choix maximum")
    return parts[0], parts[1:]


def valid_timezone(zone: str) -> str:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ToolError(f"Fuseau inconnu `{trunc(zone, 40)}` (ex: `Europe/Paris`)") from None
    return zone


EMOJI_RE = re.compile(r"^(?:<a?:\w{2,32}:\d{15,21}>|[^\w\s<>]{1,8})$")


def check_emoji(text: str) -> str:
    if not EMOJI_RE.match(text or ""):
        raise ToolError("Emoji invalide (Unicode ou `<:nom:id>`)")
    return text


# --------------------------------------------------------------------------- #
# Export / import de configuration (sans aucun secret)
# --------------------------------------------------------------------------- #
CONFIG_VERSION = 1
_ID_LISTS = ("stock_channels", "snipe_channels", "ticket_channels")
_STR_LISTS = ("keywords", "exclude_keywords", "labels_check")


def validate_config(data: Any) -> dict:
    """Valide un export (types stricts, tailles bornées) et renvoie un dict nettoyé."""
    if not isinstance(data, dict) or data.get("version") != CONFIG_VERSION:
        raise ToolError("Fichier de configuration invalide ou de version inconnue")
    clean: dict = {"version": CONFIG_VERSION}
    for key in _ID_LISTS:
        vals = data.get(key, [])
        if (not isinstance(vals, list) or len(vals) > 5000
                or not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in vals)):
            raise ToolError(f"`{key}` doit être une liste d'identifiants")
        clean[key] = sorted(set(vals))
    for key in _STR_LISTS:
        vals = data.get(key, [])
        if not isinstance(vals, list) or len(vals) > 1000 or not all(isinstance(v, str) and 0 < len(v) <= 100 for v in vals):
            raise ToolError(f"`{key}` doit être une liste de textes")
        clean[key] = sorted({v.strip().lower() for v in vals if v.strip()})
    mp = data.get("max_price")
    if mp is not None and (isinstance(mp, bool) or not isinstance(mp, (int, float)) or not 0 <= mp <= 1e6):
        raise ToolError("`max_price` invalide")
    clean["max_price"] = mp
    if data.get("auto_mode") not in (None, "dm", "ticket"):
        raise ToolError("`auto_mode` doit valoir dm ou ticket")
    clean["auto_mode"] = data.get("auto_mode")
    sd = data.get("snipe_delay")
    if sd is not None and (isinstance(sd, bool) or not isinstance(sd, (int, float)) or not 0 <= sd <= 3600):
        raise ToolError("`snipe_delay` invalide")
    clean["snipe_delay"] = sd
    return clean

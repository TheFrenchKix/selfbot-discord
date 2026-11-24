"""Fonctions pures des commandes utilitaires (sans Discord ni réseau): testables isolément."""
from __future__ import annotations

import base64
import binascii
import colorsys
import hashlib
import random
import re
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone
from typing import Any, Optional

DISCORD_EPOCH = 1420070400000
_DUR = re.compile(r"(\d+(?:\.\d+)?)\s*([smhdw])", re.I)
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
MAX_DURATION = 365 * 86400


class ToolError(Exception):
    """Entrée invalide: le message est affiché tel quel à l'utilisateur."""


def parse_duration(text: str) -> int:
    """``1h30m`` / ``90s`` / ``2d`` / ``45`` (minutes) -> secondes."""
    text = (text or "").strip().lower()
    if not text:
        raise ToolError("Durée manquante (ex: `10m`, `1h30m`, `2d`)")
    if text.isdigit():
        total = int(text) * 60
    else:
        if not re.fullmatch(r"(?:\d+(?:\.\d+)?\s*[smhdw]\s*)+", text):
            raise ToolError(f"Durée invalide `{text}` (ex: `10m`, `1h30m`, `2d`)")
        total = int(sum(float(n) * _UNITS[u.lower()] for n, u in _DUR.findall(text)))
    if total <= 0 or total > MAX_DURATION:
        raise ToolError("La durée doit être comprise entre 1 seconde et 365 jours")
    return total


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    parts = []
    for label, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        n, seconds = divmod(seconds, size)
        if n:
            parts.append(f"{n}{label}")
    return " ".join(parts[:3]) or "0s"


def snowflake_time(snowflake: int) -> datetime:
    return datetime.fromtimestamp(((int(snowflake) >> 22) + DISCORD_EPOCH) / 1000, tz=timezone.utc)


def parse_snowflake(text: str) -> int:
    m = re.search(r"\d{15,21}", text or "")
    if not m:
        raise ToolError("ID Discord invalide (15 à 21 chiffres)")
    return int(m.group())


def ts(dt_or_epoch: Any, style: str = "f") -> str:
    """Horodatage Discord ``<t:…:style>`` (rendu dans le fuseau du lecteur)."""
    epoch = dt_or_epoch.timestamp() if hasattr(dt_or_epoch, "timestamp") else float(dt_or_epoch)
    return f"<t:{int(epoch)}:{style}>"


# --------------------------------------------------------------------------- #
# Snipe (messages supprimés / modifiés)
# --------------------------------------------------------------------------- #
class SnipeStore:
    """Garde les N derniers événements par salon, avec une limite globale de salons (LRU)."""

    def __init__(self, per_channel: int = 10, max_channels: int = 200, ttl: float = 3600):
        self.per_channel, self.max_channels, self.ttl = per_channel, max_channels, ttl
        self._data: "OrderedDict[int, deque]" = OrderedDict()

    def add(self, channel_id: int, entry: dict, now: Optional[float] = None) -> None:
        entry = {**entry, "at": now if now is not None else time.time()}
        dq = self._data.pop(channel_id, None) or deque(maxlen=self.per_channel)
        dq.append(entry)
        self._data[channel_id] = dq
        while len(self._data) > self.max_channels:
            self._data.popitem(last=False)

    def get(self, channel_id: int, index: int = 1, now: Optional[float] = None) -> Optional[dict]:
        """``index`` 1 = le plus récent. None si absent ou expiré."""
        now = now if now is not None else time.time()
        live = [e for e in self._data.get(channel_id, ()) if now - e["at"] <= self.ttl]
        return live[-index] if 0 < index <= len(live) else None


# --------------------------------------------------------------------------- #
# AFK
# --------------------------------------------------------------------------- #
class AfkState:
    def __init__(self, cooldown: float = 600):
        self.cooldown = cooldown
        self.reason: Optional[str] = None
        self.since: float = 0
        self._replied: dict[int, float] = {}

    @property
    def active(self) -> bool:
        return self.reason is not None

    def enable(self, reason: str, now: Optional[float] = None) -> None:
        self.reason, self.since, self._replied = reason or "AFK", now if now is not None else time.time(), {}

    def disable(self) -> float:
        elapsed = time.time() - self.since if self.active else 0
        self.reason, self._replied = None, {}
        return elapsed

    def should_reply(self, user_id: int, now: Optional[float] = None) -> bool:
        """Au plus une réponse par utilisateur et par ``cooldown`` (évite les boucles/spam)."""
        now = now if now is not None else time.time()
        if not self.active or now - self._replied.get(user_id, -1e12) < self.cooldown:
            return False
        self._replied[user_id] = now
        return True


# --------------------------------------------------------------------------- #
# Rappels (persistés par l'appelant: liste de dicts JSON)
# --------------------------------------------------------------------------- #
MAX_REMINDERS = 50


def add_reminder(reminders: list[dict], when: float, text: str, channel_id: int, now: Optional[float] = None) -> dict:
    if len(reminders) >= MAX_REMINDERS:
        raise ToolError(f"Maximum {MAX_REMINDERS} rappels actifs")
    nid = max((r["id"] for r in reminders), default=0) + 1
    rem = {"id": nid, "at": when, "text": text[:500], "channel": channel_id}
    reminders.append(rem)
    reminders.sort(key=lambda r: r["at"])
    return rem


def due_reminders(reminders: list[dict], now: Optional[float] = None) -> list[dict]:
    now = now if now is not None else time.time()
    return [r for r in reminders if r["at"] <= now]


# --------------------------------------------------------------------------- #
# Divers
# --------------------------------------------------------------------------- #
def parse_color(text: str) -> tuple[int, int, int]:
    t = (text or "").strip().lstrip("#").lower()
    if t.startswith("0x"):
        t = t[2:]
    if re.fullmatch(r"[0-9a-f]{3}", t):
        t = "".join(c * 2 for c in t)
    if not re.fullmatch(r"[0-9a-f]{6}", t):
        raise ToolError("Couleur invalide (ex: `#ff8800`, `f80`)")
    return int(t[0:2], 16), int(t[2:4], 16), int(t[4:6], 16)


def color_info(text: str) -> dict:
    r, g, b = parse_color(text)
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    return {"hex": f"#{r:02x}{g:02x}{b:02x}", "rgb": (r, g, b), "int": (r << 16) | (g << 8) | b,
            "hsl": (round(h * 360), round(s * 100), round(l * 100))}


def roll_dice(spec: str, rng: Optional[random.Random] = None) -> tuple[list[int], int]:
    """``2d6`` / ``d20`` / ``3d8+2`` -> (jets, total)."""
    m = re.fullmatch(r"\s*(\d*)d(\d+)\s*([+-]\s*\d+)?\s*", (spec or "").lower())
    if not m:
        raise ToolError("Format invalide (ex: `d20`, `2d6`, `3d8+2`)")
    n, sides, mod = int(m.group(1) or 1), int(m.group(2)), int((m.group(3) or "0").replace(" ", ""))
    if not 1 <= n <= 50 or not 2 <= sides <= 1000:
        raise ToolError("1 à 50 dés, 2 à 1000 faces")
    rng = rng or random.SystemRandom()
    rolls = [rng.randint(1, sides) for _ in range(n)]
    return rolls, sum(rolls) + mod


HASHES = ("md5", "sha1", "sha256", "sha512")


def hash_text(algo: str, text: str) -> str:
    algo = algo.lower().replace("-", "")
    if algo not in HASHES:
        raise ToolError(f"Algorithme inconnu (disponibles: {', '.join(HASHES)})")
    return hashlib.new(algo, text.encode()).hexdigest()


def b64(mode: str, text: str) -> str:
    try:
        if mode in ("encode", "enc", "e"):
            return base64.b64encode(text.encode()).decode()
        if mode in ("decode", "dec", "d"):
            return base64.b64decode(text.encode(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        raise ToolError("Base64 invalide") from None
    raise ToolError("Mode attendu: `encode` ou `decode`")


def trunc(text: str, n: int = 1000) -> str:
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def neutralize_mentions(text: str) -> str:
    """Empêche @everyone/@here et les mentions de rôle/utilisateur dans un texte relayé."""
    text = text.replace("@everyone", "@​everyone").replace("@here", "@​here")
    return re.sub(r"<@([!&]?\d+)>", lambda m: f"<@​{m.group(1)}>", text)

"""Notifications des instances vers un webhook Discord (démarrage, arrêt, crash...).

* l'URL du webhook est un secret: stockée chiffrée (DPAPI / Fernet), toujours masquée à l'affichage
* ``notify()`` est thread-safe et non bloquant (appelable depuis la supervision)
* envoi asynchrone avec file d'attente, relance sur 429 / erreurs réseau, jamais d'exception remontée
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from .logging_setup import register_secret
from .secrets import SecretsError, decrypt, encrypt

log = logging.getLogger("selfbot.webhook")

WEBHOOK_RE = re.compile(
    r"^https://(?:(?:canary|ptb)\.)?(?:discord|discordapp)\.com/api(?:/v\d+)?/webhooks/(\d{15,25})/([\w-]{20,})/?$"
)

COLORS = {"green": 0x2ECC71, "red": 0xE74C3C, "orange": 0xE67E22, "blue": 0x3498DB, "grey": 0x95A5A6, "yellow": 0xF1C40F}
# type d'événement -> (icône, titre, couleur)
EVENTS = {
    "added": ("➕", "Instance added", "blue"),
    "removed": ("🗑️", "Instance removed", "grey"),
    "started": ("▶️", "Instance started", "green"),
    "restarted": ("🔄", "Instance restarted", "blue"),
    "relaunched": ("♻️", "Instance relaunched after a crash", "yellow"),
    "stopped": ("⏹️", "Instance stopped", "grey"),
    "crashed": ("💥", "Instance crashed", "red"),
    "fatal": ("⛔", "Fatal error: not restarted", "red"),
    "stale": ("🫀", "Heartbeat lost: restarting", "orange"),
    "test": ("🧪", "Webhook test", "blue"),
}


class WebhookError(Exception):
    pass


def validate_url(url: str) -> str:
    url = (url or "").strip()
    if not WEBHOOK_RE.match(url):
        raise WebhookError("URL invalide: attendu https://discord.com/api/webhooks/<id>/<token>")
    return url.rstrip("/")


def mask_url(url: Optional[str]) -> str:
    if not url:
        return "<aucun>"
    m = WEBHOOK_RE.match(url)
    if not m:
        return "***"
    wid, token = m.groups()
    return f"https://discord.com/api/webhooks/{wid}/{token[:3]}...{token[-3:]}"


def format_event(event: dict) -> dict:
    """Charge utile du webhook (embed) pour un événement du manager."""
    icon, title, color = EVENTS.get(event["type"], ("ℹ️", event["type"], "grey"))
    fields = [{"name": "Instance", "value": f"`{event['instance']}`", "inline": True}]

    def add(name: str, value: Any, inline: bool = True) -> None:
        if value not in (None, ""):
            fields.append({"name": name, "value": str(value)[:1000], "inline": inline})

    add("PID", f"`{event['pid']}`" if event.get("pid") else None)
    if "code" in event and event["code"] is not None:
        add("Exit code", f"`{event['code']}`")
    if event.get("uptime"):
        add("Uptime", f"`{_fmt_duration(event['uptime'])}`")
    if event.get("retry_in") is not None:
        add("Retry in", f"`{event['retry_in']}s`")
    if event.get("restarts"):
        add("Restarts", f"`{event['restarts']}`")
    if event.get("age") is not None:
        add("Heartbeat age", f"`{event['age']}s`")
    if event.get("actor") and event["actor"] != "system":
        add("By", f"<@{event['actor']}>" if str(event["actor"]).isdigit() else event["actor"])
    tail = event.get("tail")
    if tail:
        safe = tail.replace("```", "'''")[-900:]
        add("Last log lines", f"```{safe}```", inline=False)

    return {
        "username": "Selfbot controller",
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "title": f"{icon} {title}",
            "color": COLORS[color],
            "fields": fields,
            "timestamp": datetime.fromtimestamp(event.get("ts", 0) or 0, tz=timezone.utc).isoformat(),
        }],
    }


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return (f"{h}h" if h else "") + (f"{m}m" if m or h else "") + f"{s}s"


async def post_webhook(session: aiohttp.ClientSession, url: str, payload: dict, retries: int = 3) -> bool:
    """POST avec gestion du rate limit (429 + retry_after). Retourne True si livré."""
    for attempt in range(retries + 1):
        try:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status in (200, 204):
                    return True
                if resp.status == 429:
                    try:
                        delay = float((await resp.json()).get("retry_after", 1))
                    except Exception:
                        delay = float(resp.headers.get("Retry-After", 1))
                    await asyncio.sleep(min(delay, 30))
                    continue
                if resp.status in (401, 403, 404):
                    log.error("Webhook refusé (HTTP %s): il a probablement été supprimé", resp.status)
                    return False
                log.warning("Webhook HTTP %s (tentative %d)", resp.status, attempt + 1)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("Webhook injoignable (%s), tentative %d", type(exc).__name__, attempt + 1)
        await asyncio.sleep(min(2 ** attempt, 8))
    return False


Sender = Callable[[str, dict], Awaitable[bool]]


class WebhookNotifier:
    def __init__(self, path: str | Path, backend: Optional[str] = None, key: Optional[str] = None,
                 sender: Optional[Sender] = None):
        self.path = Path(path)
        self.backend, self.key = backend, key
        self._sender = sender
        self._session: Optional[aiohttp.ClientSession] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional[asyncio.Queue] = None
        self._task: Optional[asyncio.Task] = None
        self._early: list[dict] = []           # événements reçus avant le démarrage de la boucle
        self._lock = threading.Lock()
        self._url: Optional[str] = None
        self._load()

    # ---- stockage chiffré ------------------------------------------------
    def _load(self) -> None:
        try:
            blob = json.loads(self.path.read_text(encoding="utf-8")).get("url")
            self._url = decrypt(blob, self.key) if blob else None
        except FileNotFoundError:
            self._url = None
        except (OSError, json.JSONDecodeError, SecretsError) as exc:
            log.error("Webhook illisible (%s): ignoré", exc)
            self._url = None
        if self._url:
            register_secret(self._url)

    def set_url(self, url: str) -> str:
        url = validate_url(url)
        register_secret(url)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"url": encrypt(url, self.backend, self.key)}), encoding="utf-8")
        tmp.replace(self.path)
        with self._lock:
            self._url = url
        return mask_url(url)

    def clear(self) -> bool:
        with self._lock:
            had, self._url = self._url is not None, None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        return had

    @property
    def configured(self) -> bool:
        return self._url is not None

    @property
    def masked(self) -> str:
        return mask_url(self._url)

    # ---- envoi -----------------------------------------------------------
    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """À appeler depuis la boucle asyncio (ex: on_ready)."""
        if self._task and not self._task.done():
            return
        self._loop = loop or asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=200)
        self._task = self._loop.create_task(self._worker())
        for ev in self._early:
            self._queue.put_nowait(ev)
        self._early.clear()

    def notify(self, event: dict) -> None:
        """Thread-safe, non bloquant. Ignoré si aucun webhook n'est configuré."""
        if not self.configured:
            return
        loop, queue = self._loop, self._queue
        if loop is None or queue is None:
            if len(self._early) < 100:
                self._early.append(event)
            return

        def put() -> None:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("File de notifications pleine: événement %s ignoré", event.get("type"))

        try:
            loop.call_soon_threadsafe(put)
        except RuntimeError:  # boucle fermée
            pass

    async def _send(self, url: str, payload: dict) -> bool:
        if self._sender is not None:
            return await self._sender(url, payload)
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return await post_webhook(self._session, url, payload)

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            event = await self._queue.get()
            try:
                url = self._url
                if url:
                    await self._send(url, format_event(event))
                    await asyncio.sleep(0.4)  # reste sous la limite (~5 req / 2 s) du webhook
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Échec d'envoi de notification")
            finally:
                self._queue.task_done()

    async def send_test(self) -> bool:
        url = self._url
        if not url:
            raise WebhookError("Aucun webhook configuré")
        import time
        return await self._send(url, format_event({"type": "test", "instance": "controller", "ts": time.time()}))

    async def flush(self, timeout: float = 5.0) -> None:
        """Attend que la file soit vidée (arrêt propre)."""
        if self._queue is None:
            return
        await asyncio.sleep(0)  # laisse passer les call_soon_threadsafe en attente
        try:
            await asyncio.wait_for(self._queue.join(), timeout)
        except asyncio.TimeoutError:
            log.warning("Notifications non envoyées avant l'arrêt")

    async def close(self) -> None:
        await self.flush()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._session and not self._session.closed:
            await self._session.close()

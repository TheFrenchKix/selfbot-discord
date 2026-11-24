"""Gestionnaire d'instances: un sous-processus isolé par compte.

* le token est passé au worker par variable d'environnement (jamais en argument)
* un crash d'une instance n'a aucun effet sur les autres
* redémarrage automatique avec backoff exponentiel
* rate limiting par compte, refus des tokens dupliqués, audit des actions
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from .config import mask_token
from .logging_setup import redact, register_secret
from .secrets import SecretsError, TokenStore
from .storage.db import Database

log = logging.getLogger("selfbot.manager")

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
TOKEN_RE = re.compile(r"^[\w-]{20,}\.[\w-]{5,}\.[\w-]{20,}$")
# Variables jamais transmises aux workers (secrets du contrôleur / du coffre).
_ENV_BLOCKLIST = {"DISCORD_TOKEN", "CONTROLLER_TOKEN", "SELFBOT_MASTER_KEY", "OWNER_IDS"}
PROJECT_ROOT = Path(__file__).resolve().parent.parent
FATAL_EXIT_CODE = 2  # configuration / token invalide (voir bot.run_bot)


class ManagerError(Exception):
    """Erreur métier lisible (affichable à l'owner)."""


class RateLimiter:
    """Fenêtre glissante: au plus ``limit`` événements par ``window`` secondes et par clé."""

    def __init__(self, limit: int = 5, window: float = 600.0, clock: Callable[[], float] = time.monotonic):
        self.limit, self.window, self._clock = limit, window, clock
        self._events: dict[str, deque] = {}

    def allow(self, key: str) -> bool:
        now = self._clock()
        q = self._events.setdefault(key, deque())
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


@dataclass
class Instance:
    id: str
    proc: Optional[subprocess.Popen] = None
    started_at: float = 0.0
    restarts: int = 0
    desired: bool = False            # l'owner veut qu'elle tourne
    next_restart_at: float = 0.0
    last_exit: Optional[int] = None
    backoff: float = 0.0

    @property
    def pid(self) -> Optional[int]:
        return self.proc.pid if self.proc and self.proc.poll() is None else None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


class InstanceManager:
    def __init__(
        self,
        root: str | Path = "instances",
        store: Optional[TokenStore] = None,
        db: Optional[Database] = None,
        worker_cmd: Optional[Sequence[str]] = None,
        python: Optional[str] = None,
        backoff_base: float = 2.0,
        backoff_max: float = 120.0,
        stable_after: float = 60.0,
        heartbeat_timeout: float = 90.0,
        rate_limiter: Optional[RateLimiter] = None,
        extra_env: Optional[dict] = None,
    ):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = store or TokenStore(self.root / "tokens.json")
        self.db = db or Database(self.root / "manager.db")
        self.python = python or os.environ.get("WORKER_PYTHON") or sys.executable
        self.worker_cmd = list(worker_cmd) if worker_cmd else [self.python, "-m", "selfbot.worker"]
        self.backoff_base, self.backoff_max = backoff_base, backoff_max
        self.stable_after, self.heartbeat_timeout = stable_after, heartbeat_timeout
        self.rate = rate_limiter or RateLimiter()
        self.extra_env = dict(extra_env or {})
        self.instances: dict[str, Instance] = {}
        self._lock = threading.RLock()
        self._monitor_task: Optional[asyncio.Task] = None
        self.listeners: list[Callable[[dict], None]] = []

    # ------------------------------------------------------------------ #
    # événements (démarrage, arrêt, crash...) pour les notifications
    # ------------------------------------------------------------------ #
    def add_listener(self, callback: Callable[[dict], None]) -> None:
        """``callback(event)`` doit être rapide et non bloquant (peut être appelé depuis un thread)."""
        self.listeners.append(callback)

    def _emit(self, type_: str, iid: str, **data) -> None:
        event = {"type": type_, "instance": iid, "ts": time.time(), **data}
        for cb in list(self.listeners):
            try:
                cb(event)
            except Exception:  # un listener défaillant ne doit jamais casser la supervision
                log.exception("Listener d'événement en échec")

    def log_tail(self, iid: str, lines: int = 8, max_chars: int = 900) -> str:
        """Dernières lignes du log du worker, secrets masqués."""
        try:
            with open(self.log_path(iid), "rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - 8192))
                text = fh.read().decode("utf-8", errors="replace")
        except OSError:
            return ""
        tail = "\n".join(text.strip().splitlines()[-lines:])
        return redact(tail)[-max_chars:]

    # ------------------------------------------------------------------ #
    # chemins
    # ------------------------------------------------------------------ #
    def instance_dir(self, iid: str) -> Path:
        return self.root / iid

    def log_path(self, iid: str) -> Path:
        return self.instance_dir(iid) / "logs" / "worker.out.log"

    def heartbeat_path(self, iid: str) -> Path:
        return self.instance_dir(iid) / "heartbeat"

    @staticmethod
    def _check_id(iid: str) -> None:
        if not ID_RE.match(iid or ""):
            raise ManagerError("Identifiant invalide (1-32 caractères: lettres, chiffres, _ et -)")

    def _require(self, iid: str) -> None:
        self._check_id(iid)
        if iid not in self.store.ids():
            raise ManagerError(f"Instance inconnue: {iid}")

    # ------------------------------------------------------------------ #
    # gestion des comptes
    # ------------------------------------------------------------------ #
    def add_token(self, iid: str, token: str, actor: str = "owner") -> str:
        self._check_id(iid)
        token = (token or "").strip()
        if not TOKEN_RE.match(token):
            raise ManagerError("Format de token invalide")
        if iid in self.store.ids():
            raise ManagerError(f"L'instance {iid} existe déjà")
        dup = self.store.find_duplicate(token)
        if dup:
            raise ManagerError(f"Token déjà utilisé par l'instance {dup}")
        register_secret(token)
        self.store.set(iid, token)
        self.instance_dir(iid).mkdir(parents=True, exist_ok=True)
        self.db.audit(actor, "addtoken", f"{iid} {mask_token(token)}")
        self._emit("added", iid, actor=actor)
        log.info("Instance %s ajoutée (%s) par %s", iid, mask_token(token), actor)
        return mask_token(token)

    def remove(self, iid: str, actor: str = "owner") -> None:
        self._require(iid)
        self.stop(iid, actor=actor, _audit=False, _emit=False)
        self.store.delete(iid)
        with self._lock:
            self.instances.pop(iid, None)
        self.db.audit(actor, "remove", iid)
        self._emit("removed", iid, actor=actor)

    def _load_overrides(self, iid: str) -> dict[str, str]:
        """Réglages non secrets propres à l'instance (instances/<id>/instance.json: {"env": {...}})."""
        p = self.instance_dir(iid) / "instance.json"
        if not p.is_file():
            return {}
        try:
            env = json.loads(p.read_text(encoding="utf-8")).get("env", {})
        except (OSError, json.JSONDecodeError) as exc:
            raise ManagerError(f"instance.json invalide pour {iid}: {exc}") from exc
        return {str(k): str(v) for k, v in env.items() if str(k) not in _ENV_BLOCKLIST}

    def _build_env(self, iid: str, token: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in _ENV_BLOCKLIST}
        env.update(self.extra_env)
        env.update(self._load_overrides(iid))
        env["DISCORD_TOKEN"] = token          # jamais en ligne de commande
        env["INSTANCE_ID"] = iid
        env["DATA_DIR"] = "."
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(PROJECT_ROOT), env.get("PYTHONPATH", "")]))
        return env

    # ------------------------------------------------------------------ #
    # cycle de vie
    # ------------------------------------------------------------------ #
    def start(self, iid: str, actor: str = "owner", _count_rate: bool = True, _event: Optional[str] = "started") -> int:
        self._require(iid)
        with self._lock:
            inst = self.instances.setdefault(iid, Instance(iid))
            if inst.running:
                raise ManagerError(f"{iid} tourne déjà (pid {inst.pid})")
            if _count_rate and not self.rate.allow(iid):
                raise ManagerError(f"Trop de démarrages pour {iid}, réessayez plus tard")
            try:
                token = self.store.get(iid)
            except SecretsError as exc:
                raise ManagerError(f"Token illisible pour {iid}: {exc}") from exc
            if not token:
                raise ManagerError(f"Aucun token pour {iid}")

            workdir = self.instance_dir(iid)
            (workdir / "logs").mkdir(parents=True, exist_ok=True)
            out = open(self.log_path(iid), "ab")
            kwargs: dict = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            try:
                proc = subprocess.Popen(
                    [*self.worker_cmd, "--instance", iid],
                    cwd=workdir, env=self._build_env(iid, token),
                    stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, **kwargs,
                )
            finally:
                out.close()
            inst.proc, inst.started_at, inst.desired = proc, time.monotonic(), True
            inst.last_exit = None
            self.db.audit(actor, "start", f"{iid} pid={proc.pid}")
            log.info("Instance %s démarrée (pid %s)", iid, proc.pid)
            if _event:
                self._emit(_event, iid, actor=actor, pid=proc.pid, restarts=inst.restarts)
            return proc.pid

    def stop(self, iid: str, actor: str = "owner", timeout: float = 10.0, _audit: bool = True, _emit: bool = True) -> bool:
        self._check_id(iid)
        with self._lock:
            inst = self.instances.get(iid)
            if inst is None:
                return False
            inst.desired = False
            proc = inst.proc
            if proc is None or proc.poll() is not None:
                return False
            uptime = time.monotonic() - inst.started_at
            self._terminate(proc, timeout)
            inst.last_exit = proc.returncode
        if _emit:
            self._emit("stopped", iid, actor=actor, code=proc.returncode, uptime=round(uptime, 1))
        if _audit:
            self.db.audit(actor, "stop", iid)
        log.info("Instance %s arrêtée", iid)
        return True

    @staticmethod
    def _terminate(proc: subprocess.Popen, timeout: float) -> None:
        """Arrêt gracieux (CTRL_BREAK / SIGTERM) puis kill si nécessaire."""
        try:
            if sys.platform == "win32":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.terminate()
            proc.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            proc.kill()
            proc.wait(timeout=5)

    def restart(self, iid: str, actor: str = "owner") -> int:
        self._require(iid)
        if not self.rate.allow(iid):
            raise ManagerError(f"Trop de redémarrages pour {iid}, réessayez plus tard")
        self.stop(iid, actor=actor, _audit=False, _emit=False)
        pid = self.start(iid, actor=actor, _count_rate=False, _event="restarted")
        self.db.audit(actor, "restart", iid)
        return pid

    def stop_all(self) -> None:
        for iid in list(self.instances):
            try:
                self.stop(iid, actor="system", _audit=False)
            except ManagerError:
                pass

    # ------------------------------------------------------------------ #
    # état
    # ------------------------------------------------------------------ #
    def heartbeat_age(self, iid: str) -> Optional[float]:
        try:
            return max(0.0, time.time() - float(self.heartbeat_path(iid).read_text().strip()))
        except (OSError, ValueError):
            return None

    def status(self, iid: str) -> dict:
        self._require(iid)
        inst = self.instances.get(iid) or Instance(iid)
        try:
            masked = mask_token(self.store.get(iid))
        except SecretsError:
            masked = "<illisible>"
        return {
            "id": iid,
            "token": masked,
            "state": "running" if inst.running else ("backoff" if inst.desired else "stopped"),
            "pid": inst.pid,
            "uptime": round(time.monotonic() - inst.started_at, 1) if inst.running else 0.0,
            "restarts": inst.restarts,
            "last_exit": inst.last_exit,
            "heartbeat_age": self.heartbeat_age(iid),
        }

    def list(self) -> list[dict]:
        return [self.status(iid) for iid in self.store.ids()]

    # ------------------------------------------------------------------ #
    # supervision (backoff)
    # ------------------------------------------------------------------ #
    def supervise_once(self, now: Optional[float] = None) -> list[str]:
        """Un tour de supervision. Retourne les instances (re)lancées."""
        now = time.monotonic() if now is None else now
        relaunched: list[str] = []
        with self._lock:
            for iid, inst in list(self.instances.items()):
                if not inst.desired:
                    continue
                if inst.running:
                    # heartbeat figé => worker bloqué
                    age = self.heartbeat_age(iid)
                    if (age is not None and age > self.heartbeat_timeout
                            and now - inst.started_at > self.heartbeat_timeout):
                        log.warning("Heartbeat de %s périmé (%.0fs), redémarrage", iid, age)
                        self._emit("stale", iid, age=round(age))
                        self._terminate(inst.proc, 5)
                    else:
                        if now - inst.started_at > self.stable_after:
                            inst.backoff = 0.0
                        continue
                # processus terminé alors qu'il devrait tourner
                if inst.proc is not None and inst.last_exit is None:
                    inst.last_exit = inst.proc.returncode
                    if inst.last_exit == FATAL_EXIT_CODE:   # config/token invalide: inutile de relancer
                        inst.desired = False
                        self.db.audit("system", "fatal", f"{iid} code={inst.last_exit}")
                        self._emit("fatal", iid, code=inst.last_exit, tail=self.log_tail(iid))
                        log.error("Instance %s a quitté avec une erreur fatale, pas de relance", iid)
                        continue
                    inst.backoff = (min(self.backoff_max, inst.backoff * 2) if inst.backoff
                                    else self.backoff_base)
                    inst.next_restart_at = now + inst.backoff
                    self.db.audit("system", "crash", f"{iid} code={inst.last_exit} retry_in={inst.backoff:.0f}s")
                    log.warning("Instance %s arrêtée (code %s), relance dans %.0fs", iid, inst.last_exit, inst.backoff)
                    self._emit("crashed", iid, code=inst.last_exit, retry_in=round(inst.backoff),
                               uptime=round(now - inst.started_at, 1), tail=self.log_tail(iid))
                if now >= inst.next_restart_at:
                    try:
                        inst.restarts += 1
                        self.start(iid, actor="system", _count_rate=False, _event="relaunched")
                        relaunched.append(iid)
                    except ManagerError as exc:
                        inst.restarts -= 1
                        log.error("Relance de %s impossible: %s", iid, exc)
                        inst.desired = False
                        self._emit("fatal", iid, code=None, tail=str(exc))
        return relaunched

    async def run_monitor(self, interval: float = 2.0) -> None:
        try:
            while True:
                await asyncio.get_running_loop().run_in_executor(None, self.supervise_once)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass

    def start_monitor(self, interval: float = 2.0) -> None:
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.ensure_future(self.run_monitor(interval))

    async def shutdown(self) -> None:
        if self._monitor_task:
            self._monitor_task.cancel()
        await asyncio.get_running_loop().run_in_executor(None, self.stop_all)

"""Contrôleur multi-instance: un VRAI bot applicatif (token CONTROLLER_TOKEN).

Dépendance: ``discord.py`` (voir requirements-controller.txt). Elle est incompatible avec
``discord.py-self`` (même espace de noms ``discord``): installez-les dans deux
environnements et pointez ``WORKER_PYTHON`` vers l'interpréteur des workers.

Toutes les commandes sont réservées aux ``OWNER_IDS``. Les tokens ne sont jamais affichés
en clair (``MTQ...xyz``) ni loggués; ``addtoken`` n'est accepté qu'en message privé.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Iterable

from .config import load_dotenv, mask_token
from .logging_setup import register_secret, setup_logging
from .manager import InstanceManager, ManagerError

log = logging.getLogger("selfbot.controller")


class ControllerService:
    """Logique des commandes, indépendante de Discord (testable)."""

    def __init__(self, manager: InstanceManager, owner_ids: Iterable[int]):
        self.manager = manager
        self.owner_ids = {int(i) for i in owner_ids}

    def is_owner(self, user_id: int) -> bool:
        return int(user_id) in self.owner_ids

    def _guard(self, actor_id: int) -> str:
        if not self.is_owner(actor_id):
            raise ManagerError("Accès refusé")
        return str(actor_id)

    def addtoken(self, actor_id: int, iid: str, token: str) -> str:
        masked = self.manager.add_token(iid, token, actor=self._guard(actor_id))
        return f"✅ Instance `{iid}` ajoutée (token `{masked}`)"

    def start(self, actor_id: int, iid: str) -> str:
        pid = self.manager.start(iid, actor=self._guard(actor_id))
        return f"▶️ `{iid}` démarrée (pid {pid})"

    def stop(self, actor_id: int, iid: str) -> str:
        stopped = self.manager.stop(iid, actor=self._guard(actor_id))
        return f"⏹️ `{iid}` arrêtée" if stopped else f"ℹ️ `{iid}` n'était pas en cours d'exécution"

    def restart(self, actor_id: int, iid: str) -> str:
        pid = self.manager.restart(iid, actor=self._guard(actor_id))
        return f"🔄 `{iid}` redémarrée (pid {pid})"

    def remove(self, actor_id: int, iid: str) -> str:
        self.manager.remove(iid, actor=self._guard(actor_id))
        return f"🗑️ `{iid}` supprimée"

    @staticmethod
    def _fmt(st: dict) -> str:
        hb = "—" if st["heartbeat_age"] is None else f"{st['heartbeat_age']:.0f}s"
        return (f"`{st['id']}` · {st['state']} · token `{st['token']}` · pid {st['pid'] or '—'} "
                f"· restarts {st['restarts']} · heartbeat {hb}")

    def status(self, actor_id: int, iid: str) -> str:
        self._guard(actor_id)
        return self._fmt(self.manager.status(iid))

    def list(self, actor_id: int) -> str:
        self._guard(actor_id)
        rows = self.manager.list()
        return "\n".join(self._fmt(r) for r in rows) if rows else "Aucune instance."


def build_bot(service: ControllerService):
    """Construit le bot discord.py (import local: dépendance optionnelle)."""
    import discord
    from discord.ext import commands

    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(
        command_prefix=os.environ.get("CONTROLLER_PREFIX", "!"),
        intents=intents,
        owner_ids=set(service.owner_ids),
        help_command=None,
    )

    async def reply(ctx, fn, *args):
        try:
            text = await asyncio.get_running_loop().run_in_executor(None, lambda: fn(ctx.author.id, *args))
        except ManagerError as exc:
            text = f"❌ {exc}"
        await ctx.send(text[:1900])

    def owner_cmd(name, **kw):
        def deco(func):
            func = commands.cooldown(5, 10, commands.BucketType.user)(func)
            func = commands.is_owner()(func)
            return bot.command(name=name, **kw)(func)
        return deco

    @owner_cmd("addtoken")
    @commands.dm_only()
    async def addtoken(ctx, instance_id: str, token: str):
        """!addtoken <id> <token> (en message privé uniquement)"""
        register_secret(token)
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass
        await reply(ctx, service.addtoken, instance_id, token)

    @owner_cmd("start")
    async def start(ctx, instance_id: str):
        await reply(ctx, service.start, instance_id)

    @owner_cmd("stop")
    async def stop(ctx, instance_id: str):
        await reply(ctx, service.stop, instance_id)

    @owner_cmd("restart")
    async def restart(ctx, instance_id: str):
        await reply(ctx, service.restart, instance_id)

    @owner_cmd("remove")
    async def remove(ctx, instance_id: str):
        await reply(ctx, service.remove, instance_id)

    @owner_cmd("status")
    async def status(ctx, instance_id: str):
        await reply(ctx, service.status, instance_id)

    @owner_cmd("list")
    async def list_(ctx):
        await reply(ctx, service.list)

    @bot.event
    async def on_command_error(ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, (commands.NotOwner, commands.CheckFailure)):
            log.warning("Commande refusée pour l'utilisateur %s", ctx.author.id)
            return  # silence: ne pas révéler l'existence du contrôleur
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Réessayez dans {error.retry_after:.0f}s")
            return
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            await ctx.send(f"❌ Arguments invalides: {error}")
            return
        log.error("Erreur de commande: %r", error)

    @bot.event
    async def on_ready():
        log.info("Contrôleur connecté en tant que %s", bot.user)
        service.manager.start_monitor()

    async def close_hook():
        await service.manager.shutdown()

    original_close = bot.close

    async def close():
        await close_hook()
        await original_close()

    bot.close = close  # type: ignore[method-assign]
    return bot


def main() -> int:
    load_dotenv(".env")
    token = os.environ.get("CONTROLLER_TOKEN", "").strip()
    owners = {int(x) for x in os.environ.get("OWNER_IDS", "").replace(",", " ").split() if x.isdigit()}
    problems = []
    if not token:
        problems.append("CONTROLLER_TOKEN est absent (token d'un bot applicatif, voir .env.example)")
    if not owners:
        problems.append("OWNER_IDS est vide: au moins un identifiant d'owner est requis")
    if problems:
        print("Configuration invalide:\n  - " + "\n  - ".join(problems), file=sys.stderr)
        return 2

    setup_logging("controller", Path("logs"), secrets=[token])
    manager = InstanceManager(root=os.environ.get("INSTANCES_DIR", "instances"))
    service = ControllerService(manager, owners)
    try:
        bot = build_bot(service)
    except ImportError:
        print("discord.py est requis pour le contrôleur: pip install -r requirements-controller.txt", file=sys.stderr)
        return 2
    log.info("Démarrage du contrôleur (token %s, %d owner(s))", mask_token(token), len(owners))
    try:
        bot.run(token, log_handler=None)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())


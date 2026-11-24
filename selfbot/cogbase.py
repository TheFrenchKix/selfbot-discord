"""Aides communes aux cogs du selfbot (sortie « carte », erreurs, kv SQLite, HTTP JSON)."""
from __future__ import annotations

import asyncio
import logging

import aiohttp
import discord

from . import ui
from .tools import ToolError

log = logging.getLogger("selfbot.cog")
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)


class CogHelpers:
    """Mixin: nécessite ``self.bot`` et ``self._db`` (callable renvoyant la base ou None)."""

    bot: object
    _db: object

    # ---- sortie -------------------------------------------------------------
    async def out(self, ctx, text: str) -> None:
        pages = ui.chunk(text)
        try:
            await ctx.message.edit(content=pages[0])
        except discord.HTTPException:
            await ctx.send(pages[0])
        for extra in pages[1:]:
            await ctx.send(extra)

    async def fail(self, ctx, err, detail: str = "") -> None:
        await self.out(ctx, ui.err(str(err), detail))

    # ---- stockage -----------------------------------------------------------
    def _kv(self, key, default):
        db = self._db()
        return db.get(key, default) if db else default

    def _save(self, key, value) -> None:
        db = self._db()
        if db:
            db.set(key, value)

    def _audit(self, action: str, detail: str) -> None:
        db = self._db()
        if db:
            db.audit(str(getattr(self.bot.user, "id", "self")), action, detail)

    # ---- HTTP ---------------------------------------------------------------
    async def _http_json(self, url: str, headers: dict | None = None, **params):
        hdrs = {"User-Agent": "selfbot-utils", **(headers or {})}
        try:
            async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as s:
                async with s.get(url, params=params or None, headers=hdrs) as r:
                    if r.status != 200:
                        raise ToolError(f"Service indisponible (HTTP {r.status})")
                    return await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise ToolError(f"Service injoignable ({type(exc).__name__})") from None

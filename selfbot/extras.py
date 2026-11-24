"""Commandes utilitaires supplémentaires du selfbot (cog): infos, snipe, AFK, rappels, outils, modération.

Volontairement absent: tout ce qui relève du raid / spam de masse / vol de comptes.
La logique pure est dans :mod:`selfbot.tools` (testée sans Discord).
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid as uuidlib
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import discord
from discord.ext import commands

from . import tools, ui
from .cogbase import CogHelpers
from .tools import ToolError

log = logging.getLogger("selfbot.extras")
STATUSES = {"online": discord.Status.online, "idle": discord.Status.idle, "dnd": discord.Status.dnd,
            "invisible": discord.Status.invisible}
EIGHT_BALL = ["Yes", "No", "Probably", "Probably not", "Ask again later", "Definitely", "Very doubtful", "Without a doubt"]
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)
REMINDER_KEY = "reminders"
NOTES_KEY = "notes"


class Extras(CogHelpers, commands.Cog, name="Extras"):
    def __init__(self, bot: commands.Bot, db_getter=lambda: None, prefix: str = "."):
        self.bot = bot
        self._db = db_getter
        self.prefix = prefix
        self.started = time.time()
        self.deleted = tools.SnipeStore()
        self.edited = tools.SnipeStore()
        self.afk = tools.AfkState()
        self._task: asyncio.Task | None = None

    # ---- cycle de vie -------------------------------------------------------
    async def cog_load(self) -> None:
        self._task = asyncio.ensure_future(self._reminder_loop())

    async def cog_unload(self) -> None:
        if self._task:
            self._task.cancel()

    async def _resolve_user(self, ctx, arg: str | None):
        if not arg:
            return ctx.author
        uid = tools.parse_snowflake(arg)
        if ctx.guild and (m := ctx.guild.get_member(uid)):
            return m
        try:
            return await self.bot.fetch_user(uid)
        except discord.NotFound:
            raise ToolError("Utilisateur introuvable") from None

    async def _resolve_member(self, ctx, arg: str):
        if not ctx.guild:
            raise ToolError("Commande utilisable uniquement dans un serveur")
        member = ctx.guild.get_member(tools.parse_snowflake(arg))
        if member is None:
            raise ToolError("Membre introuvable sur ce serveur")
        return member

    # ======================================================================= #
    # Infos
    # ======================================================================= #
    @commands.command()
    async def ping(self, ctx):
        """Latency of the gateway and of the REST API."""
        t0 = time.perf_counter()
        await ctx.message.edit(content=ui.wait("Pinging…"))
        rest = (time.perf_counter() - t0) * 1000
        gw = self.bot.latency * 1000 if self.bot.latency == self.bot.latency else -1
        await self.out(ctx, ui.card("Pong", icon="🏓", fields=[("Gateway", f"{gw:.0f} ms" if gw >= 0 else "n/a"),
                                                              ("REST", f"{rest:.0f} ms")]))

    @commands.command()
    async def uptime(self, ctx):
        """How long the selfbot has been running."""
        await self.out(ctx, ui.card("Uptime", icon="⏱️", fields=[
            ("Running for", tools.fmt_duration(time.time() - self.started)),
            ("Since", tools.ts(self.started, "R"))]))

    @commands.command(aliases=["ui", "whois"])
    async def userinfo(self, ctx, user: str = None):
        """Information about a user (mention, ID or yourself)."""
        try:
            u = await self._resolve_user(ctx, user)
        except ToolError as e:
            return await self.fail(ctx, e)
        fields = [("User", f"{u} (`{u.id}`)"), ("Created", f"{tools.ts(u.created_at)} · {tools.ts(u.created_at, 'R')}"),
                  ("Bot", ui.yesno(bool(u.bot)).replace("On", "Yes").replace("Off", "No"))]
        if isinstance(u, discord.Member):
            if u.nick:
                fields.append(("Nickname", u.nick))
            if u.joined_at:
                fields.append(("Joined", f"{tools.ts(u.joined_at)} · {tools.ts(u.joined_at, 'R')}"))
            roles = [r.name for r in reversed(u.roles) if r.name != "@everyone"]
            if roles:
                fields.append((f"Roles ({len(roles)})", tools.trunc(", ".join(roles[:15]), 400)))
        fields.append(("Avatar", u.display_avatar.url))
        await self.out(ctx, ui.card(u.display_name, fields, icon="👤"))

    @commands.command()
    async def avatar(self, ctx, user: str = None):
        """Avatar URL of a user."""
        try:
            u = await self._resolve_user(ctx, user)
        except ToolError as e:
            return await self.fail(ctx, e)
        await self.out(ctx, ui.card(f"Avatar · {u.display_name}", icon="🖼️", desc=u.display_avatar.replace(size=1024).url))

    @commands.command()
    async def banner(self, ctx, user: str = None):
        """Profile banner of a user."""
        try:
            u = await self._resolve_user(ctx, user)
            full = await self.bot.fetch_user(u.id)
        except (ToolError, discord.NotFound) as e:
            return await self.fail(ctx, e)
        if not full.banner:
            return await self.out(ctx, ui.warn(f"{u.display_name} has no banner"))
        await self.out(ctx, ui.card(f"Banner · {u.display_name}", icon="🖼️", desc=full.banner.replace(size=1024).url))

    @commands.command(aliases=["si", "guildinfo"])
    async def serverinfo(self, ctx):
        """Information about the current server."""
        g = ctx.guild
        if not g:
            return await self.fail(ctx, "Use this command in a server")
        fields = [("Name", f"{g.name} (`{g.id}`)"), ("Owner", f"<@{g.owner_id}>"),
                  ("Created", f"{tools.ts(g.created_at)} · {tools.ts(g.created_at, 'R')}"),
                  ("Members", g.member_count), ("Channels", f"{len(g.text_channels)} text · {len(g.voice_channels)} voice"),
                  ("Roles", len(g.roles)), ("Emojis", len(g.emojis)),
                  ("Boosts", f"{g.premium_subscription_count} (tier {g.premium_tier})")]
        if g.icon:
            fields.append(("Icon", g.icon.url))
        await self.out(ctx, ui.card(g.name, fields, icon="🏰"))

    @commands.command(aliases=["members"])
    async def membercount(self, ctx):
        """Member count of the current server."""
        if not ctx.guild:
            return await self.fail(ctx, "Use this command in a server")
        await self.out(ctx, ui.card("Members", [("Total", ctx.guild.member_count)], icon="👥"))

    @commands.command(aliases=["ci"])
    async def channelinfo(self, ctx):
        """Information about the current channel."""
        c = ctx.channel
        fields = [("Channel", f"{getattr(c, 'name', 'DM')} (`{c.id}`)"), ("Created", tools.ts(c.created_at)),
                  ("Type", str(c.type).replace("ChannelType.", ""))]
        if getattr(c, "topic", None):
            fields.append(("Topic", tools.trunc(c.topic, 300)))
        if getattr(c, "slowmode_delay", 0):
            fields.append(("Slowmode", f"{c.slowmode_delay}s"))
        await self.out(ctx, ui.card("Channel", fields, icon="#️⃣"))

    @commands.command()
    async def emojis(self, ctx):
        """List the custom emojis of the server."""
        if not ctx.guild:
            return await self.fail(ctx, "Use this command in a server")
        em = " ".join(str(e) for e in ctx.guild.emojis)
        await self.out(ctx, ui.card(f"Emojis ({len(ctx.guild.emojis)})", icon="😀", desc=em or "None"))

    @commands.command()
    async def snowflake(self, ctx, value: str):
        """Creation date hidden in any Discord ID."""
        try:
            sid = tools.parse_snowflake(value)
        except ToolError as e:
            return await self.fail(ctx, e)
        d = tools.snowflake_time(sid)
        await self.out(ctx, ui.card("Snowflake", [("ID", f"`{sid}`"), ("Created", f"{tools.ts(d, 'F')} · {tools.ts(d, 'R')}")], icon="❄️"))

    # ======================================================================= #
    # Messages: snipe / firstmsg
    # ======================================================================= #
    @commands.Cog.listener()
    async def on_message_delete(self, message):
        if message.author.id == self.bot.user.id or not (message.content or message.attachments):
            return
        self.deleted.add(message.channel.id, {
            "author": str(message.author), "content": message.content,
            "files": [a.filename for a in message.attachments]})

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        if before.author.id == self.bot.user.id or before.content == after.content:
            return
        self.edited.add(before.channel.id, {"author": str(before.author), "content": before.content, "after": after.content})

    async def _snipe(self, ctx, store, title: str, index: int):
        e = store.get(ctx.channel.id, max(1, index))
        if not e:
            return await self.out(ctx, ui.warn(f"Nothing to {title.lower()} in this channel"))
        fields = [("Author", e["author"]), ("When", tools.ts(e["at"], "R")),
                  ("Content", tools.neutralize_mentions(tools.trunc(e["content"] or "—", 600)))]
        if e.get("after"):
            fields.append(("Now", tools.neutralize_mentions(tools.trunc(e["after"], 600))))
        if e.get("files"):
            fields.append(("Files", ", ".join(e["files"])))
        await self.out(ctx, ui.card(title, fields, icon="🔎", footer="in memory only · kept 1 h"))

    @commands.command()
    async def snipe(self, ctx, index: int = 1):
        """Last deleted message of the channel (`snipe 2` = the one before)."""
        await self._snipe(ctx, self.deleted, "Deleted message", index)

    @commands.command(aliases=["esnipe"])
    async def editsnipe(self, ctx, index: int = 1):
        """Last edited message of the channel (original content)."""
        await self._snipe(ctx, self.edited, "Edited message", index)

    @commands.command()
    async def firstmsg(self, ctx):
        """Link to the first message of the channel."""
        async for m in ctx.channel.history(limit=1, oldest_first=True):
            return await self.out(ctx, ui.card("First message", icon="📌", desc=m.jump_url))
        await self.out(ctx, ui.warn("This channel is empty"))

    # ======================================================================= #
    # AFK
    # ======================================================================= #
    @commands.command()
    async def afk(self, ctx, *, reason: str = "AFK"):
        """Auto-reply to DMs/mentions until you write again (`afk off` to stop)."""
        if reason.strip().lower() in ("off", "stop"):
            elapsed = self.afk.disable()
            return await self.out(ctx, ui.ok("AFK disabled", f"You were away for {tools.fmt_duration(elapsed)}"))
        self.afk.enable(tools.neutralize_mentions(tools.trunc(reason, 200)))
        await self.out(ctx, ui.ok("AFK enabled", f"{self.afk.reason}\nSends one reply per person every 10 min. Writing a message disables it."))

    @commands.Cog.listener()
    async def on_message(self, message):
        if not self.afk.active:
            return
        me = self.bot.user
        if message.author.id == me.id:
            if not message.content.lower().startswith(f"{self.prefix}afk"):
                self.afk.disable()
                log.info("AFK disabled (you sent a message)")
            return
        if message.author.bot:
            return
        if (message.guild is None or me in message.mentions) and self.afk.should_reply(message.author.id):
            try:
                await message.channel.send(f"-# 💤 AFK since {tools.ts(self.afk.since, 'R')} · {self.afk.reason}")
            except discord.HTTPException:
                pass

    # ======================================================================= #
    # Rappels et notes
    # ======================================================================= #
    @commands.command(aliases=["remindme", "rm"])
    async def remind(self, ctx, duration: str, *, text: str = "Reminder"):
        """Reminder in this channel: `remind 1h30m take the pizza out`."""
        try:
            secs = tools.parse_duration(duration)
            rems = self._kv(REMINDER_KEY, [])
            r = tools.add_reminder(rems, time.time() + secs, tools.neutralize_mentions(text), ctx.channel.id)
        except ToolError as e:
            return await self.fail(ctx, e)
        self._save(REMINDER_KEY, rems)
        await self.out(ctx, ui.ok(f"Reminder #{r['id']} set", f"{tools.ts(r['at'], 'R')} · {r['text']}"))

    @commands.command()
    async def reminders(self, ctx):
        """List active reminders."""
        rems = self._kv(REMINDER_KEY, [])
        rows = [f"`#{r['id']}` {tools.ts(r['at'], 'R')} · {r['text']}" for r in rems]
        await self.out(ctx, ui.list_card("Reminders", rows, icon="⏰", empty="no active reminder"))

    @commands.command()
    async def unremind(self, ctx, reminder_id: int):
        """Delete a reminder by its number."""
        rems = self._kv(REMINDER_KEY, [])
        kept = [r for r in rems if r["id"] != reminder_id]
        if len(kept) == len(rems):
            return await self.fail(ctx, f"No reminder #{reminder_id}")
        self._save(REMINDER_KEY, kept)
        await self.out(ctx, ui.ok(f"Reminder #{reminder_id} deleted"))

    async def _reminder_loop(self) -> None:
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:  # client jamais connecté (tests)
            return
        while True:
            try:
                rems = self._kv(REMINDER_KEY, [])
                due = tools.due_reminders(rems)
                if due:
                    self._save(REMINDER_KEY, [r for r in rems if r not in due])
                    for r in due:
                        await self._fire(r)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Reminder loop error")
            await asyncio.sleep(15)

    async def _fire(self, r: dict) -> None:
        late = time.time() - r["at"]
        text = ui.info("⏰ Reminder", r["text"] + (f"\n-# {tools.fmt_duration(late)} late" if late > 120 else ""))
        try:
            ch = self.bot.get_channel(r["channel"]) or await self.bot.fetch_channel(r["channel"])
            await ch.send(text)
        except (discord.HTTPException, AttributeError):
            log.warning("Reminder #%s: channel unavailable", r.get("id"))

    @commands.command()
    async def note(self, ctx, action: str = "list", *, text: str = ""):
        """Notes: `note add <text>` · `note list` · `note del <n>`."""
        notes = self._kv(NOTES_KEY, [])
        action = action.lower()
        if action == "add":
            if not text.strip():
                return await self.fail(ctx, "Text missing")
            if len(notes) >= 100:
                return await self.fail(ctx, "Maximum 100 notes")
            notes.append(tools.trunc(text, 500))
            self._save(NOTES_KEY, notes)
            return await self.out(ctx, ui.ok(f"Note #{len(notes)} saved"))
        if action in ("del", "rm", "delete"):
            try:
                notes.pop(int(text) - 1)
            except (ValueError, IndexError):
                return await self.fail(ctx, "Invalid note number")
            self._save(NOTES_KEY, notes)
            return await self.out(ctx, ui.ok("Note deleted"))
        if action == "list":
            return await self.out(ctx, ui.list_card("Notes", [f"`#{i}` {n}" for i, n in enumerate(notes, 1)],
                                                    icon="📝", empty="no notes"))
        await self.fail(ctx, "Unknown action", f"`{self.prefix}note add|list|del`")

    # ======================================================================= #
    # Outils
    # ======================================================================= #
    @commands.command()
    async def roll(self, ctx, dice: str = "d20"):
        """Roll dice: `roll 2d6+1`."""
        try:
            rolls, total = tools.roll_dice(dice)
        except ToolError as e:
            return await self.fail(ctx, e)
        await self.out(ctx, ui.card(f"Dice · {dice}", [("Rolls", ", ".join(map(str, rolls[:30]))), ("Total", f"**{total}**")], icon="🎲"))

    @commands.command()
    async def flip(self, ctx):
        """Flip a coin."""
        await self.out(ctx, ui.card("Coin", icon="🪙", desc=f"> **{random.SystemRandom().choice(['Heads', 'Tails'])}**"))

    @commands.command()
    async def choose(self, ctx, *, options: str):
        """Pick one option: `choose pizza | sushi | tacos`."""
        opts = [o.strip() for o in options.split("|") if o.strip()]
        if len(opts) < 2:
            return await self.fail(ctx, "Give at least 2 options separated by `|`")
        await self.out(ctx, ui.card("Choice", icon="🎯", desc=f"> **{tools.neutralize_mentions(random.SystemRandom().choice(opts))}**"))

    @commands.command(name="8ball")
    async def eightball(self, ctx, *, question: str = ""):
        """Ask the magic 8-ball."""
        await self.out(ctx, ui.card("8ball", icon="🎱", desc=f"> **{random.SystemRandom().choice(EIGHT_BALL)}**"))

    @commands.command()
    async def uuid(self, ctx):
        """Generate a random UUID."""
        await self.out(ctx, f"`{uuidlib.uuid4()}`")

    @commands.command()
    async def color(self, ctx, value: str):
        """Convert a color: `color #ff8800`."""
        try:
            c = tools.color_info(value)
        except ToolError as e:
            return await self.fail(ctx, e)
        await self.out(ctx, ui.card("Color", [("Hex", c["hex"]), ("RGB", "rgb{}".format(c["rgb"])),
                                              ("HSL", "hsl({}°, {}%, {}%)".format(*c["hsl"])), ("Int", c["int"])], icon="🎨", code=True))

    @commands.command()
    async def hash(self, ctx, algo: str, *, text: str):
        """Hash a text: `hash sha256 hello`."""
        try:
            digest = tools.hash_text(algo, text)
        except ToolError as e:
            return await self.fail(ctx, e)
        await self.out(ctx, ui.card(f"Hash · {algo.lower()}", icon="#️⃣", desc=f"```{digest}```"))

    @commands.command(name="b64")
    async def base64cmd(self, ctx, mode: str, *, text: str):
        """Base64: `b64 encode hello` / `b64 decode aGVsbG8=`."""
        try:
            result = tools.neutralize_mentions(tools.b64(mode.lower(), text))
        except ToolError as e:
            return await self.fail(ctx, e)
        await self.out(ctx, ui.card("Base64", icon="🔤", desc=f"```{tools.trunc(result, 1500).replace('```', chr(39) * 3)}```"))

    @commands.command()
    async def time(self, ctx, zone: str = "UTC"):
        """Current time in a timezone: `time Europe/Paris`."""
        try:
            now = datetime.now(ZoneInfo(zone))
        except (ZoneInfoNotFoundError, ValueError, OSError):
            return await self.fail(ctx, f"Unknown timezone `{tools.trunc(zone, 40)}`", "Use an IANA name, e.g. `Europe/Paris`")
        await self.out(ctx, ui.card(zone, [("Time", now.strftime("%H:%M:%S")), ("Date", now.strftime("%A %d %B %Y")),
                                           ("UTC offset", now.strftime("%z"))], icon="🕒"))

    @commands.command()
    async def weather(self, ctx, *, city: str):
        """Weather from wttr.in: `weather Paris`."""
        try:
            data = await self._http_json(f"https://wttr.in/{quote(city.strip()[:60])}", format="j1")
            cur = data["current_condition"][0]
            area = data["nearest_area"][0]["areaName"][0]["value"]
        except (ToolError, aiohttp.ClientError, asyncio.TimeoutError, KeyError, IndexError, ValueError) as e:
            return await self.fail(ctx, "Weather unavailable", str(e) if isinstance(e, ToolError) else "")
        await self.out(ctx, ui.card(f"Weather · {area}", [
            ("Conditions", cur["weatherDesc"][0]["value"]), ("Temperature", f"{cur['temp_C']}°C (feels {cur['FeelsLikeC']}°C)"),
            ("Humidity", f"{cur['humidity']}%"), ("Wind", f"{cur['windspeedKmph']} km/h")], icon="🌦️"))

    @commands.command()
    async def define(self, ctx, *, word: str):
        """English dictionary: `define serendipity`."""
        try:
            data = await self._http_json(f"https://api.dictionaryapi.dev/api/v2/entries/en/{quote(word.strip()[:40])}")
            entry = data[0]
            rows = []
            for meaning in entry["meanings"][:3]:
                rows.append(f"> **{meaning['partOfSpeech']}** · {tools.trunc(meaning['definitions'][0]['definition'], 250)}")
        except (ToolError, aiohttp.ClientError, asyncio.TimeoutError, KeyError, IndexError, TypeError, ValueError):
            return await self.fail(ctx, f"No definition for `{tools.trunc(word, 40)}`")
        await self.out(ctx, ui.card(entry["word"], icon="📖", desc="\n".join(rows)))

    @commands.command()
    async def qr(self, ctx, *, text: str):
        """Generate a QR code image."""
        import io

        import qrcode
        buf = io.BytesIO()
        qrcode.make(text[:500]).save(buf, format="PNG")
        buf.seek(0)
        await ctx.send(file=discord.File(buf, "qr.png"))
        await ctx.message.delete()

    # ======================================================================= #
    # Profil
    # ======================================================================= #
    @commands.command()
    async def setstatus(self, ctx, status: str):
        """Online status: online / idle / dnd / invisible."""
        st = STATUSES.get(status.lower())
        if not st:
            return await self.fail(ctx, "Unknown status", ", ".join(STATUSES))
        await self.bot.change_presence(status=st, activity=self.bot.activity)
        await self.out(ctx, ui.ok(f"Status set to `{status.lower()}`"))

    # ======================================================================= #
    # Modération (nécessite les permissions sur le serveur)
    # ======================================================================= #
    async def _mod(self, ctx, action: str, target, coro_factory, ok_text: str) -> None:
        try:
            await coro_factory()
        except discord.Forbidden:
            return await self.fail(ctx, "Missing permission", "Your role is too low or the permission is missing")
        except discord.HTTPException as e:
            return await self.fail(ctx, "Discord refused the action", str(e.text)[:200])
        self._audit(action, f"guild={ctx.guild.id} target={target}")
        await self.out(ctx, ui.ok(ok_text))

    @commands.command()
    async def kick(self, ctx, member: str, *, reason: str = None):
        """Kick a member."""
        try:
            m = await self._resolve_member(ctx, member)
        except ToolError as e:
            return await self.fail(ctx, e)
        await self._mod(ctx, "kick", m.id, lambda: m.kick(reason=reason), f"Kicked **{m}**")

    @commands.command()
    async def ban(self, ctx, user: str, *, reason: str = None):
        """Ban a user (ID or mention, even if they left)."""
        try:
            uid = tools.parse_snowflake(user)
            if not ctx.guild:
                raise ToolError("Commande utilisable uniquement dans un serveur")
        except ToolError as e:
            return await self.fail(ctx, e)
        await self._mod(ctx, "ban", uid, lambda: ctx.guild.ban(discord.Object(id=uid), reason=reason), f"Banned `{uid}`")

    @commands.command()
    async def unban(self, ctx, user: str):
        """Unban a user by ID."""
        try:
            uid = tools.parse_snowflake(user)
            if not ctx.guild:
                raise ToolError("Commande utilisable uniquement dans un serveur")
        except ToolError as e:
            return await self.fail(ctx, e)
        await self._mod(ctx, "unban", uid, lambda: ctx.guild.unban(discord.Object(id=uid)), f"Unbanned `{uid}`")

    @commands.command(aliases=["mute"])
    async def timeout(self, ctx, member: str, duration: str = "10m", *, reason: str = None):
        """Timeout a member: `timeout <user> 1h`. `timeout <user> 0` removes it."""
        try:
            m = await self._resolve_member(ctx, member)
            secs = 0 if duration == "0" else tools.parse_duration(duration)
            if secs > 28 * 86400:
                raise ToolError("Discord limits timeouts to 28 days")
        except ToolError as e:
            return await self.fail(ctx, e)
        until = timedelta(seconds=secs) if secs else None
        text = f"**{m}** timed out for {tools.fmt_duration(secs)}" if secs else f"Timeout removed for **{m}**"
        await self._mod(ctx, "timeout", m.id, lambda: m.timeout(until, reason=reason), text)

    @commands.command()
    async def slowmode(self, ctx, seconds: int = 0):
        """Set the channel slowmode (0 = off, max 21600)."""
        if not 0 <= seconds <= 21600:
            return await self.fail(ctx, "Between 0 and 21600 seconds")
        if not ctx.guild:
            return await self.fail(ctx, "Use this command in a server")
        await self._mod(ctx, "slowmode", ctx.channel.id, lambda: ctx.channel.edit(slowmode_delay=seconds),
                        f"Slowmode: {seconds}s" if seconds else "Slowmode disabled")


async def setup(bot, db_getter=lambda: None, prefix: str = ".") -> Extras:
    cog = Extras(bot, db_getter, prefix)
    await bot.add_cog(cog)
    return cog

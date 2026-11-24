import asyncio
import random
import time
from types import SimpleNamespace

import pytest

from selfbot import tools
from selfbot.tools import ToolError


def test_durations():
    assert tools.parse_duration("1h30m") == 5400 and tools.parse_duration("90s") == 90
    assert tools.parse_duration("2d") == 172800 and tools.parse_duration("45") == 2700
    assert tools.parse_duration("1.5h") == 5400
    for bad in ("", "abc", "0s", "10x", "400d", "-5m", "1h foo"):
        with pytest.raises(ToolError):
            tools.parse_duration(bad)
    assert tools.fmt_duration(3725) == "1h 2m 5s" and tools.fmt_duration(0) == "0s"


def test_snowflake():
    assert tools.snowflake_time(175928847299117063).year == 2016
    assert tools.parse_snowflake("<@175928847299117063>") == 175928847299117063
    with pytest.raises(ToolError):
        tools.parse_snowflake("nope")
    assert tools.ts(0, "R") == "<t:0:R>"


def test_snipe_store_ttl_lru():
    s = tools.SnipeStore(per_channel=2, max_channels=2, ttl=10)
    s.add(1, {"c": "a"}, now=100)
    s.add(1, {"c": "b"}, now=101)
    s.add(1, {"c": "c"}, now=102)                       # 'a' évincé
    assert s.get(1, 1, now=103)["c"] == "c" and s.get(1, 2, now=103)["c"] == "b" and s.get(1, 3, now=103) is None
    assert s.get(1, 1, now=200) is None                 # expiré
    s.add(2, {}, now=1)
    s.add(3, {}, now=1)
    assert s.get(1, 1, now=103) is None                 # salon 1 évincé (LRU)
    assert s.get(1, 0) is None


def test_afk_cooldown():
    a = tools.AfkState(cooldown=60)
    assert not a.should_reply(1)
    a.enable("sleeping", now=0)
    assert a.should_reply(1, now=1) and not a.should_reply(1, now=30) and a.should_reply(1, now=70)
    assert a.should_reply(2, now=2)
    a.disable()
    assert not a.active and not a.should_reply(1)


def test_reminders():
    rems = []
    r1 = tools.add_reminder(rems, 200, "b", 5)
    r2 = tools.add_reminder(rems, 100, "a", 5)
    assert [r["id"] for r in rems] == [r2["id"], r1["id"]] and r2["id"] == 2
    assert tools.due_reminders(rems, now=150) == [r2]
    with pytest.raises(ToolError):
        for _ in range(tools.MAX_REMINDERS):
            tools.add_reminder(rems, 1, "x", 1)


def test_misc_tools():
    assert tools.color_info("#f80")["hex"] == "#ff8800" and tools.color_info("0xFF0000")["int"] == 0xFF0000
    assert tools.color_info("ffffff")["hsl"] == (0, 0, 100)
    with pytest.raises(ToolError):
        tools.parse_color("zzz")
    rolls, total = tools.roll_dice("3d6+2", random.Random(1))
    assert len(rolls) == 3 and total == sum(rolls) + 2 and all(1 <= r <= 6 for r in rolls)
    assert tools.roll_dice("d20", random.Random(1))[0][0] <= 20
    for bad in ("0d6", "d1", "99d6", "abc", "2d6*3"):
        with pytest.raises(ToolError):
            tools.roll_dice(bad)
    assert tools.hash_text("SHA-256", "a").startswith("ca978112")
    with pytest.raises(ToolError):
        tools.hash_text("crc", "a")
    assert tools.b64("encode", "hello") == "aGVsbG8=" and tools.b64("decode", "aGVsbG8=") == "hello"
    for args in (("decode", "!!!"), ("zip", "x")):
        with pytest.raises(ToolError):
            tools.b64(*args)
    assert "@​everyone" in tools.neutralize_mentions("@everyone")
    assert tools.neutralize_mentions("<@123>") != "<@123>"
    assert len(tools.trunc("x" * 50, 10)) == 10


# --------------------------------------------------------------------------- #
# Cog réel (nécessite discord.py-self) avec faux contexte
# --------------------------------------------------------------------------- #
discord = pytest.importorskip("discord")


class User:
    def __init__(self, uid, name="bob", bot=False):
        self.id, self.name, self.bot = uid, name, bot

    def __str__(self):
        return self.name


class FakeMsg:
    def __init__(self):
        self.edits = []

    async def edit(self, content=None, **kw):
        self.edits.append(content)

    async def delete(self):
        pass


class FakeCtx:
    def __init__(self, guild=None):
        self.message, self.sent, self.guild = FakeMsg(), [], guild
        self.channel = SimpleNamespace(id=77)
        self.author = SimpleNamespace(id=1, display_name="me")

    async def send(self, content=None, **kw):
        self.sent.append(content)


class DB:
    def __init__(self):
        self.d, self.audits = {}, []

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v

    def audit(self, *a):
        self.audits.append(a)


@pytest.fixture
def cog():
    from discord.ext import commands

    from selfbot import extras
    bot = commands.Bot(command_prefix=".", self_bot=True)
    bot._connection.user = SimpleNamespace(id=1)
    db = DB()

    async def build():
        c = extras.Extras(bot, lambda: db, ".")
        await bot.add_cog(c)
        return c

    c = asyncio.run(build())
    c.db = db
    return c


def run(cog, name, ctx, *args, **kw):
    cmd = cog.bot.get_command(name)
    return asyncio.run(cmd.callback(cog, ctx, *args, **kw))


def test_all_commands_registered_without_conflict(cog):
    names = {c.name for c in cog.bot.commands}
    for n in ("ping", "userinfo", "serverinfo", "snipe", "editsnipe", "afk", "remind", "note", "roll", "8ball",
              "b64", "time", "weather", "define", "qr", "setstatus", "kick", "ban", "timeout", "slowmode"):
        assert n in names
    assert cog.bot.get_command("remindme").name == "remind" and cog.bot.get_command("mute").name == "timeout"


def test_cog_commands_do_not_clash_with_selfbot(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DISCORD_TOKEN", "AAAAAAAAAAAAAAAAAAAAAAAA.BBBBBB.CCCCCCCCCCCCCCCCCCCCCCCCCCC")
    import importlib

    from selfbot import bot as b
    b = importlib.reload(b)
    asyncio.run(b.client.setup_hook())                    # lève CommandRegisterError en cas de doublon
    assert b.client.get_command("snipe") and b.client.get_command("shop") and b.client.get_command("howto")
    assert "snipe" in b.build_help("tools")
    if b.STATE.db is not None:
        b.STATE.db.close()
        b.STATE.db = None


def test_remind_flow_and_reminder_fire(cog):
    ctx = FakeCtx()
    run(cog, "remind", ctx, "10m", text="@everyone pizza")
    assert "Reminder #1" in ctx.message.edits[-1]
    rems = cog.db.d["reminders"]
    assert len(rems) == 1 and "​" in rems[0]["text"]
    run(cog, "reminders", ctx)
    assert "pizza" in ctx.message.edits[-1]
    run(cog, "remind", ctx, "bad")
    assert "Durée invalide" in ctx.message.edits[-1] and len(cog.db.d["reminders"]) == 1
    run(cog, "unremind", ctx, 1)
    assert cog.db.d["reminders"] == []
    run(cog, "unremind", ctx, 9)
    assert "No reminder #9" in ctx.message.edits[-1]

    posted = []

    class Chan:
        async def send(self, t):
            posted.append(t)

    cog.bot.get_channel = lambda i: Chan()
    asyncio.run(cog._fire({"id": 1, "at": time.time() - 500, "text": "x", "channel": 5}))
    assert "Reminder" in posted[0] and "late" in posted[0]


def test_snipe_listeners_and_afk(cog):
    other, ch = User(2), SimpleNamespace(id=77)
    gone = SimpleNamespace(author=other, content="secret @everyone", attachments=[], channel=ch)
    asyncio.run(cog.on_message_delete(gone))
    asyncio.run(cog.on_message_edit(SimpleNamespace(author=other, content="old", channel=ch),
                                    SimpleNamespace(content="new")))
    ctx = FakeCtx()
    run(cog, "snipe", ctx)
    assert "secret" in ctx.message.edits[-1] and "@everyone" not in ctx.message.edits[-1].replace("@​everyone", "")
    run(cog, "editsnipe", ctx)
    assert "old" in ctx.message.edits[-1] and "new" in ctx.message.edits[-1]
    ctx2 = FakeCtx()
    ctx2.channel = SimpleNamespace(id=5)
    run(cog, "snipe", ctx2)
    assert "Nothing" in ctx2.message.edits[-1]

    replies = []

    class Chan:
        async def send(self, t):
            replies.append(t)

    dm = SimpleNamespace(author=other, content="hi", guild=None, mentions=[], channel=Chan())
    asyncio.run(cog.on_message(dm))
    assert replies == []                                   # AFK inactif
    run(cog, "afk", ctx, reason="lunch")
    asyncio.run(cog.on_message(dm))
    asyncio.run(cog.on_message(dm))
    assert len(replies) == 1 and "lunch" in replies[0]    # une seule réponse (cooldown)
    mine = SimpleNamespace(author=User(1), content="back", guild=None, mentions=[], channel=Chan())
    asyncio.run(cog.on_message(mine))
    assert not cog.afk.active


def test_tool_commands_output(cog):
    ctx = FakeCtx()

    def out():
        return ctx.message.edits[-1]

    run(cog, "roll", ctx, "2d6")
    assert "Total" in out()
    run(cog, "roll", ctx, "x")
    assert "Format invalide" in out()
    run(cog, "color", ctx, "#f80")
    assert "#ff8800" in out()
    run(cog, "hash", ctx, "sha256", text="a")
    assert "ca978112" in out()
    run(cog, "b64", ctx, "encode", text="hello")
    assert "aGVsbG8=" in out()
    run(cog, "time", ctx, "Europe/Paris")
    assert "UTC offset" in out()
    run(cog, "time", ctx, "Mars/Base")
    assert "Unknown timezone" in out()
    run(cog, "choose", ctx, options="a | b")
    assert out().count("**") >= 2
    run(cog, "choose", ctx, options="only")
    assert "at least 2" in out()
    run(cog, "snowflake", ctx, "175928847299117063")
    assert "<t:" in out()
    run(cog, "uptime", ctx)
    assert "Running for" in out()
    run(cog, "note", ctx, "add", text="buy milk")
    run(cog, "note", ctx, "list")
    assert "buy milk" in out()
    run(cog, "note", ctx, "del", text="1")
    assert cog.db.d["notes"] == []
    run(cog, "serverinfo", ctx)
    assert "server" in out()


def test_moderation_errors_and_audit(cog):
    ctx = FakeCtx()
    run(cog, "kick", ctx, "123456789012345678")
    assert "uniquement dans un serveur" in ctx.message.edits[-1]

    calls = []

    class Member:
        id = 111111111111111111

        def __str__(self):
            return "victim"

        async def kick(self, reason=None):
            raise discord.Forbidden(SimpleNamespace(status=403, reason="no"), "Missing Permissions")

        async def timeout(self, until, reason=None):
            calls.append(("timeout", until))

    class Guild:
        id = 9

        def get_member(self, i):
            return Member() if i == 111111111111111111 else None

        async def ban(self, obj, reason=None):
            calls.append(("ban", obj.id))

        async def unban(self, obj):
            calls.append(("unban", obj.id))

    ctx = FakeCtx(Guild())
    run(cog, "kick", ctx, "111111111111111111")
    assert "Missing permission" in ctx.message.edits[-1] and cog.db.audits == []
    run(cog, "kick", ctx, "222222222222222222")
    assert "Membre introuvable" in ctx.message.edits[-1]
    run(cog, "ban", ctx, "333333333333333333", reason="spam")
    run(cog, "unban", ctx, "333333333333333333")
    run(cog, "timeout", ctx, "111111111111111111", "1h")
    assert [c[0] for c in calls] == ["ban", "unban", "timeout"] and calls[2][1].total_seconds() == 3600
    assert [a[1] for a in cog.db.audits] == ["ban", "unban", "timeout"]
    run(cog, "timeout", ctx, "111111111111111111", "60d")
    assert "28 days" in ctx.message.edits[-1]

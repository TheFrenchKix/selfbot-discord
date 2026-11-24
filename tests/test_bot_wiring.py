"""Vérifie le câblage de selfbot.bot (nécessite discord.py-self; sinon ignoré)."""
import pytest

pytest.importorskip("discord")


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DISCORD_TOKEN", "AAAAAAAAAAAAAAAAAAAAAAAA.BBBBBB.CCCCCCCCCCCCCCCCCCCCCCCCCCC")
    import importlib
    from selfbot import bot as b
    b = importlib.reload(b)
    yield b
    if b.STATE.db is not None:
        b.STATE.db.close()
        b.STATE.db = None


def test_no_hardcoded_secrets_in_module(bot):
    assert bot.ltc_address == "" or bot.ltc_address == bot.SETTINGS.ltc_address
    assert bot.PROXY_URL == bot.SETTINGS.proxy_url
    assert bot.api_token == bot.SETTINGS.api.blockcypher


def test_mask_proxy(bot):
    assert "pass" not in bot.mask_proxy("http://user:pass@1.2.3.4:80/")
    assert bot.mask_proxy("") == "Not set"


def test_error_handlers_registered(bot):
    assert bot.client.extra_events.get("on_command_error") or hasattr(bot, "on_command_error")


def test_calcul_is_safe_version(bot):
    assert bot.calcul("2+2") == 4
    assert str(bot.calcul("__import__('os')")).startswith("Erreur")


def test_id_files_persist_in_sqlite_not_txt(bot, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    legacy = tmp_path / "stocks.txt"
    legacy.write_text("5\n6\n")
    table = set()
    bot.file_to_table("stocks.txt", table)      # import unique depuis l'ancien .txt
    assert table == {5, 6}
    table.add(7)
    bot.table_to_file("stocks.txt", table)
    assert legacy.read_text() == "5\n6\n"       # le .txt n'est ni supprimé ni réécrit
    again = set()
    bot.file_to_table("stocks.txt", again)
    assert again == {5, 6, 7}
    missing = set()
    bot.file_to_table("tickets.txt", missing)   # fichier absent: pas d'exception
    assert missing == set()


def test_keyword_changes_go_to_db_not_config_ini(bot, tmp_path):
    bot._db().set("keywords", ["x"])
    bot.load_config()
    assert bot.keywords == {"x"}
    assert not (tmp_path / "config.ini").exists()


def test_settoken_never_echoes_token(bot):
    import inspect
    src = inspect.getsource(bot.settoken.callback)
    assert "mask_token(CUSTOM_TOKEN)" in src and "{msg}" not in src and "{CUSTOM_TOKEN}" not in src


def test_help_pages_fit_in_discord_messages(bot):
    from selfbot import ui
    pages = ui.chunk(bot.build_help())
    assert len(pages) == 1 and "howto sniper" in pages[0]
    for key in bot.HELP_TOPICS:
        page = bot.build_help(key)
        assert len(page) < 2000, key
        assert ";" not in page.replace("; ", "")  # préfixe configurable, pas de « ; » codé en dur
    assert "Unknown topic" in bot.build_help("nope")
    assert bot.build_help("stocks") == bot.build_help("stock")  # alias
    assert all(len(p) <= 2000 for p in ui.chunk(bot.build_help("all")))


def test_help_documents_only_existing_commands(bot):
    import asyncio
    import re
    asyncio.run(bot.client.setup_hook())      # charge le cog selfbot/extras.py
    names = {c.name for c in bot.client.commands}
    for key in bot.HELP_TOPICS:
        for cmd in re.findall(r"`\{p\}(\w+)", " ".join(t for _, ts in [(0, [x[1] for x in bot.HELP_TOPICS[key][2]])] for t in ts)):
            assert cmd in names or cmd == "howto", f"{cmd} (topic {key}) n'existe pas"


def test_stock_card_renders(bot):
    from types import SimpleNamespace as NS
    msg = NS(channel=NS(mention="<#1>"), author=NS(mention="<@2>"), content="nitro `2€` x2")
    bot.client._connection.user = NS(mention="<@me>")
    out = bot.stock_card("New stock", msg, "2€", "x2", ticket=5, url="https://shop.example")
    assert "## 📦 New stock" in out and "<#5>" in out and "autobuy" in out and len(out) < 2000

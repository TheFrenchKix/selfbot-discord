import pytest

from selfbot.config import ConfigError, load_settings, mask_token, parse_dotenv


def test_mask_token():
    assert mask_token("MTQ4ODk5OTk5OTk5OTk5.abcdef.xyzxyzxyzxyzxyzxyzxyzxyzxyz") == "MTQ...xyz"
    assert mask_token("") == "<vide>"
    assert mask_token("short") == "***"


def test_parse_dotenv():
    text = "# c\nA=1\nexport B='two words'\nC=\"x\" \nD=val # comment\nbad line\n"
    assert parse_dotenv(text) == {"A": "1", "B": "two words", "C": "x", "D": "val"}


def test_load_settings_from_env(tmp_path):
    env = {
        "DISCORD_TOKEN": " tok ", "MSG_CHANNEL_ID": "123", "MAX_PRICE": "2,5",
        "IGNORED_GUILD_IDS": "1, 2", "KEYWORDS": "nitro, Robux", "PROXY_URL": "http://u:p@h:1/",
    }
    s = load_settings(env=env, ini_path=tmp_path / "none.ini")
    assert s.token == "tok"
    assert s.channels.message == 123
    assert s.filters.max_price == 2.5
    assert s.ignored_guild_ids == {1, 2}
    assert s.filters.keywords == {"nitro", "robux"}
    assert "u:p" not in str(s.public_dict())


def test_ini_fallback_drops_empty_entries(tmp_path):
    ini = tmp_path / "c.ini"
    ini.write_text("[settings]\nkeywords = , a, b\n[Filters]\nmax_price = 7\n", encoding="utf-8")
    s = load_settings(env={"DISCORD_TOKEN": "t"}, ini_path=ini)
    assert s.filters.keywords == {"a", "b"}
    assert s.filters.max_price == 7.0


def test_env_overrides_ini(tmp_path):
    ini = tmp_path / "c.ini"
    ini.write_text("[Filters]\nmax_price = 7\n", encoding="utf-8")
    s = load_settings(env={"DISCORD_TOKEN": "t", "MAX_PRICE": "9"}, ini_path=ini)
    assert s.filters.max_price == 9.0


def test_missing_token_is_explicit(tmp_path):
    with pytest.raises(ConfigError, match="DISCORD_TOKEN"):
        load_settings(env={}, ini_path=tmp_path / "x.ini")
    assert load_settings(env={}, ini_path=tmp_path / "x.ini", require_token=False).token == ""


@pytest.mark.parametrize("env,msg", [
    ({"DISCORD_TOKEN": "t", "MSG_CHANNEL_ID": "abc"}, "MSG_CHANNEL_ID"),
    ({"DISCORD_TOKEN": "t", "MAX_PRICE": "x"}, "MAX_PRICE"),
    ({"DISCORD_TOKEN": "t", "PROXY_URL": "ftp://h"}, "PROXY_URL"),
])
def test_invalid_values(env, msg, tmp_path):
    with pytest.raises(ConfigError, match=msg):
        load_settings(env=env, ini_path=tmp_path / "x.ini")

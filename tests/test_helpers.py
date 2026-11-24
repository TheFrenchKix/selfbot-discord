import pytest

from selfbot.helpers import (
    calcul, file_to_table, getPriceInMessage, getQuantityInMessage, getUrlInMessage,
    is_error_result, parse_list_setting, table_to_file,
)


@pytest.mark.parametrize("expr,expected", [
    ("1+2*3", 7), ("(1+2)*3", 9), ("`10/4`", 2.5), ("2**10", 1024), ("-5+2", -3), ("1,5*2", 3.0), ("7//2", 3),
])
def test_calcul_ok(expr, expected):
    assert calcul(expr) == expected


@pytest.mark.parametrize("expr", [
    "__import__('os').system('echo pwn')", "open('x')", "().__class__", "a+1", "[1,2]", "True+1",
    "2**100000", "1/0", "'a'*3", "x" * 300,
])
def test_calcul_rejects_code(expr):
    assert str(calcul(expr)).startswith("Erreur")


def test_price_parsing():
    assert getPriceInMessage("nitro 2.5€ x2") == "2.5€"
    assert getPriceInMessage("robux 3$") == "3$"
    assert getPriceInMessage("deal 1.20 now") == "1.20€"
    assert getPriceInMessage("5 ltc") == "5€"
    assert getPriceInMessage("totally FREE") == 0
    assert getPriceInMessage("hello world") is None


def test_quantity_parsing():
    assert getQuantityInMessage("2x nitro") == "2x"
    assert getQuantityInMessage("nitro x5") == "x5"
    assert getQuantityInMessage("nothing here") is None


def test_url_parsing():
    assert getUrlInMessage("buy https://shop.example/x now") == "https://shop.example/x"
    assert getUrlInMessage("see https://discord.com/channels/1") is None
    assert getUrlInMessage("**https://a.b/c**)") == "https://a.b/c"


def test_parse_list_setting():
    assert parse_list_setting(", a, b ,,c") == {"a", "b", "c"}


def test_file_to_table_missing_file_is_noop(tmp_path):
    table = {1}
    assert file_to_table(tmp_path / "absent.txt", table) == {1}


def test_table_roundtrip_keeps_file_on_rewrite(tmp_path):
    f = tmp_path / "stocks.txt"
    table_to_file(f, [3, 1, 2])
    assert file_to_table(f, set()) == {1, 2, 3}
    table_to_file(f, [9])
    assert file_to_table(f, set()) == {9}
    assert not list(tmp_path.glob("*.tmp"))


def test_is_error_result():
    assert is_error_result({"error": "x"})
    assert is_error_result(({"error": "x"}, None))
    assert is_error_result(None)
    assert not is_error_result("0xabc")

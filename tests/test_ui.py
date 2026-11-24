import pytest

from selfbot import ui


def test_callouts():
    assert ui.ok("Done") == "> ✅ Done"
    assert ui.err("Boom", "why").splitlines() == ["> ❌ Boom", "> why"]
    assert ui.warn("x").startswith("> ⚠️")


def test_card_layout():
    out = ui.card("Stock", [("Month", 12), ("Year", "`3`")], icon="📦", desc="hello", footer="foot")
    lines = out.splitlines()
    assert lines[0] == "## 📦 Stock"
    assert "> **Month** · 12" in lines and "> **Year** · `3`" in lines
    assert lines[-1] == "-# foot"


def test_list_card_and_empty():
    assert "Channels (2)" in ui.list_card("Channels", ["a", "b"], icon="📦")
    assert ui.list_card("Channels", []).startswith("> ⚠️")
    assert "et 3 de plus" in ui.bullets(list(range(5)), limit=2)


def test_chunk_respects_discord_limit():
    text = "\n".join(f"line {i} " + "x" * 80 for i in range(200))
    pages = ui.chunk(text)
    assert len(pages) > 1 and all(len(p) <= 2000 for p in pages)
    assert "\n".join(pages) == text
    assert all(len(p) <= 2000 for p in ui.chunk("y" * 5000))


def test_vouch_is_copyable():
    assert ui.vouch("<#1>", "+rep me").endswith("```+rep me```")

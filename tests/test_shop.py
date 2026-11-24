import asyncio

import pytest
from aiohttp import web

from selfbot import shop


def sku(i, name, ctype, eur, nitro=None, orbs=None, exclusive=False, bundled=None):
    normal = [{"amount": eur, "currency": "eur", "exponent": 2}, {"amount": eur, "currency": "usd", "exponent": 2}]
    if orbs:
        normal.append({"amount": orbs, "currency": "discord_orb", "exponent": 0})
    prices = {"0": {"country_prices": {"country_code": "DE", "prices": normal}}}
    if nitro:
        prices["4"] = {"country_prices": {"country_code": "DE", "prices": [{"amount": nitro, "currency": "eur", "exponent": 2}]}}
    s = {"id": i, "name": name, "description": "desc", "premium": False, "exclusive": exclusive,
         "tenant_metadata": {"collectibles": {"type": ctype}}, "prices": prices}
    if bundled:
        s["bundled_skus"] = [{"id": "x", "name": b} for b in bundled]
    return s


PRODUCTS = [
    {"name": "Venom", "created_at": "2026-03-11T20:29:59+00:00", "skus": [sku("1", "Venom (No Logo)", 0, 899, 699)]},
    {"name": "Gothica", "created_at": "2026-01-01T00:00:00+00:00", "skus": [sku("2", "Nevermore", 2, 599, 499, orbs=4100)]},
    {"name": "Bundles", "created_at": "2026-02-01T00:00:00+00:00",
     "skus": [sku("3", "Cerberus Bundle", 1000, 1599, bundled=["Hellflame", "Frame"], exclusive=True)]},
    {"name": "Free", "created_at": "", "skus": [sku("4", "Zeta", 1, 199)]},
]


@pytest.fixture
def items():
    return [i for p in PRODUCTS for i in shop.parse_product(p)]


def names(res):
    return [i.name for i in res]


def test_parse_product(items):
    venom, nameplate, bundle, _ = items
    assert venom.kind == "deco" and venom.price == 8.99 and venom.nitro_price == 6.99 and venom.discount == 22
    assert nameplate.kind == "nameplate" and nameplate.orbs == 4100
    assert bundle.kind == "bundle" and bundle.exclusive and bundle.bundled == ["Hellflame", "Frame"]
    assert venom.collection == "Venom" and venom.created == "2026-03-11"


def test_parse_query():
    q = shop.parse_query("deco venom dark max=7,5 min=1 sort=price order=desc nitro=yes limit=99 country=fr")
    assert (q.kind, q.name, q.max_price, q.min_price, q.sort, q.reverse, q.nitro, q.limit, q.country) == \
           ("deco", "venom dark", 7.5, 1.0, "price", True, True, 40, "FR")
    assert shop.parse_query("decorations").kind == "deco"
    assert shop.parse_query("type=bundle").kind == "bundle"
    for bad in ("max=abc", "sort=zzz", "foo=1", "nitro=maybe", "type=nope", "country=FRA", "max="):
        with pytest.raises(shop.ShopError):
            shop.parse_query(bad)


def test_filters_and_sorts(items):
    f = lambda t: names(shop.apply_filters(items, shop.parse_query(t)))
    assert f("") == ["Venom (No Logo)", "Nevermore", "Cerberus Bundle", "Zeta"]
    assert f("nevermore") == ["Nevermore"] and f("gothica") == ["Nevermore"]       # nom ou collection
    assert f("deco") == ["Venom (No Logo)"] and f("bundle") == ["Cerberus Bundle"]
    assert f("max=6") == ["Nevermore", "Zeta"] and f("min=10") == ["Cerberus Bundle"]
    assert f("nitro=yes") == ["Venom (No Logo)", "Nevermore"]
    assert f("nitro=no") == ["Cerberus Bundle", "Zeta"]
    assert f("orbs=yes") == ["Nevermore"] and f("exclusive=yes") == ["Cerberus Bundle"]
    assert f("sort=price") == ["Zeta", "Nevermore", "Venom (No Logo)", "Cerberus Bundle"]
    assert f("sort=price order=desc")[0] == "Cerberus Bundle"
    assert f("sort=name")[0] == "Cerberus Bundle"
    assert f("sort=nitro")[:2] == ["Nevermore", "Venom (No Logo)"]


def test_paginate_and_format(items):
    page, pages = shop.paginate(items, 2, 3)
    assert pages == 2 and names(page) == ["Zeta"]
    assert shop.paginate(items, 9, 3)[1] == 2
    line = shop.format_item(items[0])
    assert "8.99€" in line and "Nitro 6.99€ (-22%)" in line and "Venom" in line
    assert "4100 orbs" in shop.format_item(items[1])
    assert "contains: Hellflame, Frame" in shop.format_item(items[2])
    assert shop.fmt_price(None, "eur") == "—" and shop.fmt_price(3, "usd") == "$3.00"
    assert "shop" in shop.usage("!")


def test_usd_fallback():
    p = {"name": "P", "skus": [{"id": "9", "name": "U", "tenant_metadata": {"collectibles": {"type": 0}},
                                "prices": {"0": {"country_prices": {"prices": [{"amount": 500, "currency": "usd", "exponent": 2}]}}}}]}
    it = shop.parse_product(p)[0]
    assert it.currency == "usd" and it.price == 5.0


def test_fetch_items_against_local_server(monkeypatch):
    calls = {"search": [], "skus": []}

    async def search(req):
        calls["search"].append(dict(req.query))
        off = int(req.query["offset"])
        ids = ["1", "2", "3", "4"][off: off + int(req.query["limit"])]
        return web.json_response({"skus": ids, "pagination": {"has_more": off + len(ids) < 4}})

    async def skus(req):
        calls["skus"].append(req.query.getall("sku_ids"))
        assert req.headers["authorization"] == "TOK"
        return web.json_response({"products": PRODUCTS})

    async def run():
        import aiohttp
        app = web.Application()
        app.router.add_get("/shop/search", search)
        app.router.add_get("/storefront/products/skus", skus)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        monkeypatch.setattr(shop, "API", f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}")
        monkeypatch.setattr(shop, "BATCH", 3)
        shop.clear_cache()
        q = shop.parse_query("deco scan=20")
        async with aiohttp.ClientSession() as s:
            res = await shop.fetch_items(s, "TOK", q)
            again = await shop.fetch_items(s, "TOK", q)       # cache
            with pytest.raises(shop.ShopError, match="HTTP 404"):
                monkeypatch.setattr(shop, "API", shop.API + "/nope")
                shop.clear_cache()
                await shop.fetch_items(s, "TOK", q)
        await runner.cleanup()
        return res, again

    res, again = asyncio.run(run())
    assert names(res) == ["Venom (No Logo)", "Nevermore", "Cerberus Bundle", "Zeta"] and res is again
    assert calls["search"][0]["item_types"] == "AVATAR_DECORATION" and calls["search"][0]["sort_type"] == "popularity"
    assert len(calls["search"]) == 2 and len(calls["skus"]) == 2

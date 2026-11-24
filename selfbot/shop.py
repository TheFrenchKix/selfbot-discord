"""Catalogue du shop Discord (décorations, effets, plaques, cadres, bundles).

Endpoints observés (HAR du client web):
* ``GET /shop/search``                -> ids de SKU (filtre ``item_types``, tri, pagination)
* ``GET /storefront/products/skus``   -> détails + prix des SKU (par lots)

La recherche serveur ne filtre que par type et tri: le nom, le prix, etc. sont filtrés ici.
Le parsing/filtrage est pur (sans réseau) pour rester testable.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

import aiohttp

API = "https://discord.com/api/v9"
BATCH = 20                     # SKU par requête de détails
MAX_FETCH = 400                # garde-fou: nombre max de SKU récupérés par recherche
CACHE_TTL = 600                # secondes

# nom utilisateur -> (valeur item_types côté API, libellé, icône)
TYPES = {
    "deco": ("AVATAR_DECORATION", "Avatar decoration", "🖼️"),
    "effect": ("PROFILE_EFFECT", "Profile effect", "✨"),
    "nameplate": ("NAMEPLATE", "Nameplate", "🏷️"),
    "frame": ("PROFILE_FRAME", "Profile frame", "🔲"),
    "bundle": ("BUNDLE", "Bundle", "📦"),
}
TYPE_ALIASES = {
    "decoration": "deco", "decorations": "deco", "avatar": "deco", "effects": "effect", "fx": "effect",
    "nameplates": "nameplate", "plate": "nameplate", "frames": "frame", "bundles": "bundle", "all": "all",
}
# tenant_metadata.collectibles.type -> clé de TYPES
_KIND_BY_CODE = {0: "deco", 1: "effect", 2: "nameplate", 3: "frame", 1000: "bundle"}
SORTS = {
    "popular": ("popularity", "desc"), "popularity": ("popularity", "desc"),
    "new": ("recency", "desc"), "recent": ("recency", "desc"), "old": ("recency", "asc"),
}
LOCAL_SORTS = ("price", "nitro", "name", "orbs")
PREMIUM_NITRO = "4"            # clé de ``prices`` pour les abonnés Nitro (``0`` = prix normal)


class ShopError(Exception):
    pass


@dataclass
class Item:
    id: str
    name: str
    kind: str
    description: str = ""
    collection: str = ""            # nom du produit/option (ex: "Venom")
    price: Optional[float] = None   # prix normal (devise principale)
    nitro_price: Optional[float] = None
    currency: str = "eur"
    orbs: Optional[int] = None
    premium: bool = False
    exclusive: bool = False
    bundled: list[str] = field(default_factory=list)
    created: str = ""

    @property
    def discount(self) -> Optional[int]:
        if self.price and self.nitro_price is not None and self.nitro_price < self.price:
            return round(100 * (1 - self.nitro_price / self.price))
        return None


def _amount(prices: list[dict], currency: str) -> Optional[float]:
    for p in prices:
        if p.get("currency") == currency:
            return p["amount"] / (10 ** p.get("exponent", 0))
    return None


def parse_product(product: dict, currency: str = "eur") -> list[Item]:
    """Transforme un produit de ``storefront/products/skus`` en :class:`Item` (un par SKU)."""
    items = []
    skus = product.get("skus", [])
    names = {s["id"]: s["name"] for s in skus}
    for s in skus:
        col = (s.get("tenant_metadata") or {}).get("collectibles") or {}
        kind = _KIND_BY_CODE.get(col.get("type"), "other")
        by_tier = {k: v.get("country_prices", {}).get("prices", []) for k, v in (s.get("prices") or {}).items()}
        normal = by_tier.get("0", [])
        cur = currency if _amount(normal, currency) is not None else "usd"
        items.append(Item(
            id=s["id"], name=s["name"], kind=kind, description=s.get("description") or "",
            collection=product.get("name", ""),
            price=_amount(normal, cur), nitro_price=_amount(by_tier.get(PREMIUM_NITRO, []), cur),
            currency=cur, orbs=(int(o) if (o := _amount(normal, "discord_orb")) is not None else None),
            premium=bool(s.get("premium")), exclusive=bool(s.get("exclusive")),
            bundled=list(dict.fromkeys(b.get("name", "") if isinstance(b, dict) else names.get(b, "") for b in s.get("bundled_skus") or [])),
            created=(product.get("created_at") or "")[:10],
        ))
    return items


# --------------------------------------------------------------------------- #
# Requête de l'utilisateur
# --------------------------------------------------------------------------- #
@dataclass
class Query:
    kind: str = "all"               # clé de TYPES ou "all"
    name: str = ""
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    nitro: Optional[bool] = None    # True: seulement ceux qui ont une réduction Nitro
    orbs: Optional[bool] = None     # True: achetables en orbs / False: sans orbs
    exclusive: Optional[bool] = None
    free_text: str = ""
    sort: str = "popular"           # SORTS ou LOCAL_SORTS
    reverse: bool = False
    limit: int = 15
    page: int = 1
    country: str = "DE"
    currency: str = "eur"
    scan: int = 100                 # nombre de SKU à parcourir côté serveur


_TRUE, _FALSE = {"1", "yes", "y", "true", "on", "oui"}, {"0", "no", "n", "false", "off", "non"}


def _bool(v: str) -> bool:
    v = v.lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ShopError(f"Valeur booléenne invalide: `{v}` (yes/no)")


def _num(key: str, v: str) -> float:
    try:
        return float(v.replace(",", "."))
    except ValueError:
        raise ShopError(f"`{key}` attend un nombre, reçu `{v}`") from None


def parse_query(text: str) -> Query:
    """``deco venom max=7 sort=price nitro=yes`` -> :class:`Query`. Les mots libres forment le nom."""
    q = Query()
    words: list[str] = []
    for tok in (text or "").split():
        if "=" not in tok:
            key = TYPE_ALIASES.get(tok.lower(), tok.lower())
            if key in TYPES or key == "all":
                q.kind = key
            else:
                words.append(tok)
            continue
        k, v = tok.split("=", 1)
        k = k.lower()
        if not v:
            raise ShopError(f"Valeur manquante pour `{k}`")
        if k == "type":
            key = TYPE_ALIASES.get(v.lower(), v.lower())
            if key not in TYPES and key != "all":
                raise ShopError(f"Type inconnu `{v}` ({', '.join(TYPES)}, all)")
            q.kind = key
        elif k in ("name", "n"):
            words.append(v)
        elif k in ("min", "from"):
            q.min_price = _num(k, v)
        elif k in ("max", "to"):
            q.max_price = _num(k, v)
        elif k == "nitro":
            q.nitro = _bool(v)
        elif k in ("orbs", "orb"):
            q.orbs = _bool(v)
        elif k == "exclusive":
            q.exclusive = _bool(v)
        elif k == "sort":
            v = v.lower()
            if v not in SORTS and v not in LOCAL_SORTS:
                raise ShopError(f"Tri inconnu `{v}` ({', '.join(list(SORTS) + list(LOCAL_SORTS))})")
            q.sort = v
        elif k in ("order", "dir"):
            q.reverse = v.lower() in ("desc", "d", "reverse")
        elif k in ("limit", "l"):
            q.limit = max(1, min(int(_num(k, v)), 40))
        elif k in ("page", "p"):
            q.page = max(1, int(_num(k, v)))
        elif k in ("country", "c"):
            if len(v) != 2 or not v.isalpha():
                raise ShopError("`country` attend un code pays à 2 lettres (ex: FR)")
            q.country = v.upper()
        elif k in ("currency", "cur"):
            if v.lower() not in ("eur", "usd"):
                raise ShopError("`currency` doit valoir eur ou usd")
            q.currency = v.lower()
        elif k == "scan":
            q.scan = max(20, min(int(_num(k, v)), MAX_FETCH))
        else:
            raise ShopError(f"Option inconnue `{k}`")
    q.name = " ".join(words).strip()
    return q


def apply_filters(items: Iterable[Item], q: Query) -> list[Item]:
    terms = q.name.lower().split()
    out = []
    for it in items:
        hay = f"{it.name} {it.collection} {it.description}".lower()
        if any(t not in hay for t in terms):
            continue
        if q.kind != "all" and it.kind != q.kind:
            continue
        if q.min_price is not None and (it.price is None or it.price < q.min_price):
            continue
        if q.max_price is not None and (it.price is None or it.price > q.max_price):
            continue
        if q.nitro is not None and (it.discount is not None) != q.nitro:
            continue
        if q.orbs is not None and (it.orbs is not None) != q.orbs:
            continue
        if q.exclusive is not None and it.exclusive != q.exclusive:
            continue
        out.append(it)

    if q.sort in LOCAL_SORTS:
        key = {
            "price": lambda i: (i.price is None, i.price or 0),
            "nitro": lambda i: (i.nitro_price is None, i.nitro_price or 0),
            "orbs": lambda i: (i.orbs is None, i.orbs or 0),
            "name": lambda i: i.name.lower(),
        }[q.sort]
        out.sort(key=key, reverse=q.reverse)
    elif q.reverse:
        out.reverse()
    return out


def paginate(items: list[Item], page: int, limit: int) -> tuple[list[Item], int]:
    pages = max(1, -(-len(items) // limit))
    page = min(page, pages)
    return items[(page - 1) * limit: page * limit], pages


# --------------------------------------------------------------------------- #
# Réseau
# --------------------------------------------------------------------------- #
_cache: dict[tuple, tuple[float, list[Item]]] = {}


def _headers(token: str) -> dict:
    return {"authorization": token, "accept": "*/*", "x-discord-locale": "en-US"}


async def _get(session: aiohttp.ClientSession, url: str, token: str, params) -> dict:
    async with session.get(url, params=params, headers=_headers(token), timeout=aiohttp.ClientTimeout(total=15)) as r:
        if r.status == 429:
            raise ShopError("Rate limit Discord: réessaie dans quelques secondes")
        if r.status in (401, 403):
            raise ShopError(f"Accès refusé par Discord (HTTP {r.status})")
        if r.status != 200:
            raise ShopError(f"Discord a répondu HTTP {r.status}")
        return await r.json()


async def fetch_items(session: aiohttp.ClientSession, token: str, q: Query) -> list[Item]:
    """Récupère (avec cache) les SKU correspondant au type/tri serveur de la requête."""
    server_sort = SORTS.get(q.sort, SORTS["popular"])
    api_type = TYPES[q.kind][0] if q.kind in TYPES else None
    ckey = (api_type, server_sort, q.country, q.currency, q.scan)
    hit = _cache.get(ckey)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]

    ids: list[str] = []
    while len(ids) < q.scan:
        params = {"offset": len(ids), "limit": min(BATCH, q.scan - len(ids)), "sort_type": server_sort[0],
                  "sort_direction": server_sort[1], "published_collections_only": "true"}
        if api_type:
            params["item_types"] = api_type
        data = await _get(session, f"{API}/shop/search", token, params)
        ids += data.get("skus", [])
        if not data.get("skus") or not data.get("pagination", {}).get("has_more"):
            break

    items: dict[str, Item] = {}
    for i in range(0, len(ids), BATCH):
        params = [("country_code", q.country), ("locale", "en-US"), ("with_bundled_skus", "true"),
                  ("include_google_sku_ids", "false"), ("ignore_cache", "false")]
        params += [("sku_ids", s) for s in ids[i:i + BATCH]]
        data = await _get(session, f"{API}/storefront/products/skus", token, params)
        for p in data.get("products", []):
            for it in parse_product(p, q.currency):
                items[it.id] = it
    # ordre serveur conservé, sans les variantes que la recherche n'a pas demandées
    result = [items[s] for s in ids if s in items]
    _cache[ckey] = (time.monotonic(), result)
    return result


def clear_cache() -> None:
    _cache.clear()


# --------------------------------------------------------------------------- #
# Affichage (Markdown « embed-like »)
# --------------------------------------------------------------------------- #
def fmt_price(value: Optional[float], currency: str) -> str:
    if value is None:
        return "—"
    sym = {"eur": "€", "usd": "$"}.get(currency, currency)
    return f"{value:.2f}{sym}" if sym == "€" else f"{sym}{value:.2f}"


def format_item(it: Item) -> str:
    icon = TYPES.get(it.kind, ("", "", "🛍️"))[2]
    bits = [fmt_price(it.price, it.currency)]
    if it.nitro_price is not None and it.discount:
        bits.append(f"Nitro {fmt_price(it.nitro_price, it.currency)} (-{it.discount}%)")
    if it.orbs is not None:
        bits.append(f"{it.orbs} orbs")
    if it.exclusive:
        bits.append("exclusive")
    extra = f" · *{it.collection}*" if it.collection and it.collection != it.name else ""
    line = f"> {icon} **{it.name}**{extra} · {' · '.join(bits)}"
    if it.bundled:
        line += f"\n> -# contains: {', '.join(it.bundled[:5])}" + (" …" if len(it.bundled) > 5 else "")
    return line


def usage(prefix: str) -> str:
    return (
        f"`{prefix}shop [type] [mots du nom] [options]`\n"
        f"> **Types** · {', '.join(TYPES)}, all\n"
        f"> **Filtres** · `min=` `max=` (prix) · `nitro=yes|no` · `orbs=yes|no` · `exclusive=yes|no` · `name=`\n"
        f"> **Tri** · `sort=` {', '.join(dict.fromkeys(list(SORTS) + list(LOCAL_SORTS)))} · `order=desc` inverse\n"
        f"> **Affichage** · `limit=` (≤40) · `page=` · `country=FR` · `currency=eur|usd` · `scan=` (SKU parcourus, ≤{MAX_FETCH})\n"
        f"> **Exemples** · `{prefix}shop deco max=7 sort=price` · `{prefix}shop venom` · `{prefix}shop bundle nitro=yes`"
    )

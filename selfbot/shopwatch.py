"""Surveillance du shop et comparaison d'items (logique pure, sans réseau)."""
from __future__ import annotations

from typing import Iterable

from .shop import Item, fmt_price


def diff_watch(seen: dict, items: Iterable[Item]) -> tuple:
    """Compare le catalogue à l'état mémorisé ``{id: prix}``.

    Renvoie (nouveaux items, baisses de prix [(item, ancien prix)], nouvel état).
    """
    new, drops, state = [], [], {}
    for it in items:
        state[it.id] = it.price
        if it.id not in seen:
            new.append(it)
        elif seen[it.id] is not None and it.price is not None and it.price < seen[it.id]:
            drops.append((it, seen[it.id]))
    return new, drops, state


def compare_items(a: Item, b: Item) -> list:
    """Lignes de comparaison entre deux items."""
    rows = []
    for label, x, y in (("Price", a.price, b.price), ("Nitro price", a.nitro_price, b.nitro_price)):
        if x is not None and y is not None:
            cheaper = a.name if x < y else b.name if y < x else "equal"
            rows.append(f"> **{label}** · {fmt_price(x, a.currency)} vs {fmt_price(y, b.currency)} · cheaper: {cheaper}")
    if a.orbs is not None and b.orbs is not None:
        rows.append(f"> **Orbs** · {a.orbs} vs {b.orbs}")
    rows.append(f"> **Type** · {a.kind} vs {b.kind}")
    return rows

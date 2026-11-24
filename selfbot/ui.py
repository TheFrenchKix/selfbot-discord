"""Mise en forme « façon embed » en Markdown Discord.

Un compte utilisateur ne peut pas envoyer d'embeds: on reproduit le rendu avec des titres,
des citations (``>``), du gras, du code et du sous-texte (``-#``).

    card("Stock", [("Month", 12), ("Year", 3)], icon="📦")
    ok("Keyword **nitro** added")
"""
from __future__ import annotations

from typing import Iterable, Optional, Sequence, Tuple, Union

Field = Tuple[str, object]

OK, ERR, WARN, INFO, WAIT = "✅", "❌", "⚠️", "ℹ️", "⏳"
DISCORD_LIMIT = 2000


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line.strip() else ">" for line in str(text).split("\n"))


def callout(icon: str, text: str, detail: str = "") -> str:
    """Bandeau d'une ou deux lignes: ``> ✅ texte``."""
    out = f"> {icon} {text}"
    if detail:
        out += "\n" + _quote(detail)
    return out


def ok(text: str, detail: str = "") -> str:
    return callout(OK, text, detail)


def err(text: str, detail: str = "") -> str:
    return callout(ERR, text, detail)


def warn(text: str, detail: str = "") -> str:
    return callout(WARN, text, detail)


def info(text: str, detail: str = "") -> str:
    return callout(INFO, text, detail)


def wait(text: str, detail: str = "") -> str:
    return callout(WAIT, text, detail)


def field(label: str, value: object, code: bool = False) -> str:
    value = f"`{value}`" if code else value
    return f"> **{label}** · {value}"


def card(
    title: str,
    fields: Iterable[Union[Field, str]] = (),
    *,
    icon: str = "",
    desc: str = "",
    footer: str = "",
    level: int = 2,
    code: bool = False,
) -> str:
    """Pseudo-embed: titre, description, champs ``label · valeur`` et pied de page."""
    head = f"{'#' * level} {icon + ' ' if icon else ''}{title}".rstrip()
    parts = [head]
    if desc:
        parts.append(desc)
    lines = [f if isinstance(f, str) else field(f[0], f[1], code) for f in fields]
    if lines:
        parts.append("\n".join(lines))
    if footer:
        parts.append(f"-# {footer}")
    return "\n".join(parts)


def bullets(items: Sequence[object], limit: Optional[int] = None) -> str:
    """Liste en citation: ``> • item``."""
    items = list(items)
    shown = items if limit is None else items[:limit]
    lines = [f"> • {i}" for i in shown]
    if limit is not None and len(items) > limit:
        lines.append(f"> … et {len(items) - limit} de plus")
    return "\n".join(lines)


def list_card(title: str, items: Sequence[object], *, icon: str = "", empty: str = "Nothing here yet",
              footer: str = "", limit: Optional[int] = None) -> str:
    """Carte listant des éléments, avec compteur; message vide propre."""
    if not items:
        return warn(f"{title}: {empty}")
    return card(f"{title} ({len(items)})", icon=icon, desc=bullets(items, limit), footer=footer)


def vouch(channel_mention: str, text: str) -> str:
    """Bloc à copier-coller dans un salon de vouch."""
    return f"## 💬 Vouch → {channel_mention}\n```{text}```"


def chunk(text: str, limit: int = DISCORD_LIMIT - 100) -> list[str]:
    """Découpe un long texte en messages < 2000 caractères, sur les sauts de ligne."""
    out, cur = [], ""
    for line in str(text).split("\n"):
        while len(line) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out or [""]


def yesno(value: bool) -> str:
    return "🟢 On" if value else "🔴 Off"

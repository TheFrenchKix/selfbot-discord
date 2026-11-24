"""Fonctions pures (testables sans Discord): parsing de messages, calcul sûr, fichiers d'IDs."""
from __future__ import annotations

import ast
import base64
import operator
import os
import re
from pathlib import Path
from typing import Iterable, Optional, Union


# --------------------------------------------------------------------------- #
# Calcul sûr (remplace eval)
# --------------------------------------------------------------------------- #
_BIN_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_MAX_POW = 1000


def _eval_node(node: ast.AST):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left, right = _eval_node(node.left), _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_POW:
            raise ValueError("exposant trop grand")
        return _BIN_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand))
    raise ValueError("expression non autorisée")


def calcul(expression: str) -> Union[int, float, str]:
    """Évalue une expression arithmétique (+ - * / // % ** et parenthèses).

    Aucun nom, appel de fonction ou attribut n'est accepté: pas d'exécution de code.
    Retourne ``"Erreur: ..."`` en cas d'échec (comportement historique).
    """
    try:
        expression = expression.replace("`", "").replace(",", ".").strip()
        if len(expression) > 200:
            raise ValueError("expression trop longue")
        return _eval_node(ast.parse(expression, mode="eval"))
    except Exception as exc:
        return f"Erreur: {exc}"


# --------------------------------------------------------------------------- #
# Parsing de messages
# --------------------------------------------------------------------------- #
def getUrlInMessage(message: str) -> Optional[str]:
    # URLs hors discord.com et *.discord.com
    matches = re.findall(r'(https?://(?!(?:[\w-]*\.)?discord\.com)\S+)', message)
    for match in matches:
        match = match[:-1] if match[-1] == ')' else match
        return match.replace('*', '')
    return None


def getPriceInMessage(message: str):
    msg_lower = message.lower()

    for match in re.findall(r'\b\d+(?:[.,]\d+)?\s?(?:€|e|\$|dol)', msg_lower):
        price = re.search(r'\d+(?:[.,]\d+)?', match).group()
        if '€' in match or 'e' in match:
            return price + '€'
        if '$' in match or 'dol' in match:
            return price + '$'
        return price

    for match in re.findall(r'\b\d+\.\d+\b', message):
        try:
            if 0.1 <= float(match) <= 10000:
                return match + '€'
        except ValueError:
            continue

    for match in re.findall(r'\b(\d+)\s+(?:ltc|btc|eth|usdt|eur|usd)\b', msg_lower):
        try:
            if 1 <= int(match) <= 10000:
                return match + '€'
        except ValueError:
            continue

    if re.search(r'\bfree\b', msg_lower):
        return 0
    return None


def getQuantityInMessage(message: str) -> Optional[str]:
    for match in re.findall(r'(\d+)\s?[x×k]|[x×k]\s?(\d+)', message.lower()):
        if match[0]:
            return f"{match[0]}x"
        if match[1]:
            return f"x{match[1]}"
    return None


def encode_code(code: str) -> str:
    return base64.b64encode(code.encode()).decode()


def decode_code(code: str) -> str:
    return base64.b64decode(code).decode()


def pluralize(word: str, count: int) -> str:
    return word if count == 1 else word + "s"


def parse_price_value(price: str) -> float:
    return float(str(price).replace('€', '').replace('$', '').replace(',', '.'))


# --------------------------------------------------------------------------- #
# Fichiers d'IDs
# --------------------------------------------------------------------------- #
def file_to_table(file: str | os.PathLike, table: set) -> set:
    """Charge un fichier d'IDs. Fichier absent => table inchangée (pas d'exception)."""
    p = Path(file)
    if not p.is_file():
        return table
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.lstrip("-").isdigit():
            table.add(int(line))
    return table


def table_to_file(file: str | os.PathLike, table: Iterable) -> None:
    """Écriture atomique (tmp + replace): jamais de fichier supprimé/perdu en cas d'erreur."""
    p = Path(file)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text("".join(f"{item}\n" for item in table), encoding="utf-8")
    os.replace(tmp, p)


def parse_list_setting(value: str) -> set[str]:
    """``"a, b,c"`` -> ``{"a","b","c"}`` (ignore les vides)."""
    return {part.strip() for part in (value or "").split(",") if part.strip()}


def is_error_result(result) -> bool:
    """Détecte les retours d'erreur de send_usdt_matic: ``{"error": ...}`` ou tuple ``({"error":..}, None)``."""
    if isinstance(result, tuple) and result:
        result = result[0]
    if isinstance(result, dict) and "error" in result:
        return True
    return not result

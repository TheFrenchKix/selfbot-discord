"""Chargement et validation de la configuration.

Les secrets viennent uniquement de l'environnement (ou d'un fichier ``.env``).
``config.ini`` ne contient plus que des réglages non sensibles (mots-clés...).
"""
from __future__ import annotations

import configparser
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional


class ConfigError(Exception):
    """Configuration invalide ou incomplète (message lisible pour l'utilisateur)."""


# --------------------------------------------------------------------------- #
# Utilitaires
# --------------------------------------------------------------------------- #
def mask_token(token: Optional[str]) -> str:
    """``MTQ4ODk...xyz`` : ne jamais afficher / logger un token complet."""
    if not token:
        return "<vide>"
    token = str(token)
    if len(token) <= 10:
        return "***"
    return f"{token[:3]}...{token[-3:]}"


def parse_dotenv(text: str) -> dict[str, str]:
    """Parseur minimal de ``.env`` (KEY=VALUE, commentaires, guillemets)."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            values[key] = value
    return values


def load_dotenv(path: str | os.PathLike = ".env", override: bool = False) -> bool:
    """Charge ``.env`` dans ``os.environ``. Retourne True si le fichier existe."""
    p = Path(path)
    if not p.is_file():
        return False
    for key, value in parse_dotenv(p.read_text(encoding="utf-8")).items():
        if override or key not in os.environ:
            os.environ[key] = value
    return True


def _split_csv(value: str) -> set[str]:
    return {part.strip() for part in re.split(r"[,\n]", value or "") if part.strip()}


def _to_int(value: Optional[str], name: str, default: Optional[int] = None) -> Optional[int]:
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(str(value).strip())
    except ValueError as exc:
        raise ConfigError(f"{name} doit être un entier (reçu: {value!r})") from exc


def _to_float(value: Optional[str], name: str, default: float) -> float:
    if value is None or str(value).strip() == "":
        return default
    try:
        return float(str(value).strip().replace(",", "."))
    except ValueError as exc:
        raise ConfigError(f"{name} doit être un nombre (reçu: {value!r})") from exc


def _to_id_set(value: Optional[str], name: str) -> set[int]:
    out: set[int] = set()
    for part in _split_csv(value or ""):
        n = _to_int(part, name)
        if n is not None:
            out.add(n)
    return out


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #
@dataclass
class ApiKeys:
    exchangerate: str = ""
    tatum: str = ""
    blockcypher: str = ""
    swapzone: str = ""
    fixedfloat_key: str = ""
    fixedfloat_secret: str = ""


@dataclass
class Channels:
    message: int = 0
    vouch: int = 0
    transaction: int = 0


@dataclass
class Filters:
    keywords: set[str] = field(default_factory=set)
    exclude_keywords: set[str] = field(default_factory=set)
    labels_check: set[str] = field(default_factory=set)
    max_price: float = 100.0


@dataclass
class Settings:
    token: str = ""
    api: ApiKeys = field(default_factory=ApiKeys)
    channels: Channels = field(default_factory=Channels)
    filters: Filters = field(default_factory=Filters)
    proxy_url: str = ""
    ltc_address: str = ""
    usdt_address: str = ""
    user_agent: str = ""
    command_prefix: str = "."
    ignored_guild_ids: set[int] = field(default_factory=set)
    owner_ids: set[int] = field(default_factory=set)
    instance_id: str = "default"
    data_dir: Path = Path(".")
    debug: bool = False
    swap_provider: str = "auto"          # auto | swapzone | fixedfloat
    swap_max_fee: float = 3.0            # % maximum sous le prix du marché

    # -- validation -------------------------------------------------------- #
    def validate(self, require_token: bool = True) -> "Settings":
        problems: list[str] = []
        if require_token and not self.token:
            problems.append("DISCORD_TOKEN est absent (voir .env.example)")
        if self.proxy_url and not re.match(r"^(https?|socks[45])://", self.proxy_url):
            problems.append("PROXY_URL doit commencer par http://, https:// ou socks5://")
        if self.filters.max_price < 0:
            problems.append("MAX_PRICE doit être positif")
        if self.swap_provider not in ("auto", "swapzone", "fixedfloat"):
            problems.append("SWAP_PROVIDER doit valoir auto, swapzone ou fixedfloat")
        if not 0 < self.swap_max_fee <= 50:
            problems.append("SWAP_MAX_FEE_PCT doit être compris entre 0 et 50")
        if problems:
            raise ConfigError("Configuration invalide:\n  - " + "\n  - ".join(problems))
        return self

    def public_dict(self) -> dict:
        """Représentation sans secret, utilisable dans les logs."""
        return {
            "token": mask_token(self.token),
            "proxy": "configuré" if self.proxy_url else "aucun",
            "instance_id": self.instance_id,
            "prefix": self.command_prefix,
            "debug": self.debug,
        }


def _read_ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    if path.is_file():
        parser.read(path, encoding="utf-8")
    return parser


def load_settings(
    env: Optional[Mapping[str, str]] = None,
    ini_path: str | os.PathLike = "config.ini",
    require_token: bool = True,
    read_dotenv: bool = True,
) -> Settings:
    """Construit les ``Settings``.

    Priorité: variables d'environnement > ``config.ini`` (réglages non secrets
    uniquement) > valeurs par défaut.
    """
    if env is None:
        if read_dotenv:
            load_dotenv(".env")
        env = os.environ

    ini = _read_ini(Path(ini_path))

    def ini_get(section: str, option: str, default: str = "") -> str:
        return ini.get(section, option, fallback=default)

    keywords = env.get("KEYWORDS") or ini_get("settings", "keywords")
    exclude = env.get("EXCLUDE_KEYWORDS") or ini_get("settings", "excludekeywords")
    labels = env.get("LABELS_CHECK") or ini_get("settings", "labelscheck")
    max_price = _to_float(env.get("MAX_PRICE") or ini_get("Filters", "max_price"), "MAX_PRICE", 100.0)

    settings = Settings(
        token=(env.get("DISCORD_TOKEN") or "").strip(),
        api=ApiKeys(
            exchangerate=env.get("EXCHANGERATE_API_KEY", ""),
            tatum=env.get("TATUM_API_KEY", ""),
            blockcypher=env.get("BLOCKCYPHER_TOKEN", ""),
            swapzone=env.get("SWAPZONE_API_KEY", ""),
            fixedfloat_key=env.get("FIXEDFLOAT_API_KEY", ""),
            fixedfloat_secret=env.get("FIXEDFLOAT_API_SECRET", ""),
        ),
        channels=Channels(
            message=_to_int(env.get("MSG_CHANNEL_ID"), "MSG_CHANNEL_ID", 0) or 0,
            vouch=_to_int(env.get("VOUCH_CHANNEL_ID"), "VOUCH_CHANNEL_ID", 0) or 0,
            transaction=_to_int(env.get("TRANSACTION_CHANNEL_ID"), "TRANSACTION_CHANNEL_ID", 0) or 0,
        ),
        filters=Filters(
            keywords={k.lower() for k in _split_csv(keywords)},
            exclude_keywords={k.lower() for k in _split_csv(exclude)},
            labels_check=_split_csv(labels),
            max_price=max_price,
        ),
        proxy_url=env.get("PROXY_URL", "").strip(),
        ltc_address=env.get("LTC_ADDRESS", "").strip(),
        usdt_address=env.get("USDT_ADDRESS", "").strip(),
        user_agent=env.get("USER_AGENT", "").strip(),
        command_prefix=env.get("COMMAND_PREFIX", ".") or ".",
        ignored_guild_ids=_to_id_set(env.get("IGNORED_GUILD_IDS"), "IGNORED_GUILD_IDS"),
        owner_ids=_to_id_set(env.get("OWNER_IDS"), "OWNER_IDS"),
        instance_id=env.get("INSTANCE_ID", "default").strip() or "default",
        data_dir=Path(env.get("DATA_DIR", ".")),
        debug=env.get("DEBUG", "").strip().lower() in ("1", "true", "yes", "on"),
        swap_provider=(env.get("SWAP_PROVIDER", "auto").strip().lower() or "auto"),
        swap_max_fee=_to_float(env.get("SWAP_MAX_FEE_PCT"), "SWAP_MAX_FEE_PCT", 3.0),
    )
    return settings.validate(require_token=require_token)


__all__ = [
    "ApiKeys", "Channels", "ConfigError", "Filters", "Settings",
    "load_dotenv", "load_settings", "mask_token", "parse_dotenv",
]

"""SQLite: remplace les ``.txt`` d'ID et les réécritures de ``config.ini``.

Tables:
  * ``id_sets``  (name, value)  : ensembles d'IDs (stocks, snipes, tickets, guilds)
  * ``kv``       (key, value)   : réglages (mots-clés, templates, délais...)
  * ``audit``    (ts, actor, action, detail) : actions du propriétaire
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS id_sets (name TEXT NOT NULL, value INTEGER NOT NULL, PRIMARY KEY (name, value));
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
                                  actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT);
"""


class Database:
    def __init__(self, path: str | Path = "selfbot.db"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- ensembles d'IDs ---------------------------------------------------- #
    def get_set(self, name: str) -> set[int]:
        with self._lock:
            rows = self._conn.execute("SELECT value FROM id_sets WHERE name=?", (name,)).fetchall()
        return {r[0] for r in rows}

    def replace_set(self, name: str, values: Iterable[int]) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM id_sets WHERE name=?", (name,))
            self._conn.executemany(
                "INSERT OR IGNORE INTO id_sets(name, value) VALUES (?, ?)", [(name, int(v)) for v in values]
            )

    def add_to_set(self, name: str, value: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT OR IGNORE INTO id_sets(name, value) VALUES (?, ?)", (name, int(value)))

    def remove_from_set(self, name: str, value: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM id_sets WHERE name=? AND value=?", (name, int(value)))

    # -- clé/valeur --------------------------------------------------------- #
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    # -- audit -------------------------------------------------------------- #
    def audit(self, actor: str, action: str, detail: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO audit(ts, actor, action, detail) VALUES (?, ?, ?, ?)",
                (time.time(), str(actor), action, detail),
            )

    def audit_log(self, limit: int = 20) -> list[tuple]:
        with self._lock:
            return self._conn.execute(
                "SELECT ts, actor, action, detail FROM audit ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    # -- migration depuis les anciens .txt ---------------------------------- #
    def import_txt(self, name: str, path: str | Path) -> int:
        """Importe un ancien fichier d'IDs (un par ligne). N'efface jamais le fichier."""
        p = Path(path)
        if not p.is_file():
            return 0
        n = 0
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.isdigit():
                self.add_to_set(name, int(line))
                n += 1
        return n


def open_db(path: Optional[str | Path] = None) -> Database:
    return Database(path or "selfbot.db")

"""Scout's own tables (notes, lists) in state/scout.db. Searchable text goes to the
memory service (memory_client); this holds the records themselves."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .config import DATA

DB = DATA / "state" / "scout.db"


class AppDB:
    def __init__(self, path: Path = DB) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self.last_rowid: int | None = None

    def sql(self, statement: str, args: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            cur = self._conn.execute(statement, tuple(args))
            rows = cur.fetchall()
            self.last_rowid = cur.lastrowid
            return rows

    def script(self, statements: str) -> None:
        with self._lock:
            self._conn.executescript(statements)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_db: AppDB | None = None


def default() -> AppDB:
    global _db
    if _db is None:
        _db = AppDB(DB)
    return _db

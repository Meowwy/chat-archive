"""SQLite connection helpers.

Read paths open the database read-only so the viewer can never corrupt the
archive; ingest and migration open it read-write.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config


def _tune(con: sqlite3.Connection, *, writable: bool) -> sqlite3.Connection:
    con.row_factory = sqlite3.Row
    # DHT enables ON DELETE CASCADE; we never delete and don't want it armed.
    con.execute("PRAGMA foreign_keys = OFF")
    if writable:
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = NORMAL")
    con.execute("PRAGMA busy_timeout = 10000")
    return con


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Read-write connection, for migrate and ingest."""
    path = path or config.require_db()
    return _tune(sqlite3.connect(str(path)), writable=True)


def connect_ro(path: Path | None = None) -> sqlite3.Connection:
    """Read-only connection, for the API's query endpoints."""
    path = path or config.require_db()
    uri = f"file:{Path(path).as_posix()}?mode=ro"
    return _tune(sqlite3.connect(uri, uri=True, check_same_thread=False), writable=False)


def table_columns(con: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA {schema}.table_info({table})")]

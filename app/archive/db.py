"""SQLite connection helpers.

Read paths open the database read-only so the viewer can never corrupt the
archive; ingest and migration open it read-write. Both take the path they are
given - deciding *which* archive is `Archive`'s job, not this module's.
"""

from __future__ import annotations

import sqlite3
import urllib.parse
from pathlib import Path


def _tune(con: sqlite3.Connection, *, writable: bool) -> sqlite3.Connection:
    con.row_factory = sqlite3.Row
    # DHT enables ON DELETE CASCADE; we never delete and don't want it armed.
    con.execute("PRAGMA foreign_keys = OFF")
    if writable:
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = NORMAL")
    con.execute("PRAGMA busy_timeout = 10000")
    return con


def connect(path: Path | str) -> sqlite3.Connection:
    """Read-write connection, for migrate and ingest."""
    return _tune(sqlite3.connect(str(path)), writable=True)


def ro_uri(path: Path | str) -> str:
    """A read-only SQLite URI for a path on this machine.

    With uri=True SQLite reads the filename as a URI, where "?" starts the
    query, "#" a fragment and "%" an escape - all of which are legal in a
    Windows path. Encode those, and leave the drive colon and separators alone.
    """
    return f"file:{urllib.parse.quote(Path(path).as_posix(), safe='/:')}?mode=ro"


def connect_ro(path: Path | str) -> sqlite3.Connection:
    """Read-only connection, for the API's query endpoints."""
    return _tune(
        sqlite3.connect(ro_uri(path), uri=True, check_same_thread=False), writable=False
    )


def table_columns(con: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA {schema}.table_info({table})")]

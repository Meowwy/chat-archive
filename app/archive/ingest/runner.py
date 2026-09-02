"""Orchestrates an ingest run: one transaction per source, logged to ingest_log.

A run either completes and commits, or rolls back entirely and records the
failure - the archive is never left half-updated.
"""

from __future__ import annotations

import sqlite3
import time
import traceback
from pathlib import Path
from typing import Callable, Iterator

from .. import db, migrate
from . import discord as discord_ingest
from .detect import ExportSource, detect
from .meta import MetaIngest, Stats

Progress = Callable[[str], None]


def _open_log(con: sqlite3.Connection, kind: str, path: str) -> int:
    cursor = con.execute(
        "INSERT INTO ingest_log (started_at, source_kind, source_path, status) VALUES (?, ?, ?, 'running')",
        (int(time.time() * 1000), kind, path),
    )
    con.commit()
    return cursor.lastrowid


def _close_log(
    con: sqlite3.Connection,
    run_id: int,
    stats: Stats,
    status: str,
    error: str | None = None,
) -> None:
    row = stats.as_row()
    con.execute(
        """
        UPDATE ingest_log SET
            finished_at = ?, threads_seen = ?, new_threads = ?, msgs_seen = ?,
            new_msgs = ?, dup_msgs = ?, media_seen = ?, new_media = ?,
            dup_media = ?, missing_media = ?, status = ?, error = ?
        WHERE run_id = ?
        """,
        (
            int(time.time() * 1000),
            row["threads_seen"], row["new_threads"], row["msgs_seen"],
            row["new_msgs"], row["dup_msgs"], row["media_seen"], row["new_media"],
            row["dup_media"], row["missing_media"], status, error, run_id,
        ),
    )
    con.commit()


def ingest_source(
    con: sqlite3.Connection,
    source: ExportSource,
    progress: Progress | None = None,
) -> Stats:
    """Ingest one detected export inside a single transaction."""
    progress = progress or (lambda _message: None)
    run_id = _open_log(con, source.kind, str(source.marker_dir))
    stats = Stats()
    try:
        con.execute("BEGIN")
        stats = MetaIngest(con, source, progress).run()
        con.execute("COMMIT")
    except BaseException as exc:
        con.execute("ROLLBACK")
        _close_log(con, run_id, stats, "failed", traceback.format_exc(limit=5))
        progress(f"[error] {source.kind}: {exc}")
        raise
    _close_log(con, run_id, stats, "ok")
    return stats


def ingest_discord_media(con: sqlite3.Connection, progress: Progress | None = None) -> Stats:
    progress = progress or (lambda _message: None)
    run_id = _open_log(con, "discord_media", str(Path(con.execute("PRAGMA database_list").fetchone()[2])))
    stats = Stats()
    try:
        con.execute("BEGIN")
        stats = discord_ingest.run(con, progress)
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        _close_log(con, run_id, stats, "failed", traceback.format_exc(limit=5))
        raise
    _close_log(con, run_id, stats, "ok")
    return stats


def ingest_path(path: str | Path, progress: Progress | None = None) -> list[tuple[str, Stats]]:
    """Detect and ingest every Meta export at `path`."""
    progress = progress or (lambda _message: None)
    sources = detect(path)
    con = db.connect()
    try:
        migrate.ensure_schema(con)
        results = []
        for source in sources:
            progress(f"[ingest] {source.label}: {len(source.thread_files)} thread(s) from {source.marker_dir}")
            results.append((source.kind, ingest_source(con, source, progress)))
        return results
    finally:
        con.close()


def stream_ingest(path: str | Path) -> Iterator[dict]:
    """Generator form used by the API's progress stream."""
    try:
        sources = detect(path)
    except ValueError as exc:
        yield {"event": "error", "message": str(exc)}
        return

    yield {"event": "detected", "sources": [source.summary() for source in sources]}

    con = db.connect()
    try:
        migrate.ensure_schema(con)
        for source in sources:
            messages: list[str] = []
            try:
                stats = ingest_source(con, source, messages.append)
            except Exception as exc:  # noqa: BLE001 - surfaced to the UI
                yield {"event": "error", "message": f"{source.label}: {exc}"}
                return
            yield {
                "event": "source-done",
                "kind": source.kind,
                "label": source.label,
                "stats": stats.as_row(),
                "missing_examples": stats.missing_examples,
            }
        yield {"event": "done"}
    finally:
        con.close()

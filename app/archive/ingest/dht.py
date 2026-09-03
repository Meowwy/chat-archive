"""Discord History Tracker (.dht) -> the archive.

A .dht file is itself a SQLite database, written by the tracker while you browse
Discord. Importing it is a row-for-row copy into the archive's own tables, and
it is append-only: every Discord row is keyed by a snowflake id, so importing
the same file twice - or a newer file that overlaps an older one - inserts only
what is genuinely new and never overwrites what is already there.

The two tables DHT keeps without a unique key of their own (embeds and
reactions) are deduplicated on their full content instead.

Attachments come along in the same pass. Discord's CDN links are signed and
expire about a day after they are issued, so the bytes the tracker embedded in
the file (`download_blobs`) are the only copy that survives - they are moved
into the media vault as part of the import rather than needing a second step.

The file is never read directly: SQLite's online backup API takes a consistent
snapshot first, which is safe even while the tracker has the file open.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Callable

from .. import db
from . import discord as discord_media
from .meta import Stats

Progress = Callable[[str], None]

# Copied on their primary key - INSERT OR IGNORE is enough to make the import
# append-only. Order matters only for readability; there are no FKs armed.
PK_TABLES = (
    "servers",
    "channels",
    "users",
    "messages",
    "attachments",
    "message_attachments",
    "message_replied_to",
    "message_edit_timestamps",
    "download_metadata",
    "download_blobs",
    "metadata",
)

# No unique key in DHT's schema, so these are deduplicated on their content.
CONTENT_TABLES = ("message_embeds", "message_reactions")


def _count(con: sqlite3.Connection, schema: str, table: str) -> int:
    return con.execute(f"SELECT COUNT(*) FROM {schema}.{table}").fetchone()[0]


def _tables(con: sqlite3.Connection, schema: str) -> set[str]:
    return {
        row[0]
        for row in con.execute(f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table'")
    }


def snapshot(source: Path) -> Path:
    """Consistent copy of a possibly-live .dht file, in the OS temp folder."""
    handle, name = tempfile.mkstemp(prefix="dht-import-", suffix=".sqlite")
    os.close(handle)
    target = Path(name)
    src = sqlite3.connect(db.ro_uri(source), uri=True)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return target


def _copy_pk_table(con: sqlite3.Connection, table: str, progress: Progress) -> int:
    """Append the rows of `src.<table>` that main does not already have."""
    src_cols = db.table_columns(con, table, "src")
    dst_cols = set(db.table_columns(con, table))
    if not src_cols or not dst_cols:
        return 0

    cols = [c for c in src_cols if c in dst_cols]
    unknown = [c for c in src_cols if c not in dst_cols]
    if unknown:
        # A newer tracker version storing something this archive has no column
        # for. Say so rather than dropping it silently.
        progress(f"[dht] note: {table}.{', '.join(unknown)} not stored - unknown to this archive")
    if not cols:
        return 0

    col_list = ", ".join(f'"{c}"' for c in cols)
    before = _count(con, "main", table)
    con.execute(
        f"INSERT OR IGNORE INTO main.{table} ({col_list}) SELECT {col_list} FROM src.{table}"
    )
    return _count(con, "main", table) - before


def _copy_embeds(con: sqlite3.Connection) -> int:
    before = _count(con, "main", "message_embeds")
    con.execute(
        """
        INSERT INTO main.message_embeds (message_id, json)
        SELECT s.message_id, s.json
        FROM src.message_embeds s
        WHERE NOT EXISTS (
            SELECT 1 FROM main.message_embeds m
            WHERE m.message_id = s.message_id AND m.json = s.json
        )
        """
    )
    return _count(con, "main", "message_embeds") - before


def _copy_reactions(con: sqlite3.Connection) -> int:
    before = _count(con, "main", "message_reactions")
    # No IS NOT DISTINCT FROM in older SQLite, so NULLs are compared by hand.
    con.execute(
        """
        INSERT INTO main.message_reactions (message_id, emoji_id, emoji_name, emoji_flags, count)
        SELECT s.message_id, s.emoji_id, s.emoji_name, s.emoji_flags, s.count
        FROM src.message_reactions s
        WHERE NOT EXISTS (
            SELECT 1 FROM main.message_reactions m
            WHERE m.message_id = s.message_id
              AND m.emoji_flags = s.emoji_flags
              AND ((m.emoji_id IS NULL AND s.emoji_id IS NULL) OR m.emoji_id = s.emoji_id)
              AND ((m.emoji_name IS NULL AND s.emoji_name IS NULL) OR m.emoji_name = s.emoji_name)
        )
        """
    )
    return _count(con, "main", "message_reactions") - before


def ingest(con: sqlite3.Connection, source: Path | str, progress: Progress | None = None) -> Stats:
    """Copy one .dht file into the connected archive. Owns its transaction.

    ATTACH cannot run inside a transaction, so unlike the Meta ingest this
    begins and commits itself; the caller only has to handle the exception.
    """
    progress = progress or (lambda _message: None)
    source = Path(source)
    stats = Stats()

    progress(f"[dht] snapshotting {source.name}")
    copy = snapshot(source)
    try:
        con.execute("ATTACH DATABASE ? AS src", (str(copy),))
        try:
            present = _tables(con, "src")
            handled = set(PK_TABLES) | set(CONTENT_TABLES)
            skipped = sorted(t for t in present - handled if not t.startswith("sqlite_"))
            if skipped:
                progress(f"[dht] note: not copied - {', '.join(skipped)}")

            stats.threads_seen = _count(con, "src", "channels")
            stats.msgs_seen = _count(con, "src", "messages")
            progress(
                f"[dht] {stats.threads_seen} channel(s), {stats.msgs_seen} message(s) in the file"
            )

            con.execute("BEGIN")
            try:
                for table in PK_TABLES:
                    if table not in present:
                        continue
                    added = _copy_pk_table(con, table, progress)
                    progress(f"[dht]   {table}: +{added}")
                    if table == "channels":
                        stats.new_threads = added
                    elif table == "messages":
                        stats.new_msgs = added
                if "message_embeds" in present:
                    progress(f"[dht]   message_embeds: +{_copy_embeds(con)}")
                if "message_reactions" in present:
                    progress(f"[dht]   message_reactions: +{_copy_reactions(con)}")
                stats.dup_msgs = stats.msgs_seen - stats.new_msgs

                # Now that the attachment rows exist, give them their bytes.
                discord_media.recover_blobs(con, stats, progress, pending_only=True)
                discord_media.link_downloaded(con, stats, progress)
                discord_media.backfill_mime(con)
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
        finally:
            con.execute("DETACH DATABASE src")
    finally:
        copy.unlink(missing_ok=True)

    return stats

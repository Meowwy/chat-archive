"""
Append-only mirror of the live Discord History Tracker (.dht) database
into a customizable SQLite file in the same folder.

- Reads the live DHT via SQLite's online backup API (safe while DHT is running).
- Inserts only NEW rows into the mirror; existing rows are never overwritten.
- Maintains a sync_log for visibility.

Run:  py sync_dht.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

ARCHIVE_DIR = Path(__file__).resolve().parent
SOURCE_DHT = ARCHIVE_DIR / "discord_archive.dht"
MIRROR_DB = ARCHIVE_DIR / "discord_archive_custom.sqlite"
TMP_DIR = ARCHIVE_DIR / "_tmp"
SNAPSHOT = TMP_DIR / "snapshot.sqlite"


# Schema for the mirror database. Column definitions match DHT exactly so
# existing queries continue to work. We do NOT redeclare foreign keys, since
# the DHT enables ON DELETE CASCADE which we explicitly do not want
# (append-only preservation).
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS servers (
    id   INTEGER PRIMARY KEY NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channels (
    id        INTEGER PRIMARY KEY NOT NULL,
    server    INTEGER NOT NULL,
    name      TEXT NOT NULL,
    parent_id INTEGER,
    position  INTEGER,
    topic     TEXT,
    nsfw      INTEGER
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY NOT NULL,
    name          TEXT NOT NULL,
    display_name  TEXT,
    avatar_url    TEXT,
    discriminator TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    message_id INTEGER PRIMARY KEY NOT NULL,
    sender_id  INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    text       TEXT NOT NULL,
    timestamp  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS attachments (
    attachment_id  INTEGER NOT NULL PRIMARY KEY NOT NULL,
    name           TEXT NOT NULL,
    type           TEXT,
    normalized_url TEXT NOT NULL,
    download_url   TEXT,
    size           INTEGER NOT NULL,
    width          INTEGER,
    height         INTEGER
);

CREATE TABLE IF NOT EXISTS message_attachments (
    message_id    INTEGER NOT NULL,
    attachment_id INTEGER NOT NULL,
    PRIMARY KEY (message_id, attachment_id)
);

CREATE TABLE IF NOT EXISTS message_embeds (
    message_id INTEGER NOT NULL,
    json       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS embeds_message_ix ON message_embeds(message_id);

CREATE TABLE IF NOT EXISTS message_reactions (
    message_id  INTEGER NOT NULL,
    emoji_id    INTEGER,
    emoji_name  TEXT,
    emoji_flags INTEGER NOT NULL,
    count       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS reactions_message_ix ON message_reactions(message_id);

CREATE TABLE IF NOT EXISTS message_replied_to (
    message_id    INTEGER PRIMARY KEY NOT NULL,
    replied_to_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS message_edit_timestamps (
    message_id     INTEGER PRIMARY KEY NOT NULL,
    edit_timestamp INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS download_metadata (
    normalized_url TEXT NOT NULL PRIMARY KEY,
    download_url   TEXT NOT NULL,
    status         INTEGER NOT NULL,
    type           TEXT,
    size           INTEGER
);

CREATE TABLE IF NOT EXISTS download_blobs (
    normalized_url TEXT NOT NULL PRIMARY KEY,
    blob           BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Custom tables --

CREATE TABLE IF NOT EXISTS downloaded_images (
    attachment_id     INTEGER PRIMARY KEY,
    image_id          TEXT UNIQUE,            -- e.g. 20260426_1234567890_1 (filename stem)
    message_id        INTEGER NOT NULL,
    channel_id        INTEGER NOT NULL,
    source_url        TEXT NOT NULL,
    local_path        TEXT,                   -- relative to D:/4 Archives/discord_image_archive/
    status            TEXT NOT NULL,
    http_status       INTEGER,
    file_size         INTEGER,
    sha256            TEXT,
    error             TEXT,
    attempt_count     INTEGER NOT NULL DEFAULT 0,
    last_attempt_at   INTEGER NOT NULL,
    downloaded_at     INTEGER
);
CREATE INDEX IF NOT EXISTS downloaded_images_status_ix ON downloaded_images(status);
CREATE INDEX IF NOT EXISTS downloaded_images_message_ix ON downloaded_images(message_id);

CREATE TABLE IF NOT EXISTS downloaded_files (
    attachment_id     INTEGER PRIMARY KEY,
    file_id           TEXT UNIQUE,            -- e.g. 20260426_1234567890_1 (filename stem)
    message_id        INTEGER NOT NULL,
    channel_id        INTEGER NOT NULL,
    source_url        TEXT NOT NULL,
    local_path        TEXT,                   -- relative to D:/4 Archives/discord_image_archive/
    mime_type         TEXT,
    status            TEXT NOT NULL,
    http_status       INTEGER,
    file_size         INTEGER,
    sha256            TEXT,
    error             TEXT,
    attempt_count     INTEGER NOT NULL DEFAULT 0,
    last_attempt_at   INTEGER NOT NULL,
    downloaded_at     INTEGER
);
CREATE INDEX IF NOT EXISTS downloaded_files_status_ix ON downloaded_files(status);
CREATE INDEX IF NOT EXISTS downloaded_files_message_ix ON downloaded_files(message_id);

CREATE TABLE IF NOT EXISTS sync_log (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      INTEGER NOT NULL,
    finished_at     INTEGER,
    new_messages    INTEGER,
    new_attachments INTEGER,
    new_blobs       INTEGER,
    notes           TEXT
);
"""


# Per-table append-only copy strategies.
#
# For tables with a PK (or composite PK), INSERT OR IGNORE is sufficient.
# For embeds/reactions which have no natural unique key, we use a
# NOT EXISTS subquery to dedupe by full-row content.
COPY_PK_TABLES = [
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
]


def ensure_mirror_schema(con: sqlite3.Connection) -> None:
    # The viewer app owns the schema - the tables below plus its own additions
    # (platform columns, people, FTS) - so that this script and the app can
    # never disagree about it. SCHEMA_SQL above is only the offline fallback.
    sys.path.insert(0, str(ARCHIVE_DIR.parent / "app"))
    try:
        from archive.migrate import ensure_schema
    except ImportError:
        print("[sync] note: archive package not found, using the local schema")
        con.executescript(SCHEMA_SQL)
        con.commit()
        return
    ensure_schema(con)


def snapshot_source() -> None:
    """Use SQLite's online backup API to copy the live DHT to a temp file.

    This is safe to run while the DHT app is writing.
    """
    TMP_DIR.mkdir(exist_ok=True)
    if SNAPSHOT.exists():
        SNAPSHOT.unlink()

    src_uri = f"file:{SOURCE_DHT.as_posix()}?mode=ro"
    src = sqlite3.connect(src_uri, uri=True)
    try:
        dst = sqlite3.connect(str(SNAPSHOT))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def copy_pk_table(con: sqlite3.Connection, table: str) -> int:
    # Name the source columns explicitly rather than SELECT *. The mirror has
    # extra columns the DHT source does not (platform, sha256, ...); those take
    # their declared defaults, which is exactly what we want for Discord rows.
    src_cols = [r[1] for r in con.execute(f"PRAGMA src.table_info({table})")]
    dst_cols = {r[1] for r in con.execute(f"PRAGMA main.table_info({table})")}
    cols = [c for c in src_cols if c in dst_cols]
    if not cols:
        return 0
    col_list = ", ".join(f'"{c}"' for c in cols)

    before = con.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0]
    con.execute(
        f"INSERT OR IGNORE INTO main.{table} ({col_list}) "
        f"SELECT {col_list} FROM src.{table}"
    )
    after = con.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0]
    return after - before


def copy_embeds(con: sqlite3.Connection) -> int:
    before = con.execute("SELECT COUNT(*) FROM main.message_embeds").fetchone()[0]
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
    after = con.execute("SELECT COUNT(*) FROM main.message_embeds").fetchone()[0]
    return after - before


def copy_reactions(con: sqlite3.Connection) -> int:
    before = con.execute("SELECT COUNT(*) FROM main.message_reactions").fetchone()[0]
    # IS NOT DISTINCT FROM not available in older SQLite — emulate with
    # explicit NULL handling for emoji_id/emoji_name.
    con.execute(
        """
        INSERT INTO main.message_reactions (message_id, emoji_id, emoji_name, emoji_flags, count)
        SELECT s.message_id, s.emoji_id, s.emoji_name, s.emoji_flags, s.count
        FROM src.message_reactions s
        WHERE NOT EXISTS (
            SELECT 1 FROM main.message_reactions m
            WHERE m.message_id = s.message_id
              AND m.emoji_flags = s.emoji_flags
              AND ( (m.emoji_id IS NULL AND s.emoji_id IS NULL) OR m.emoji_id = s.emoji_id )
              AND ( (m.emoji_name IS NULL AND s.emoji_name IS NULL) OR m.emoji_name = s.emoji_name )
        )
        """
    )
    after = con.execute("SELECT COUNT(*) FROM main.message_reactions").fetchone()[0]
    return after - before


def main() -> int:
    if not SOURCE_DHT.exists():
        print(f"ERROR: source not found: {SOURCE_DHT}", file=sys.stderr)
        return 2

    started_at = int(time.time() * 1000)
    print(f"[sync] source: {SOURCE_DHT}")
    print(f"[sync] mirror: {MIRROR_DB}")

    print("[sync] snapshotting live DHT (online backup API)...")
    t0 = time.time()
    snapshot_source()
    print(f"[sync]   snapshot done in {time.time() - t0:.1f}s, size={SNAPSHOT.stat().st_size:,} B")

    con = sqlite3.connect(str(MIRROR_DB))
    try:
        con.execute("PRAGMA foreign_keys = OFF")  # we never delete; FKs would just slow inserts
        ensure_mirror_schema(con)

        con.execute(f"ATTACH DATABASE ? AS src", (str(SNAPSHOT),))
        try:
            print("[sync] copying tables (append-only)...")
            inserted = {}
            con.execute("BEGIN")
            try:
                for tbl in COPY_PK_TABLES:
                    n = copy_pk_table(con, tbl)
                    inserted[tbl] = n
                    print(f"[sync]   {tbl}: +{n}")
                inserted["message_embeds"] = copy_embeds(con)
                print(f"[sync]   message_embeds: +{inserted['message_embeds']}")
                inserted["message_reactions"] = copy_reactions(con)
                print(f"[sync]   message_reactions: +{inserted['message_reactions']}")

                finished_at = int(time.time() * 1000)
                con.execute(
                    """
                    INSERT INTO sync_log
                        (started_at, finished_at, new_messages, new_attachments, new_blobs, notes)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        started_at,
                        finished_at,
                        inserted.get("messages", 0),
                        inserted.get("attachments", 0),
                        inserted.get("download_blobs", 0),
                        None,
                    ),
                )
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        finally:
            con.execute("DETACH DATABASE src")
    finally:
        con.close()

    try:
        SNAPSHOT.unlink()
    except OSError:
        pass

    print("[sync] done.")
    print(f"[sync] new messages: {inserted.get('messages', 0):,}")
    print(f"[sync] new attachments: {inserted.get('attachments', 0):,}")
    print(f"[sync] new download_blobs: {inserted.get('download_blobs', 0):,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Idempotent schema migrations for the unified archive database.

The database started life as an append-only mirror of Discord History Tracker's
`.dht` file (see Archives/sync_dht.py). Every DHT table keeps its original shape
and column order; we only ever *append* columns, all with defaults, so the
mirror's `INSERT ... SELECT` copy keeps working and existing queries are
unaffected.

Migrations are gated on `PRAGMA user_version` and each one is written to be
safely re-runnable.
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, db, noise

SCHEMA_VERSION = 2


def _has_column(con: sqlite3.Connection, table: str, column: str) -> bool:
    return column in db.table_columns(con, table)


def _add_column(con: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    if not _has_column(con, table, column):
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


# --- the base tables ------------------------------------------------------
# Discord History Tracker's own shapes, verbatim, so `Archives/sync_dht.py`
# can keep mirroring into this database. Existing archives already have every
# one of these; running them matters only when creating an empty archive from
# scratch, which is what makes the database pluggable. This is the single
# source of truth - sync_dht.py falls back to its own copy only when the
# archive package cannot be imported at all.
_BASE_TABLES = """
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


# --- new columns on the DHT-mirrored tables ------------------------------
# 'platform' defaults to 'discord', so the 134k rows already present are
# correctly labelled without a backfill pass.
_COLUMNS: list[tuple[str, str, str]] = [
    ("servers", "platform", "TEXT NOT NULL DEFAULT 'discord'"),
    ("channels", "platform", "TEXT NOT NULL DEFAULT 'discord'"),
    ("channels", "avatar_sha256", "TEXT"),
    ("users", "platform", "TEXT NOT NULL DEFAULT 'discord'"),
    ("users", "person_id", "INTEGER"),
    ("users", "avatar_sha256", "TEXT"),
    ("messages", "platform", "TEXT NOT NULL DEFAULT 'discord'"),
    ("messages", "source_key", "TEXT"),
    ("messages", "is_unsent", "INTEGER NOT NULL DEFAULT 0"),
    ("attachments", "platform", "TEXT NOT NULL DEFAULT 'discord'"),
    ("attachments", "local_path", "TEXT"),
    ("attachments", "sha256", "TEXT"),
]

# Columns on tables this package owns, applied after they are created.
_LATE_COLUMNS: list[tuple[str, str, str]] = [
    ("ingest_log", "skipped_notices", "INTEGER DEFAULT 0"),
]

_TABLES = """
CREATE TABLE IF NOT EXISTS people (
    person_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    display     TEXT NOT NULL,
    is_self     INTEGER NOT NULL DEFAULT 0,
    notes       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS people_display_ux ON people(display);

-- Meta records reactions per actor; DHT's message_reactions only aggregates by
-- emoji. Keep both so the UI can show counts and who reacted.
CREATE TABLE IF NOT EXISTS message_reaction_actors (
    message_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    emoji_name TEXT NOT NULL,
    PRIMARY KEY (message_id, user_id, emoji_name)
);

CREATE TABLE IF NOT EXISTS channel_participants (
    channel_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    PRIMARY KEY (channel_id, user_id)
);

CREATE TABLE IF NOT EXISTS ingest_log (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   INTEGER NOT NULL,
    finished_at  INTEGER,
    source_kind  TEXT NOT NULL,
    source_path  TEXT NOT NULL,
    threads_seen INTEGER DEFAULT 0,
    new_threads  INTEGER DEFAULT 0,
    msgs_seen    INTEGER DEFAULT 0,
    new_msgs     INTEGER DEFAULT 0,
    dup_msgs     INTEGER DEFAULT 0,
    media_seen   INTEGER DEFAULT 0,
    new_media    INTEGER DEFAULT 0,
    dup_media    INTEGER DEFAULT 0,
    missing_media INTEGER DEFAULT 0,
    skipped_notices INTEGER DEFAULT 0,
    status       TEXT NOT NULL,
    error        TEXT
);
"""

_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS messages_source_key_ux
    ON messages(source_key) WHERE source_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS messages_channel_ts_ix ON messages(channel_id, timestamp);
CREATE INDEX IF NOT EXISTS messages_platform_ix   ON messages(platform);
CREATE INDEX IF NOT EXISTS messages_sender_ix     ON messages(sender_id);
CREATE INDEX IF NOT EXISTS attachments_sha_ix     ON attachments(sha256);
CREATE INDEX IF NOT EXISTS channels_platform_ix   ON channels(platform);
CREATE INDEX IF NOT EXISTS users_person_ix        ON users(person_id);
CREATE INDEX IF NOT EXISTS reaction_actors_msg_ix ON message_reaction_actors(message_id);
"""

# messages.message_id is INTEGER PRIMARY KEY, i.e. the rowid itself, so an
# external-content FTS5 index maps onto it directly. Negative rowids (our
# synthetic Meta ids) are perfectly legal.
# remove_diacritics 2 is what lets 'necekal' find 'nečekal'.
_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text,
    content='messages',
    content_rowid='message_id',
    tokenize="unicode61 remove_diacritics 2"
);

CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.message_id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text)
        VALUES ('delete', old.message_id, old.text);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE OF text ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text)
        VALUES ('delete', old.message_id, old.text);
    INSERT INTO messages_fts(rowid, text) VALUES (new.message_id, new.text);
END;
"""


def backup(path: Path | None = None) -> Path:
    """Snapshot the database next to itself before any DDL runs."""
    path = Path(path or config.DB_PATH)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copyfile(path, dest)
    return dest


def ensure_schema(con: sqlite3.Connection) -> None:
    """Apply the whole schema. Safe to call on every startup, and on an empty
    file - which is how `create_archive()` builds a new archive."""
    con.executescript(_BASE_TABLES)
    for table, column, decl in _COLUMNS:
        _add_column(con, table, column, decl)
    con.executescript(_TABLES)
    for table, column, decl in _LATE_COLUMNS:
        _add_column(con, table, column, decl)
    con.executescript(_INDEXES)
    con.executescript(_FTS)
    con.commit()


def rebuild_fts(con: sqlite3.Connection) -> int:
    """(Re)build the full-text index from the messages table."""
    con.execute("INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")
    con.commit()
    return con.execute("SELECT count(*) FROM messages_fts").fetchone()[0]


def create_archive(path: Path | str, *, verbose: bool = True) -> Path:
    """Build an empty but complete archive at `path`.

    Everything the viewer needs is here from the start: DHT's tables, our added
    columns, the people table and the full-text index. Ingest a Meta export or
    point `sync_dht.py` at it and it fills up.
    """
    path = Path(path).expanduser().resolve()
    if path.exists():
        raise FileExistsError(f"There is already a file at {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = db.connect(path)
    try:
        ensure_schema(con)
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        con.commit()
    finally:
        con.close()
    if verbose:
        print(f"[migrate] new archive -> {path}")
    return path


def migrate(*, make_backup: bool = True, verbose: bool = True) -> int:
    path = config.require_db()

    con = db.connect(path)
    try:
        current = con.execute("PRAGMA user_version").fetchone()[0]
        if current >= SCHEMA_VERSION:
            ensure_schema(con)  # cheap, and heals a partially-applied run
            if verbose:
                print(f"[migrate] already at version {current}; schema verified")
            return current

        if make_backup:
            dest = backup(path)
            if verbose:
                print(f"[migrate] backup -> {dest.name} ({dest.stat().st_size:,} B)")

        t0 = time.time()
        ensure_schema(con)
        if verbose:
            print("[migrate] schema applied")

        # Instagram's "Reacted 😂 to your message" pseudo-messages are dropped
        # at ingest time now; clear the ones earlier runs let through.
        gone = noise.purge_reaction_notices(con, verbose=verbose)
        if verbose and not gone:
            print("[migrate] no reaction notices to clean")

        n = rebuild_fts(con)
        if verbose:
            print(f"[migrate] full-text index built: {n:,} rows in {time.time() - t0:.1f}s")

        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        con.commit()
        if verbose:
            print(f"[migrate] user_version -> {SCHEMA_VERSION}")
        return SCHEMA_VERSION
    finally:
        con.close()


if __name__ == "__main__":
    migrate()

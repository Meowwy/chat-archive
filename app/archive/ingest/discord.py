"""Fold whatever Discord media we actually have into the vault.

Discord CDN links are signed and expire roughly 24h after they are issued, so
the overwhelming majority of scraped attachments now return HTTP 404 and are
gone for good. This module deliberately makes no network calls; it only
recovers what already exists locally: the attachment and avatar bytes the
tracker embedded in the `.dht` file itself, in its `download_blobs` table. That
is free, offline, and the only copy of a Discord attachment that survives.

Everything else keeps its `attachments` row (name, type, size, dimensions, dead
URL) so the viewer can render an informative placeholder rather than a broken
image.
"""

from __future__ import annotations

import mimetypes
import re
import sqlite3
from pathlib import Path
from typing import Callable

from ..vault import Vault
from .meta import Stats

Progress = Callable[[str], None]

_AVATAR_RE = re.compile(r"/avatars/(\d+)/([0-9a-f]+)\.", re.I)


# Blobs whose bytes are still needed by something: an attachment with nothing
# in the vault yet, or a user whose avatar has not been stored. Anything else
# has already been recovered, and re-reading it would mean re-hashing every
# embedded byte on every import.
_PENDING_BLOBS = """
WHERE EXISTS (
        SELECT 1 FROM attachments a
        WHERE a.normalized_url = b.normalized_url
          AND (a.sha256 IS NULL OR a.local_path IS NULL)
      )
   OR EXISTS (
        SELECT 1 FROM users u
        WHERE u.avatar_sha256 IS NULL AND u.avatar_url IS NOT NULL
          AND b.normalized_url LIKE '%/avatars/%/' || u.avatar_url || '.%'
      )
"""


def recover_blobs(
    con: sqlite3.Connection,
    vault: Vault,
    stats: Stats,
    progress: Progress,
    *,
    pending_only: bool = False,
) -> None:
    """Move DHT-embedded blobs into the vault and link them to their rows.

    `pending_only` skips blobs that have already been recovered - what an import
    wants. The full sweep also re-stores anything deleted from the vault since,
    which is why `discord-media` asks for it.
    """
    where = _PENDING_BLOBS if pending_only else ""
    count = con.execute(f"SELECT COUNT(*) FROM download_blobs b {where}").fetchone()[0]
    progress(f"[discord] {count} embedded blob(s) to recover")

    # Counted separately so the blobs themselves are read one row at a time.
    for row in con.execute(f"SELECT b.normalized_url, b.blob FROM download_blobs b {where}"):
        url = row["normalized_url"]
        blob = row["blob"]
        stats.media_seen += 1

        suffix = Path(url.split("?")[0]).suffix or ".bin"
        sha256, relpath, _size, was_new = vault.put_bytes(blob, suffix)
        stats.new_media += 1 if was_new else 0
        stats.dup_media += 0 if was_new else 1

        updated = con.execute(
            """
            UPDATE attachments SET sha256 = ?, local_path = ?
            WHERE normalized_url = ? AND (sha256 IS NULL OR local_path IS NULL)
            """,
            (sha256, relpath, url),
        ).rowcount

        # Avatars are not attachments; match them back to their user instead.
        match = _AVATAR_RE.search(url)
        if match:
            user_id, avatar_hash = int(match.group(1)), match.group(2)
            updated += con.execute(
                "UPDATE users SET avatar_sha256 = ? WHERE id = ? AND avatar_url = ?",
                (sha256, user_id, avatar_hash),
            ).rowcount
        if not updated:
            # Emoji and stale avatars land here: stored in the vault, unlinked.
            pass


def backfill_mime(con: sqlite3.Connection) -> int:
    """Fill in missing attachment MIME types from the filename."""
    rows = con.execute(
        "SELECT attachment_id, name FROM attachments WHERE type IS NULL"
    ).fetchall()
    updated = 0
    for row in rows:
        guess = mimetypes.guess_type(row["name"])[0]
        if guess:
            con.execute(
                "UPDATE attachments SET type = ? WHERE attachment_id = ?",
                (guess, row["attachment_id"]),
            )
            updated += 1
    return updated


def run(con: sqlite3.Connection, vault: Vault, progress: Progress | None = None) -> Stats:
    """Everything `py -m archive discord-media` does: a full offline sweep."""
    progress = progress or (lambda _message: None)
    stats = Stats()
    recover_blobs(con, vault, stats, progress)
    backfill_mime(con)
    return stats

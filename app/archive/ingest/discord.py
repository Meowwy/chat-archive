"""Fold whatever Discord media we actually have into the vault.

Discord CDN links are signed and expire roughly 24h after they are issued, so
the overwhelming majority of scraped attachments now return HTTP 404 and are
gone for good. This module deliberately makes no network calls; it only
recovers what already exists locally:

1. `download_blobs` - attachment and avatar bytes embedded in the DHT file
   itself. Free, offline, and always works.
2. The successful downloads produced by download_images.py / download_files.py,
   which live on the external drive.

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

from .. import config, vault
from .meta import Stats

Progress = Callable[[str], None]

_AVATAR_RE = re.compile(r"/avatars/(\d+)/([0-9a-f]+)\.", re.I)


def recover_blobs(con: sqlite3.Connection, stats: Stats, progress: Progress) -> None:
    """Move DHT-embedded blobs into the vault and link them to their rows."""
    rows = con.execute("SELECT normalized_url, blob FROM download_blobs").fetchall()
    progress(f"[discord] {len(rows)} embedded blob(s) in the DHT")

    for row in rows:
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


def link_downloaded(con: sqlite3.Connection, stats: Stats, progress: Progress) -> None:
    """Fold previously downloaded attachments into the vault."""
    roots = [root for root in config.LEGACY_DISCORD_MEDIA if root.is_dir()]
    if not roots:
        progress("[discord] no legacy download folders present, skipping")
        return

    for table in ("downloaded_images", "downloaded_files"):
        rows = con.execute(
            f"""
            SELECT d.attachment_id, d.local_path, a.name
            FROM {table} d
            JOIN attachments a ON a.attachment_id = d.attachment_id
            WHERE d.status = 'success' AND d.local_path IS NOT NULL
              AND (a.sha256 IS NULL OR a.local_path IS NULL)
            """
        ).fetchall()
        progress(f"[discord] {table}: {len(rows)} successful download(s) to fold in")

        for row in rows:
            source = _first_existing(roots, row["local_path"])
            stats.media_seen += 1
            if source is None:
                stats.missing_media += 1
                if len(stats.missing_examples) < 5:
                    stats.missing_examples.append(row["local_path"])
                continue
            sha256, relpath, _size, was_new = vault.put(source)
            stats.new_media += 1 if was_new else 0
            stats.dup_media += 0 if was_new else 1
            con.execute(
                "UPDATE attachments SET sha256 = ?, local_path = ? WHERE attachment_id = ?",
                (sha256, relpath, row["attachment_id"]),
            )

    _link_loose_files(con, stats, progress)


def _first_existing(roots: list[Path], relative: str) -> Path | None:
    for root in roots:
        candidate = root / relative
        if candidate.is_file():
            return candidate
    return None


_LOOSE_RE = re.compile(r"msg(\d+)_att(\d+)_", re.I)


def _link_loose_files(con: sqlite3.Connection, stats: Stats, progress: Progress) -> None:
    """Pick up files named by an older script: msg<id>_att<id>_<kind>.<ext>."""
    folder = config.ARCHIVES_DIR / "images"
    if not folder.is_dir():
        return
    found = 0
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        match = _LOOSE_RE.match(path.name)
        if not match:
            continue
        attachment_id = int(match.group(2))
        row = con.execute(
            "SELECT sha256, local_path FROM attachments WHERE attachment_id = ?",
            (attachment_id,),
        ).fetchone()
        if row is None or (row["sha256"] and row["local_path"]):
            continue
        sha256, relpath, _size, was_new = vault.put(path)
        stats.media_seen += 1
        stats.new_media += 1 if was_new else 0
        stats.dup_media += 0 if was_new else 1
        con.execute(
            "UPDATE attachments SET sha256 = ?, local_path = ? WHERE attachment_id = ?",
            (sha256, relpath, attachment_id),
        )
        found += 1
    if found:
        progress(f"[discord] linked {found} loose file(s) from Archives/images")


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


def run(con: sqlite3.Connection, progress: Progress | None = None) -> Stats:
    progress = progress or (lambda _message: None)
    stats = Stats()
    recover_blobs(con, stats, progress)
    link_downloaded(con, stats, progress)
    backfill_mime(con)
    return stats

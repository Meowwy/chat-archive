"""Facebook / Instagram JSON -> unified archive tables.

Every Meta row gets a negative synthetic id (see ids.synth_id), so Discord's
positive snowflakes and Meta's ids can never collide inside the shared tables.

The adapter is idempotent: message identity is a content-derived `source_key`,
and every child row (attachments, reactions, embeds) is written through a
primary key or a scoped delete-then-insert, so re-ingesting an overlapping
export is a no-op apart from picking up newly-added reactions.
"""

from __future__ import annotations

import json
import mimetypes
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import noise, vault
from ..ids import demojibake, media_entries, message_source_key, synth_id
from .detect import ExportSource

Progress = Callable[[str], None]


@dataclass
class Stats:
    threads_seen: int = 0
    new_threads: int = 0
    msgs_seen: int = 0
    new_msgs: int = 0
    dup_msgs: int = 0
    media_seen: int = 0
    new_media: int = 0
    dup_media: int = 0
    missing_media: int = 0
    skipped_notices: int = 0
    missing_examples: list[str] = field(default_factory=list)

    def merge(self, other: "Stats") -> None:
        for key, value in vars(other).items():
            current = getattr(self, key)
            if isinstance(current, list):
                current.extend(value[: max(0, 5 - len(current))])
            else:
                setattr(self, key, current + value)

    def as_row(self) -> dict:
        return {k: v for k, v in vars(self).items() if not isinstance(v, list)}


class MetaIngest:
    def __init__(
        self,
        con: sqlite3.Connection,
        source: ExportSource,
        progress: Progress | None = None,
    ):
        self.con = con
        self.source = source
        self.platform = source.kind
        self.progress = progress or (lambda _message: None)
        self.stats = Stats()

    # -- ids -------------------------------------------------------------
    def _channel_id(self, thread_path: str) -> int:
        return synth_id(self.platform, thread_path)

    def _user_id(self, name: str) -> int:
        return synth_id(self.platform, "user", name)

    # -- media -----------------------------------------------------------
    def _resolve(self, uri: str) -> Path | None:
        """Locate an export-relative media URI on disk."""
        for base in (self.source.media_root, self.source.marker_dir):
            candidate = base / uri
            if candidate.is_file():
                return candidate
        # URIs are prefixed with the marker folder name; try stripping it.
        parts = Path(uri).parts
        if parts and parts[0] == self.source.marker_dir.name:
            candidate = self.source.marker_dir / Path(*parts[1:])
            if candidate.is_file():
                return candidate
        return None

    def _store_media(self, uri: str) -> tuple[int, str | None, str | None]:
        """Copy one media file into the vault and upsert its attachment row.

        Returns (attachment_id, sha256, vault_relpath). sha256 is None when the
        export references a file that is missing from disk.
        """
        self.stats.media_seen += 1
        attachment_id = synth_id(self.platform, "attachment", uri)
        name = Path(uri).name

        row = self.con.execute(
            "SELECT sha256, local_path FROM attachments WHERE attachment_id = ?",
            (attachment_id,),
        ).fetchone()
        if row and row["sha256"] and row["local_path"] and vault.exists(row["local_path"]):
            self.stats.dup_media += 1
            return attachment_id, row["sha256"], row["local_path"]

        source_file = self._resolve(uri)
        if source_file is None:
            self.stats.missing_media += 1
            if len(self.stats.missing_examples) < 5:
                self.stats.missing_examples.append(uri)
            sha256 = relpath = None
            size = 0
        else:
            sha256, relpath, size, was_new = vault.put(source_file)
            if was_new:
                self.stats.new_media += 1
            else:
                self.stats.dup_media += 1

        self.con.execute(
            """
            INSERT INTO attachments
                (attachment_id, name, type, normalized_url, download_url,
                 size, width, height, platform, local_path, sha256)
            VALUES (?, ?, ?, ?, NULL, ?, NULL, NULL, ?, ?, ?)
            ON CONFLICT(attachment_id) DO UPDATE SET
                local_path = excluded.local_path,
                sha256     = excluded.sha256,
                size       = excluded.size
            """,
            (
                attachment_id,
                name,
                mimetypes.guess_type(name)[0],
                uri,
                size,
                self.platform,
                relpath,
                sha256,
            ),
        )
        return attachment_id, sha256, relpath

    # -- thread ----------------------------------------------------------
    def ingest_thread(self, path: Path) -> None:
        raw = json.loads(path.read_bytes())
        thread_path = raw.get("thread_path") or f"inbox/{path.parent.name}"
        channel_id = self._channel_id(thread_path)
        title = demojibake(raw.get("title")) or path.parent.name
        participants = [demojibake(p.get("name", "")) for p in raw.get("participants", [])]
        thread_kind = "DM" if len(participants) <= 2 else "GROUP"

        self.stats.threads_seen += 1
        if not self.con.execute("SELECT 1 FROM channels WHERE id = ?", (channel_id,)).fetchone():
            self.stats.new_threads += 1

        avatar_sha = None
        image = raw.get("image")
        if isinstance(image, dict) and image.get("uri"):
            _, avatar_sha, _ = self._store_media(image["uri"])

        self.con.execute(
            """
            INSERT INTO servers (id, name, type, platform) VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET name = excluded.name, type = excluded.type
            """,
            (channel_id, title, thread_kind, self.platform),
        )
        self.con.execute(
            """
            INSERT INTO channels
                (id, server, name, parent_id, position, topic, nsfw, platform, avatar_sha256)
            VALUES (?, ?, ?, NULL, NULL, ?, 0, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name          = excluded.name,
                topic         = excluded.topic,
                avatar_sha256 = COALESCE(excluded.avatar_sha256, channels.avatar_sha256)
            """,
            (channel_id, channel_id, title, thread_path, self.platform, avatar_sha),
        )

        for name in participants:
            self._upsert_user(name)
            self.con.execute(
                "INSERT OR IGNORE INTO channel_participants (channel_id, user_id) VALUES (?, ?)",
                (channel_id, self._user_id(name)),
            )

        for message in raw.get("messages", []):
            self._ingest_message(channel_id, thread_path, message)

    def _upsert_user(self, name: str) -> int:
        """Meta exports carry display names only - there are no stable user ids."""
        user_id = self._user_id(name)
        self.con.execute(
            """
            INSERT INTO users (id, name, display_name, avatar_url, discriminator, platform)
            VALUES (?, ?, ?, NULL, NULL, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (user_id, name, name, self.platform),
        )
        return user_id

    def _ingest_message(self, channel_id: int, thread_path: str, message: dict) -> None:
        self.stats.msgs_seen += 1
        text = demojibake(message.get("content", "")) or ""
        # "Reacted 😂 to your message" is Instagram narrating a reaction it also
        # attached to the message it belongs to. Drop it, but never when it
        # carries anything of its own.
        if (
            noise.is_reaction_notice(text)
            and not message.get("share")
            and not any(True for _ in media_entries(message))
        ):
            self.stats.skipped_notices += 1
            return
        source_key = message_source_key(self.platform, thread_path, message)
        message_id = synth_id(source_key)

        sender = demojibake(message.get("sender_name", "")) or "(unknown)"
        sender_id = self._upsert_user(sender)
        # Group threads occasionally contain a sender absent from participants.
        self.con.execute(
            "INSERT OR IGNORE INTO channel_participants (channel_id, user_id) VALUES (?, ?)",
            (channel_id, sender_id),
        )

        cursor = self.con.execute(
            """
            INSERT OR IGNORE INTO messages
                (message_id, sender_id, channel_id, text, timestamp, platform,
                 source_key, is_unsent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                sender_id,
                channel_id,
                text,
                int(message["timestamp_ms"]),
                self.platform,
                source_key,
                1 if message.get("is_unsent") else 0,
            ),
        )
        if cursor.rowcount:
            self.stats.new_msgs += 1
        else:
            self.stats.dup_msgs += 1
            existing = self.con.execute(
                "SELECT source_key FROM messages WHERE message_id = ?", (message_id,)
            ).fetchone()
            if existing and existing["source_key"] != source_key:
                raise RuntimeError(
                    f"synthetic id collision on {message_id}: "
                    f"{existing['source_key']!r} vs {source_key!r}"
                )

        for _kind, entry in media_entries(message):
            attachment_id, _sha, _rel = self._store_media(entry["uri"])
            self.con.execute(
                "INSERT OR IGNORE INTO message_attachments (message_id, attachment_id) VALUES (?, ?)",
                (message_id, attachment_id),
            )

        self._write_reactions(message_id, message.get("reactions") or [])
        self._write_share(message_id, message.get("share"))

    def _write_reactions(self, message_id: int, reactions: list[dict]) -> None:
        """Store both the DHT-style aggregate and Meta's per-actor detail."""
        if not reactions:
            return
        # Scoped to a single message, and Meta message ids are always negative,
        # so this can never disturb mirrored Discord rows.
        self.con.execute("DELETE FROM message_reactions WHERE message_id = ?", (message_id,))
        counts: dict[str, int] = {}
        for reaction in reactions:
            emoji = demojibake(reaction.get("reaction", "")) or "?"
            actor = demojibake(reaction.get("actor", "")) or "(unknown)"
            counts[emoji] = counts.get(emoji, 0) + 1
            self.con.execute(
                """
                INSERT OR IGNORE INTO message_reaction_actors (message_id, user_id, emoji_name)
                VALUES (?, ?, ?)
                """,
                (message_id, self._upsert_user(actor), emoji),
            )
        self.con.executemany(
            """
            INSERT INTO message_reactions (message_id, emoji_id, emoji_name, emoji_flags, count)
            VALUES (?, NULL, ?, 0, ?)
            """,
            [(message_id, emoji, count) for emoji, count in counts.items()],
        )

    def _write_share(self, message_id: int, share: dict | None) -> None:
        """Shared links reuse the existing message_embeds table."""
        if not isinstance(share, dict) or not share:
            return
        payload = {"type": "share"}
        payload.update(
            {key: demojibake(value) if isinstance(value, str) else value
             for key, value in share.items()}
        )
        self.con.execute("DELETE FROM message_embeds WHERE message_id = ?", (message_id,))
        self.con.execute(
            "INSERT INTO message_embeds (message_id, json) VALUES (?, ?)",
            (message_id, json.dumps(payload, ensure_ascii=False)),
        )

    # -- entry point -----------------------------------------------------
    def run(self) -> Stats:
        total = len(self.source.thread_files)
        for index, path in enumerate(self.source.thread_files, 1):
            self.progress(f"[{self.platform}] thread {index}/{total}: {path.parent.name}")
            self.ingest_thread(path)
        return self.stats

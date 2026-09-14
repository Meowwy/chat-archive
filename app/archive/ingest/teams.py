"""Microsoft Teams export -> the unified archive tables.

A fourth platform, and the first with a stable id on every message, which
changes two things for the better.

**Deduplication is exact.** Meta exports carry no ids, so `ingest/meta.py` has
to derive one by hashing the message's own content. Teams numbers every message
and the number is its arrival time in milliseconds, so it is the same in every
export you will ever request:

    source_key = teams | <conversation id> | <message id>

Re-importing an overlapping export is therefore a true no-op - not "no-op unless
a media filename changed", which is the caveat the encrypted-Messenger download
carries.

**Senders are accounts, not names.** A `users` row is keyed by the Skype account
id (`8:live:marketa.slachtova_1`), so two people who happen to share a display
name stay two identities - the caveat that applies to every Meta import does not
apply here. Getting there takes some work, because Teams attributes a message
three different ways depending on which client sent it and whether Skype
migrated it - and, on some recent group messages, not at all.
`teams_content.identity_hints` explains where the missing names are found.

The account that requested the export is known for certain - `messages.json`
names it - so it is linked to whoever is already marked as you on the People
page, and your own messages align right from the first import.

Media lives beside the JSON as flat, hash-named objects; `teams_export` finds
them, inside the tar if that is what it was handed, and the bytes stream from
there into the vault without the export ever being unpacked.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

from ..ids import media_type, synth_id
from ..vault import Vault
from . import teams_content as content
from .meta import Stats
from .teams_export import TeamsExport

Progress = Callable[[str], None]

PLATFORM = "teams"

# What a message from an account nothing in the export ever names is filed
# under. Teams drops the sender outright on some recent group messages - there
# is no id, no name and no way to work out who it was - and the alternative to
# one shared identity is discarding the messages.
UNKNOWN_KEY = "(unknown)"
UNKNOWN_NAME = "Unknown (Teams)"

# threadProperties.picture: "URL@https://api.asm.skype.com/v1/objects/<doc id>"
_AVATAR_DOC = re.compile(r"/objects/([^/?\s]+)")


def _timestamp_ms(message: dict) -> int:
    """When the message arrived, in epoch milliseconds.

    `originalarrivaltime` is the readable answer and the message id is the same
    instant as a number, so the id stands in when the timestamp is unparseable.
    """
    raw = message.get("originalarrivaltime")
    if isinstance(raw, str) and raw:
        try:
            moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return int(moment.timestamp() * 1000)
        except ValueError:
            pass
    identifier = str(message.get("id") or "")
    return int(identifier) if identifier.isdigit() else 0


def importable(message: dict) -> bool:
    """True for a message that is conversation rather than a system event."""
    kind = message.get("messagetype") or ""
    if kind in content.SKIPPED_TYPES or kind.startswith(content.SKIPPED_TYPES_PREFIXES):
        return False
    body = message.get("content")
    return not (isinstance(body, str) and content.CALL_LOG.match(body))


@dataclass
class Decoded:
    """A message reduced to what would actually be drawn for it."""

    message: dict
    text: str
    attachments: list[content.Attachment]
    embeds: list[dict]
    deleted: bool


class Directory:
    """Who the accounts in an export are, and what to call each of them.

    Built by reading every message once before any of them is imported, because
    a name for an account often appears only in a message that account did not
    send - an @mention, a quoted reply, the roster Teams staples onto both.
    Where an account is named more than one way the commonest wins, so a person
    is filed under what they are usually called rather than what the last
    message to mention them happened to say.
    """

    def __init__(self, conversations: Iterable[dict], self_mri: str | None):
        votes: dict[str, Counter] = defaultdict(Counter)
        for conversation in conversations:
            for message in conversation.get("MessageList") or []:
                for mri, name in content.identity_hints(message):
                    votes[mri][name] += 1

        self.self_mri = self_mri
        self.names = {mri: counted.most_common(1)[0][0] for mri, counted in votes.items()}
        self._disambiguate()

        # Reverse lookup, for the places Teams gives a name and no account: the
        # member list of a thread, and the actor on a reaction.
        by_name: dict[str, Counter] = defaultdict(Counter)
        for mri, counted in votes.items():
            for name, count in counted.items():
                by_name[name][mri] += count
        self.mris = {name: counted.most_common(1)[0][0] for name, counted in by_name.items()}

    def _disambiguate(self) -> None:
        """Two accounts can genuinely share a name - a parent and a child, say.

        They stay two identities because they are keyed by account, but showing
        both as "Pavel Kolečkář" makes the People page unreadable, so the
        account handle is appended to each of them.
        """
        clashes = {
            name for name, count in Counter(self.names.values()).items() if count > 1
        }
        for mri, name in list(self.names.items()):
            if name in clashes:
                self.names[mri] = f"{name} ({content.handle_of(mri)})"

    def name_of(self, mri: str) -> str:
        return self.names.get(mri) or content.handle_of(mri)

    def sender(self, message: dict) -> tuple[str, str]:
        """(identity key, display name) of whoever sent this message."""
        mri = content.mri_of(message)
        if mri:
            return mri, self.name_of(mri)

        name = message.get("displayName")
        if isinstance(name, str) and name and name not in content.PLACEHOLDER_NAMES:
            resolved = self.mris.get(name)
            return (resolved, self.name_of(resolved)) if resolved else (name, name)

        return UNKNOWN_KEY, UNKNOWN_NAME

    def by_name(self, name: str | None) -> tuple[str, str] | None:
        """An identity for a bare display name, as members and reactions give it."""
        name = (name or "").strip()
        if not name:
            return None
        if name == "Export Owner" and self.self_mri:
            return self.self_mri, self.name_of(self.self_mri)
        if name in content.PLACEHOLDER_NAMES:
            # "Unknown User" stands for a different person every time it is
            # written; one identity for all of them would be a fiction.
            return None
        resolved = self.mris.get(name)
        return (resolved, self.name_of(resolved)) if resolved else (name, name)


class TeamsIngest:
    """One Teams export, read into the archive's tables and media vault."""

    def __init__(
        self,
        con: sqlite3.Connection,
        source,
        vault: Vault,
        progress: Progress | None = None,
    ):
        self.con = con
        self.source = source
        self.export: TeamsExport = source.export
        self.vault = vault
        self.progress = progress or (lambda _message: None)
        self.stats = Stats()
        self.directory: Directory | None = None
        self.self_mri: str | None = None

    # -- ids -------------------------------------------------------------
    def _channel_id(self, conversation_id: str) -> int:
        return synth_id(PLATFORM, conversation_id)

    def _user_id(self, key: str) -> int:
        return synth_id(PLATFORM, "user", key)

    def _message_id(self, conversation_id: str, message_id: str) -> tuple[int, str]:
        source_key = "\x1f".join([PLATFORM, conversation_id, str(message_id)])
        return synth_id(source_key), source_key

    # -- media -----------------------------------------------------------
    def _store_media(self, attachment: content.Attachment, fallback_key: str) -> int:
        """Copy one object into the vault and upsert its attachment row."""
        self.stats.media_seen += 1
        key = attachment.doc_id or attachment.url or fallback_key
        attachment_id = synth_id(PLATFORM, "attachment", key)

        entry = self.export.media().get(attachment.doc_id) if attachment.doc_id else None
        name = attachment.name or (
            f"{attachment.doc_id}{entry.suffix}" if entry else attachment.doc_id or "file"
        )

        row = self.con.execute(
            "SELECT sha256, local_path FROM attachments WHERE attachment_id = ?",
            (attachment_id,),
        ).fetchone()
        if row and row["sha256"] and row["local_path"] and self.vault.exists(row["local_path"]):
            self.stats.dup_media += 1
            return attachment_id

        sha256 = relpath = None
        size = attachment.size
        if entry is not None:
            stream = self.export.open_media(attachment.doc_id)
            if stream is not None:
                with stream:
                    stored = self.vault.put_stream(stream, entry.suffix)
                sha256, relpath, size = stored.sha256, stored.relpath, stored.size
                if stored.is_new:
                    self.stats.new_media += 1
                else:
                    self.stats.dup_media += 1
        if sha256 is None:
            # Teams leaves a good deal of what its messages point at out of the
            # download - every voice message and every non-media file in this
            # author's export. The row is kept so the viewer can say a file was
            # here and is not any more.
            self.stats.missing_media += 1
            if len(self.stats.missing_examples) < 5:
                self.stats.missing_examples.append(name)

        self.con.execute(
            """
            INSERT INTO attachments
                (attachment_id, name, type, normalized_url, download_url,
                 size, width, height, platform, local_path, sha256)
            VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(attachment_id) DO UPDATE SET
                local_path = excluded.local_path,
                sha256     = excluded.sha256,
                size       = excluded.size,
                type       = excluded.type
            """,
            (
                attachment_id,
                name,
                media_type(name),
                attachment.url or key,
                size,
                attachment.width,
                attachment.height,
                PLATFORM,
                relpath,
                sha256,
            ),
        )
        return attachment_id

    def _store_avatar(self, picture: object) -> str | None:
        """The conversation's picture, which is an ordinary stored object."""
        if not isinstance(picture, str) or not picture:
            return None
        match = _AVATAR_DOC.search(picture)
        if match is None:
            return None
        doc_id = match.group(1)
        entry = self.export.media().get(doc_id)
        if entry is None:
            return None
        stream = self.export.open_media(doc_id)
        if stream is None:
            return None
        with stream:
            stored = self.vault.put_stream(stream, entry.suffix)
        if stored.is_new:
            self.stats.new_media += 1
        else:
            self.stats.dup_media += 1
        self.stats.media_seen += 1
        return stored.sha256

    # -- users -----------------------------------------------------------
    def _upsert_user(self, key: str, name: str) -> int:
        user_id = self._user_id(key)
        self.con.execute(
            """
            INSERT INTO users (id, name, display_name, avatar_url, discriminator, platform)
            VALUES (?, ?, ?, NULL, NULL, ?)
            ON CONFLICT(id) DO UPDATE SET
                name         = excluded.name,
                display_name = excluded.display_name
            """,
            (user_id, name, name, PLATFORM),
        )
        return user_id

    def _link_self(self) -> None:
        """Point the exporting account at whoever is already marked as you.

        Meta exports cannot do this - there is nothing in them that says which
        participant you are. A Teams export names the account that asked for it,
        so the link is a fact rather than a guess, and making it here is what
        stops your own messages showing up as somebody else's on first import.
        """
        if not self.self_mri:
            return
        person = self.con.execute("SELECT person_id FROM people WHERE is_self = 1").fetchone()
        if person is None:
            return
        user_id = self._user_id(self.self_mri)
        self.con.execute(
            "UPDATE users SET person_id = ? WHERE id = ? AND person_id IS NULL",
            (person["person_id"], user_id),
        )

    # -- conversations ---------------------------------------------------
    def _title(self, conversation: dict, participants: list[tuple[str, str]]) -> str:
        title = (conversation.get("displayName") or "").strip()
        if title:
            return title
        others = [name for key, name in participants if key != self.self_mri]
        return ", ".join(others) if others else conversation["id"]

    def ingest_conversation(self, conversation: dict) -> None:
        conversation_id = conversation.get("id")
        if not conversation_id:
            return
        assert self.directory is not None

        # Decode first, write second. Teams ships an entry for every thread the
        # account has ever been attached to - empty ones, and the streams its
        # own features keep their bookkeeping in - and whether a thread holds
        # any conversation is only known once its messages have been reduced to
        # what would be drawn for them. Deciding before anything is written is
        # what stops a blank thread appearing in the viewer.
        decoded: list[Decoded] = []
        for message in conversation.get("MessageList") or []:
            if not importable(message):
                continue
            self.stats.msgs_seen += 1
            found = self._decode(message)
            if found is None:
                self.stats.skipped_notices += 1
            else:
                decoded.append(found)
        if not decoded:
            return

        messages = [found.message for found in decoded]
        properties = conversation.get("threadProperties") or {}

        # Who is in the thread has to be settled before the channel row is
        # written, because it decides whether this is a DM or a group. The
        # member list alone will not do it: Teams writes "Unknown User" for
        # anyone it could not resolve, and those are dropped rather than fused
        # into one imaginary person, which would leave a five-person group
        # looking like a DM. The senders fill the gap.
        participants: list[tuple[str, str]] = []
        seen: set[str] = set()
        for member in content.as_list(properties.get("members")):
            resolved = self.directory.by_name(member if isinstance(member, str) else None)
            if resolved and resolved[0] not in seen:
                seen.add(resolved[0])
                participants.append(resolved)
        for message in messages:
            sender = self.directory.sender(message)
            if sender[0] not in seen:
                seen.add(sender[0])
                participants.append(sender)

        channel_id = self._channel_id(conversation_id)
        title = self._title(conversation, participants)
        members = properties.get("membercount")
        size = max(len(participants), members if isinstance(members, int) else 0)
        kind = "DM" if size <= 2 else "GROUP"

        self.stats.threads_seen += 1
        if not self.con.execute("SELECT 1 FROM channels WHERE id = ?", (channel_id,)).fetchone():
            self.stats.new_threads += 1

        avatar_sha = self._store_avatar(properties.get("picture"))

        self.con.execute(
            """
            INSERT INTO servers (id, name, type, platform) VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET name = excluded.name, type = excluded.type
            """,
            (channel_id, title, kind, PLATFORM),
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
            (
                channel_id,
                channel_id,
                title,
                properties.get("topic") or conversation_id,
                PLATFORM,
                avatar_sha,
            ),
        )

        for key, name in participants:
            self._join(channel_id, key, name)

        for found in decoded:
            self._ingest_message(channel_id, conversation_id, found)

    def _join(self, channel_id: int, key: str, name: str) -> int:
        user_id = self._upsert_user(key, name)
        self.con.execute(
            "INSERT OR IGNORE INTO channel_participants (channel_id, user_id) VALUES (?, ?)",
            (channel_id, user_id),
        )
        return user_id

    # -- messages --------------------------------------------------------
    def _decode(self, message: dict) -> Decoded | None:
        """Reduce a message to what would be drawn, or None if that is nothing.

        Nothing is the ordinary outcome for an album header, whose photos are
        messages of their own, and for a sticker Teams no longer serves.
        """
        properties = message.get("properties") or {}
        deleted = bool(properties.get("deletetime") or properties.get("hardDeleteTime"))
        if deleted:
            # The placeholder is the content; whatever was said is gone.
            return Decoded(message, "", [], [], True)

        text = content.text(message)
        attachments = content.attachments(message)
        embeds = self._embeds(message)
        if not text and not attachments and not embeds:
            return None
        return Decoded(message, text, attachments, embeds, False)

    def _ingest_message(self, channel_id: int, conversation_id: str, found: Decoded) -> None:
        assert self.directory is not None
        message = found.message
        properties = message.get("properties") or {}
        message_id, source_key = self._message_id(conversation_id, message.get("id"))
        sender_key, sender_name = self.directory.sender(message)
        sender_id = self._join(channel_id, sender_key, sender_name)

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
                found.text,
                _timestamp_ms(message),
                PLATFORM,
                source_key,
                1 if found.deleted else 0,
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

        for index, attachment in enumerate(found.attachments):
            attachment_id = self._store_media(attachment, f"{source_key}#{index}")
            self.con.execute(
                "INSERT OR IGNORE INTO message_attachments (message_id, attachment_id) VALUES (?, ?)",
                (message_id, attachment_id),
            )

        self._write_embeds(message_id, found.embeds)
        self._write_reactions(message_id, message)
        self._write_reply(message_id, conversation_id, message)
        self._write_edit(message_id, properties)

    def _embeds(self, message: dict) -> list[dict]:
        embeds = content.link_embeds(message)
        card = content.card_embed(message)
        if card:
            embeds.append(card)
        return embeds

    def _write_embeds(self, message_id: int, embeds: list[dict]) -> None:
        if not embeds:
            return
        self.con.execute("DELETE FROM message_embeds WHERE message_id = ?", (message_id,))
        self.con.executemany(
            "INSERT INTO message_embeds (message_id, json) VALUES (?, ?)",
            [(message_id, json.dumps(embed, ensure_ascii=False)) for embed in embeds],
        )

    def _write_reactions(self, message_id: int, message: dict) -> None:
        assert self.directory is not None
        reactions = content.reactions(message)
        if not reactions:
            return
        self.con.execute("DELETE FROM message_reactions WHERE message_id = ?", (message_id,))
        counts: Counter = Counter()
        for emoji, actor in reactions:
            counts[emoji] += 1
            resolved = self.directory.by_name(actor)
            if resolved is None:
                continue
            self.con.execute(
                """
                INSERT OR IGNORE INTO message_reaction_actors (message_id, user_id, emoji_name)
                VALUES (?, ?, ?)
                """,
                (message_id, self._upsert_user(*resolved), emoji),
            )
        self.con.executemany(
            """
            INSERT INTO message_reactions (message_id, emoji_id, emoji_name, emoji_flags, count)
            VALUES (?, NULL, ?, 0, ?)
            """,
            [(message_id, emoji, count) for emoji, count in counts.items()],
        )

    def _write_reply(self, message_id: int, conversation_id: str, message: dict) -> None:
        target = content.reply_to(message)
        if not target:
            return
        replied_to, _ = self._message_id(conversation_id, target)
        self.con.execute(
            "INSERT OR REPLACE INTO message_replied_to (message_id, replied_to_id) VALUES (?, ?)",
            (message_id, replied_to),
        )

    def _write_edit(self, message_id: int, properties: dict) -> None:
        edited = properties.get("edittime")
        if not edited:
            return
        try:
            stamp = int(edited)
        except (TypeError, ValueError):
            return
        self.con.execute(
            "INSERT OR REPLACE INTO message_edit_timestamps (message_id, edit_timestamp) VALUES (?, ?)",
            (message_id, stamp),
        )

    # -- entry point -----------------------------------------------------
    def run(self) -> Stats:
        index = self.export.read_index()
        conversations = index.get("conversations") or []
        self.self_mri = content.normalise_mri(index.get("userId"))
        self.directory = Directory(conversations, self.self_mri)

        self.progress(
            f"[teams] {len(conversations)} conversations, "
            f"{len(self.export.media())} stored media objects"
        )

        for position, conversation in enumerate(conversations, 1):
            name = (conversation.get("displayName") or conversation.get("id") or "?")[:60]
            self.progress(f"[teams] conversation {position}/{len(conversations)}: {name}")
            self.ingest_conversation(conversation)

        self._drop_dangling_replies()
        self._link_self()
        return self.stats

    def _drop_dangling_replies(self) -> None:
        """Forget replies whose parent is not in the archive.

        A reply can point at a call, a poll or something else this importer
        leaves out, and Teams lists a conversation newest message first - so
        when the reply is written its parent has not been reached yet and
        cannot be checked. Sweeping once at the end is what keeps the viewer
        from drawing a reply to nothing.
        """
        self.con.execute(
            """
            DELETE FROM message_replied_to
            WHERE message_id IN (
                SELECT r.message_id FROM message_replied_to r
                JOIN messages m ON m.message_id = r.message_id
                LEFT JOIN messages parent ON parent.message_id = r.replied_to_id
                WHERE m.platform = ? AND parent.message_id IS NULL
            )
            """,
            (PLATFORM,),
        )

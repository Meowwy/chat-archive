"""Messenger "secure storage" export -> the shape the Meta importer already reads.

End-to-end encrypted chats are absent from Facebook's Download Your Information
archive - Meta cannot read them, so it cannot include them. They come instead
from Messenger's own download (Privacy & safety > End-to-end encrypted chats >
Message storage), which unzips to a flat `messages/` folder of one JSON per
conversation beside a shared `media/` folder:

    <root>/messages/Jane Doe_3.json
    <root>/media/ff8bfa0f-....jpeg          # referenced as "./media/ff8bfa0f-....jpeg"

The payload says the same things as a DYI export in different words, and in
clean UTF-8 rather than Meta's latin-1 mojibake:

    participants  ["Me", "Jane Doe"]        vs  [{"name": "Jane Doe"}, ...]
    threadName    "Jane Doe_3"              vs  title / thread_path
    senderName    timestamp    text         vs  sender_name  timestamp_ms  content
    isUnsent      media[{uri}]              vs  is_unsent    photos/videos/files/...
    reactions     [{actor, reaction}]       vs  identical

So rather than a second importer, this module rewrites a thread into the DYI
shape and hands it to MetaIngest, which then does the rest unchanged: synthetic
ids, the vault, reactions, dedup by source_key.

These are Facebook Messenger conversations, so they are ingested as platform
"facebook". A counterpart already known from a group chat resolves to the very
same users row - `synth_id("facebook", "user", name)` - so identities and any
people links carry over rather than forking.

One caveat worth knowing: media URIs are random UUIDs Meta mints per download,
and they feed the message source_key. Re-ingesting *this* folder is a no-op, but
a freshly requested download would carry new UUIDs and so re-import media-only
messages as new rows. The vault still stores the bytes once.
"""

from __future__ import annotations

import re
from typing import Any

from ..ids import media_type

# Meta appends an index to keep filenames unique when two people share a name.
_INDEX_SUFFIX = re.compile(r"_\d+$")

# Which DYI media bucket a file belongs in. The importer only uses the bucket to
# find the URI, but keeping it honest means the source_key stays meaningful.
_BUCKETS = {"image": "photos", "video": "videos", "audio": "audio_files"}

# Every message carries a type; only these two need more than a straight copy.
UNSENT_TYPE = "placeholder"


def thread_title(thread_name: str | None, stem: str) -> str:
    """A display name for the conversation: the counterpart, minus Meta's index."""
    name = (thread_name or stem).strip()
    return _INDEX_SUFFIX.sub("", name) or stem


def _bucket(uri: str) -> str:
    """Which DYI media bucket a URI belongs in.

    Meta sometimes writes the literal string "Failed to download media" where a
    URI should be. It is kept rather than dropped - the message really did carry
    an attachment - and lands here as an unresolvable one, which the importer
    counts as missing media and the viewer shows as an unavailable file.
    """
    kind = (media_type(uri) or "").split("/")[0]
    return _BUCKETS.get(kind, "files")


def normalize_message(message: dict) -> dict | None:
    """Rewrite one message in DYI terms, or None if it carries no timestamp."""
    timestamp = message.get("timestamp")
    if timestamp is None:
        return None

    out: dict[str, Any] = {
        "sender_name": message.get("senderName", ""),
        "timestamp_ms": int(timestamp),
        "reactions": message.get("reactions") or [],
    }

    if message.get("isUnsent"):
        # "User unsent a message" is Messenger narrating the gap, not content.
        # DYI writes these with is_unsent and no content at all; match that, so
        # the viewer renders one placeholder and search stays clean.
        out["is_unsent"] = True
    else:
        out["content"] = message.get("text") or ""

    for entry in message.get("media") or []:
        uri = entry.get("uri") if isinstance(entry, dict) else None
        if uri:
            out.setdefault(_bucket(uri), []).append({"uri": uri})

    return out


def normalize_thread(raw: dict, stem: str) -> dict:
    """Rewrite a whole conversation file in DYI terms.

    `stem` is the filename without its suffix - unique per conversation, so it
    is what the thread_path (and through it the channel id) is built from.
    """
    messages = [normalize_message(m) for m in raw.get("messages", []) if isinstance(m, dict)]
    return {
        "title": thread_title(raw.get("threadName"), stem),
        "thread_path": f"secure/{stem}",
        "participants": [{"name": p} for p in raw.get("participants", []) if isinstance(p, str)],
        "messages": [m for m in messages if m is not None],
    }


def looks_like_thread(raw: Any) -> bool:
    """True for the JSON shape this module understands.

    Deliberately shape-based rather than name-based: a DYI export also has a
    folder called `messages`, full of settings JSON that must not match.
    """
    return (
        isinstance(raw, dict)
        and isinstance(raw.get("messages"), list)
        and isinstance(raw.get("participants"), list)
        and "threadName" in raw
    )

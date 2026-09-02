"""Identifier synthesis and Meta text repair.

Discord snowflakes are always positive, so every synthetic id we mint for
Facebook/Instagram rows is negative. Collision with Discord is therefore
structurally impossible rather than merely improbable.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

_SEP = "\x1f"


def synth_id(*parts: Any) -> int:
    """Deterministic negative 62-bit id derived from the given key material."""
    material = _SEP.join("" if p is None else str(p) for p in parts)
    digest = hashlib.blake2b(material.encode("utf-8"), digest_size=8).digest()
    return -((int.from_bytes(digest, "big") & ((1 << 62) - 1)) + 1)


def demojibake(value: str | None) -> str | None:
    """Repair Meta's latin-1/UTF-8 double-encoding.

    Facebook and Instagram write UTF-8 bytes escaped as latin-1 code points, so
    'Dobrá zpráva, můžeš' arrives as 'DobrÃ¡ zprÃ¡va, mÅ¯Å¾eÅ¡'. Round-tripping through
    latin-1 restores the original exactly. Strings that are not encodable as
    latin-1, or whose bytes are not valid UTF-8, are already correct and are
    returned untouched.
    """
    if not value:
        return value
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def fix_deep(obj: Any) -> Any:
    """Recursively demojibake every string in a decoded JSON structure."""
    if isinstance(obj, str):
        return demojibake(obj)
    if isinstance(obj, list):
        return [fix_deep(x) for x in obj]
    if isinstance(obj, dict):
        return {demojibake(k): fix_deep(v) for k, v in obj.items()}
    return obj


_MEDIA_KEYS = ("photos", "videos", "audio_files", "files", "gifs")


def message_source_key(platform: str, thread_path: str, message: dict) -> str:
    """Stable dedup key for a Meta message, which carries no id of its own.

    Verified unique across all 6,792 messages in the current exports. Built from
    the raw (still-mojibake) message so it stays stable regardless of future
    changes to the repair logic.
    """
    uris = [a.get("uri") for k in _MEDIA_KEYS for a in message.get(k, [])]
    sticker = message.get("sticker") or {}
    payload = json.dumps(
        {
            "c": message.get("content", ""),
            "u": uris,
            "k": sticker.get("uri"),
            "s": (message.get("share") or {}).get("link"),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    body = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return _SEP.join(
        [platform, thread_path, message.get("sender_name", ""), str(message["timestamp_ms"]), body]
    )


def media_entries(message: dict) -> Iterable[tuple[str, dict]]:
    """Yield (kind, entry) for every media attachment on a Meta message."""
    for key in _MEDIA_KEYS:
        for entry in message.get(key, []):
            if entry.get("uri"):
                yield key, entry
    sticker = message.get("sticker")
    if isinstance(sticker, dict) and sticker.get("uri"):
        yield "stickers", sticker

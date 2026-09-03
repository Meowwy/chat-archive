"""Filtering of the pseudo-messages Meta exports mix into a conversation.

Instagram writes a reaction into the thread *twice*: once as a `reactions`
entry on the message it belongs to, and once as a standalone message reading
"Reacted 😂 to your message" or "Liked a message". The viewer already shows
reactions under the message they belong to, so the standalone copies are pure
noise - they inflate every message count and clutter the thread.

They are recognised by shape, not by sender, and only ever dropped when they
carry nothing else: no media, no share, no text beyond the notice itself.
"""

from __future__ import annotations

import re
import sqlite3

# "Reacted 😂 to your message", "kgasparikova reacted 👍 to your message",
# "Liked a message", "alestopol liked a message". The optional leading word is
# the actor's handle; the emoji is one short run of non-space characters
# (emoji built from several codepoints, ZWJ and all, still count as one).
_NOTICE = re.compile(
    r"""^\s*
        (?:\S+\s+)?                                  # optional actor handle
        (?: reacted \s+ \S{1,8} \s+ to \s+ your \s+ message
          | liked \s+ a \s+ message )
        \s*$""",
    re.IGNORECASE | re.VERBOSE,
)

# Cheap prefilter so the purge does not have to run the regex over 141k rows.
_LIKE = "(text LIKE '%to your message%' OR text LIKE '%liked a message%')"


def is_reaction_notice(text: str | None) -> bool:
    """True for a 'Reacted 👍 to your message' style pseudo-message."""
    return bool(text) and bool(_NOTICE.match(text))


def purge_reaction_notices(con: sqlite3.Connection, *, verbose: bool = True) -> int:
    """Delete reaction notices already sitting in the archive.

    Only messages that carry nothing but the notice go; anything with an
    attachment, an embed or a reaction of its own is left alone, on the
    principle that a false positive must never cost real content.
    """
    doomed = [
        row["message_id"]
        for row in con.execute(f"SELECT message_id, text FROM messages WHERE {_LIKE}")
        if is_reaction_notice(row["text"])
    ]
    if not doomed:
        return 0

    kept = 0
    removed = 0
    for message_id in doomed:
        carries = con.execute(
            """
            SELECT EXISTS(SELECT 1 FROM message_attachments WHERE message_id = ?)
                OR EXISTS(SELECT 1 FROM message_embeds      WHERE message_id = ?)
                OR EXISTS(SELECT 1 FROM message_reactions   WHERE message_id = ?)
            """,
            (message_id, message_id, message_id),
        ).fetchone()[0]
        if carries:
            kept += 1
            continue
        con.execute("DELETE FROM message_reaction_actors WHERE message_id = ?", (message_id,))
        con.execute("DELETE FROM message_replied_to WHERE message_id = ?", (message_id,))
        con.execute("DELETE FROM messages WHERE message_id = ?", (message_id,))
        removed += 1
    con.commit()
    if verbose:
        print(f"[clean] reaction notices removed: {removed:,}", end="")
        print(f" ({kept:,} kept - they carry content)" if kept else "")
    return removed

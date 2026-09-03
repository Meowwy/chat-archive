"""Cross-platform identity mapping.

Discord gives stable numeric user ids; Meta exports give only display names. A
`people` row ties the two together so the viewer can answer "everything this
person ever sent me", across all three platforms.

The `people` table is the whole story: the People page edits it and
`users.person_id` directly, so the mapping travels inside the archive file and
there is nothing on the side to keep in step with it.

The name a person is given here overrides the per-platform names everywhere in
the UI - that is the point of the "custom name" field.
"""

from __future__ import annotations

import sqlite3


def identities(con: sqlite3.Connection) -> list[dict]:
    """Every distinct identity in the archive, with its message count."""
    rows = con.execute(
        """
        SELECT u.id, u.platform, u.name, u.display_name, u.person_id,
               p.display AS person, p.is_self,
               COUNT(m.message_id) AS messages
        FROM users u
        LEFT JOIN people p   ON p.person_id = u.person_id
        LEFT JOIN messages m ON m.sender_id = u.id
        GROUP BY u.id
        ORDER BY messages DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------- editing
# Everything below is what the People page drives. Each mutation commits before
# it returns, so a reload always shows what the archive actually holds.


class PeopleError(ValueError):
    """A rejected edit - the message is meant to be shown to the user."""


def _clean_display(display: str | None) -> str:
    display = (display or "").strip()
    if not display:
        raise PeopleError("The name cannot be empty.")
    return display


def _set_self(con: sqlite3.Connection, person_id: int) -> None:
    """Only one person can be 'me' - the UI aligns their messages right."""
    con.execute("UPDATE people SET is_self = 0 WHERE person_id <> ?", (person_id,))
    con.execute("UPDATE people SET is_self = 1 WHERE person_id = ?", (person_id,))


def create(
    con: sqlite3.Connection,
    display: str,
    is_self: bool = False,
    notes: str | None = None,
    user_ids: list[int] | tuple[int, ...] = (),
) -> int:
    display = _clean_display(display)
    if con.execute("SELECT 1 FROM people WHERE display = ?", (display,)).fetchone():
        raise PeopleError(f"Somebody is already called “{display}”.")
    person_id = con.execute(
        "INSERT INTO people (display, is_self, notes) VALUES (?, ?, ?)",
        (display, 1 if is_self else 0, notes or None),
    ).lastrowid
    if is_self:
        _set_self(con, person_id)
    _assign(con, person_id, user_ids)
    con.commit()
    return person_id


def update(
    con: sqlite3.Connection,
    person_id: int,
    display: str | None = None,
    is_self: bool | None = None,
    notes: str | None = None,
) -> None:
    if not con.execute("SELECT 1 FROM people WHERE person_id = ?", (person_id,)).fetchone():
        raise PeopleError("No such person.")
    if display is not None:
        display = _clean_display(display)
        clash = con.execute(
            "SELECT person_id FROM people WHERE display = ? AND person_id <> ?",
            (display, person_id),
        ).fetchone()
        if clash:
            raise PeopleError(f"Somebody is already called “{display}”.")
        con.execute("UPDATE people SET display = ? WHERE person_id = ?", (display, person_id))
    if notes is not None:
        con.execute(
            "UPDATE people SET notes = ? WHERE person_id = ?", (notes.strip() or None, person_id)
        )
    if is_self is not None:
        if is_self:
            _set_self(con, person_id)
        else:
            con.execute("UPDATE people SET is_self = 0 WHERE person_id = ?", (person_id,))
    con.commit()


def delete(con: sqlite3.Connection, person_id: int) -> int:
    """Remove a person; their identities revert to their platform names."""
    unlinked = con.execute(
        "UPDATE users SET person_id = NULL WHERE person_id = ?", (person_id,)
    ).rowcount
    con.execute("DELETE FROM people WHERE person_id = ?", (person_id,))
    con.commit()
    return unlinked


def _assign(
    con: sqlite3.Connection, person_id: int | None, user_ids: list[int] | tuple[int, ...]
) -> int:
    changed = 0
    for user_id in user_ids:
        changed += con.execute(
            "UPDATE users SET person_id = ? WHERE id = ?", (person_id, user_id)
        ).rowcount
    return changed


def link(
    con: sqlite3.Connection, person_id: int | None, user_ids: list[int] | tuple[int, ...]
) -> int:
    """Attach identities to a person, or detach them when person_id is None."""
    if person_id is not None and not con.execute(
        "SELECT 1 FROM people WHERE person_id = ?", (person_id,)
    ).fetchone():
        raise PeopleError("No such person.")
    changed = _assign(con, person_id, user_ids)
    con.commit()
    return changed

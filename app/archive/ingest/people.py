"""Cross-platform identity mapping.

Discord gives stable numeric user ids; Meta exports give only display names. A
`people` row ties the two together so the viewer can answer "everything this
person ever sent me", across all three platforms.

The database is the source of truth: the People page edits `people` and
`users.person_id` directly. The YAML file is a human-readable mirror of that,
rewritten after every change so it stays hand-editable and `people apply` keeps
working in both directions. Ingest never touches it.

The name a person is given here overrides the per-platform names everywhere in
the UI - that is the point of the "custom name" field.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import yaml

from .. import config


def load(path: Path | None = None) -> dict:
    path = Path(path or config.PEOPLE_YAML)
    if not path.is_file():
        return {"people": []}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {"people": []}


_HEADER = """# Cross-platform identity mapping - a mirror of the `people` table.
#
# This file is rewritten every time the People page changes something, so edit it
# only when the app is idle, then press "Load the file and link" (or run
# `py -m archive people apply`) to read it back in. Comments below this header
# are not preserved.
#
# - `display`  the custom name shown everywhere in the app (must be unique)
# - `is_self`  exactly one person - their messages align right
# - `aliases`  per-platform names, matched against the login name OR the
#              display name recorded in the archive
#
# Identities you never list keep working - they simply stay unlinked and show
# under their original per-platform name.

"""


def save(data: dict, path: Path | None = None) -> Path:
    path = Path(path or config.PEOPLE_YAML)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _HEADER + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    return path


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


def scaffold(con: sqlite3.Connection, path: Path | None = None) -> Path:
    """Write a starter mapping listing every unmapped identity."""
    path = Path(path or config.PEOPLE_YAML)
    if path.is_file():
        return path

    entries = []
    for row in identities(con):
        entries.append(
            {
                "display": row["display_name"] or row["name"],
                "is_self": False,
                "aliases": {row["platform"]: [row["name"]]},
                "_messages": row["messages"],
            }
        )
    save({"people": entries}, path)
    return path


def apply(con: sqlite3.Connection, path: Path | None = None) -> tuple[int, int]:
    """Upsert people and stamp users.person_id. Returns (people, users linked)."""
    data = load(path)
    linked = 0
    people = 0

    for entry in data.get("people") or []:
        display = (entry.get("display") or "").strip()
        if not display:
            continue
        people += 1
        con.execute(
            """
            INSERT INTO people (display, is_self, notes) VALUES (?, ?, ?)
            ON CONFLICT(display) DO UPDATE SET
                is_self = excluded.is_self, notes = excluded.notes
            """,
            (display, 1 if entry.get("is_self") else 0, entry.get("notes")),
        )
        person_id = con.execute(
            "SELECT person_id FROM people WHERE display = ?", (display,)
        ).fetchone()[0]

        for platform, names in (entry.get("aliases") or {}).items():
            for name in names or []:
                # Discord identities are matched on the login name, Meta ones on
                # the display name that is all the export gives us.
                linked += con.execute(
                    """
                    UPDATE users SET person_id = ?
                    WHERE platform = ? AND (name = ? OR display_name = ?)
                    """,
                    (person_id, platform, name, name),
                ).rowcount
    con.commit()
    return people, linked


def unmapped(con: sqlite3.Connection) -> list[dict]:
    return [row for row in identities(con) if row["person_id"] is None]


# --------------------------------------------------------------- editing
# Everything below is what the People page drives. Each mutation commits and then
# rewrites the YAML mirror, so the file and the database never drift apart.


class PeopleError(ValueError):
    """A rejected edit - the message is meant to be shown to the user."""


def export(con: sqlite3.Connection, path: Path | None = None) -> Path:
    """Rewrite the YAML mirror from the database."""
    entries = []
    for person in con.execute(
        "SELECT person_id, display, is_self, notes FROM people ORDER BY display"
    ):
        aliases: dict[str, list[str]] = {}
        for row in con.execute(
            "SELECT platform, name, display_name FROM users WHERE person_id = ? "
            "ORDER BY platform, name",
            (person["person_id"],),
        ):
            names = aliases.setdefault(row["platform"], [])
            # Write both spellings: `apply` matches either, and Discord's login
            # name and display name are often different.
            for candidate in (row["name"], row["display_name"]):
                if candidate and candidate not in names:
                    names.append(candidate)
        entry: dict = {"display": person["display"]}
        if person["is_self"]:
            entry["is_self"] = True
        if person["notes"]:
            entry["notes"] = person["notes"]
        entry["aliases"] = aliases
        entries.append(entry)
    return save({"people": entries}, path)


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
    export(con)
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
    export(con)


def delete(con: sqlite3.Connection, person_id: int) -> int:
    """Remove a person; their identities revert to their platform names."""
    unlinked = con.execute(
        "UPDATE users SET person_id = NULL WHERE person_id = ?", (person_id,)
    ).rowcount
    con.execute("DELETE FROM people WHERE person_id = ?", (person_id,))
    con.commit()
    export(con)
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
    export(con)
    return changed

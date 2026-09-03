"""FastAPI backend for the local archive viewer.

Query endpoints hold a read-only connection so browsing can never modify the
archive; only the ingest endpoints open it read-write.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config, czech, db, migrate as migrate_mod, picker, query as query_mod, vault
from .ingest.detect import detect as detect_exports
from .ingest import people as people_mod
from .ingest import runner

# Discord snowflakes and our synthetic Meta ids are 62-63 bit integers, but
# JavaScript numbers lose precision above 2^53 - JSON.parse would silently
# round -1214588567190329077 to -1214588567190329000. Every id therefore
# crosses the wire as a string; the browser treats them as opaque, and Python
# parses them back exactly (its ints are arbitrary precision).
_ID_FIELDS = frozenset(
    {
        "id",
        "message_id",
        "channel_id",
        "sender_id",
        "attachment_id",
        "replied_to_id",
        "user_id",
        "person_id",
        "server",
    }
)


def _ids_to_str(value: Any) -> Any:
    if isinstance(value, list):
        return [_ids_to_str(item) for item in value]
    if isinstance(value, dict):
        return {
            key: str(item) if key in _ID_FIELDS and isinstance(item, int) else _ids_to_str(item)
            for key, item in value.items()
        }
    return value


class IdSafeJSONResponse(JSONResponse):
    """JSON responses with 64-bit ids rendered as strings."""

    def render(self, content: Any) -> bytes:
        return json.dumps(
            _ids_to_str(content), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")


app = FastAPI(
    title="Chat Archive",
    docs_url=None,
    redoc_url=None,
    default_response_class=IdSafeJSONResponse,
)

_ro: sqlite3.Connection | None = None


def ro() -> sqlite3.Connection:
    global _ro
    if _ro is None:
        try:
            _ro = db.connect_ro()
        except config.NoDatabase as exc:
            # 503, not 500: nothing is broken, there is just no archive yet.
            # The web UI turns this into the "Connect a database" screen.
            raise HTTPException(503, str(exc)) from exc
    return _ro


def rows(sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in ro().execute(sql, params)]


# ---------------------------------------------------------------- threads
#
# One person often turns up as several conversations - a Discord DM and an
# Instagram chat with the same human. A thread is filed under a person when it
# has exactly one counterpart and that counterpart is mapped on the People
# page; group chats have several counterparts, so they always stand alone.

_MEMBER_SQL = """
    SELECT u.id, COALESCE(p.display, u.display_name, u.name) AS name,
           u.avatar_sha256, u.person_id, p.display AS person,
           COALESCE(p.is_self, 0) AS is_self
    FROM users u
    LEFT JOIN people p ON p.person_id = u.person_id
    WHERE u.id IN (SELECT user_id FROM channel_participants WHERE channel_id = ?)
       OR u.id IN (SELECT DISTINCT sender_id FROM messages WHERE channel_id = ?)
"""


def _members(thread_id: int, *, counts: bool = False) -> list[dict]:
    """Everyone who joined or spoke in a thread.

    Message counts cost a scan of the thread, so they are opt-in: the
    conversation list asks for hundreds of threads and does not need them.
    """
    if counts:
        found = rows(
            _MEMBER_SQL.replace(
                "COALESCE(p.is_self, 0) AS is_self",
                "COALESCE(p.is_self, 0) AS is_self,"
                " (SELECT COUNT(*) FROM messages"
                "   WHERE sender_id = u.id AND channel_id = ?) AS messages",
            ),
            (thread_id, thread_id, thread_id),
        )
        return sorted(found, key=lambda m: -m["messages"])
    return sorted(rows(_MEMBER_SQL, (thread_id, thread_id)), key=lambda m: m["name"])


def _counterpart(members: list[dict]) -> int | None:
    """The person on the other side, when there is exactly one of them."""
    others = [m for m in members if not m["is_self"]]
    if len(others) != 1:
        return None
    return others[0]["person_id"]


@app.get("/api/threads")
def list_threads(platform: str | None = None, q: str | None = None) -> list[dict]:
    where, params = [], []
    if platform and platform != "all":
        where.append("c.platform = ?")
        params.append(platform)
    if q:
        where.append("c.name LIKE ?")
        params.append(f"%{q}%")
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    threads = rows(
        f"""
        SELECT c.id, c.platform, c.name, c.avatar_sha256, s.type AS kind,
               COUNT(m.message_id) AS messages,
               MIN(m.timestamp)    AS first_ts,
               MAX(m.timestamp)    AS last_ts,
               (SELECT text FROM messages
                 WHERE channel_id = c.id ORDER BY timestamp DESC LIMIT 1) AS preview
        FROM channels c
        LEFT JOIN servers  s ON s.id = c.server
        LEFT JOIN messages m ON m.channel_id = c.id
        {clause}
        GROUP BY c.id
        HAVING messages > 0
        ORDER BY last_ts DESC
        """,
        tuple(params),
    )

    for thread in threads:
        members = _members(thread["id"])
        person_id = _counterpart(members)
        thread["participants"] = [m["name"] for m in members]
        thread["person_id"] = person_id
        thread["person"] = next(
            (m["person"] for m in members if m["person_id"] == person_id), None
        )
        thread["preview"] = (thread["preview"] or "")[:160]
    return threads


def _person_threads(person_id: int) -> list[int]:
    """Every thread whose single counterpart is this person, busiest first."""
    candidates = rows(
        """
        SELECT DISTINCT channel_id FROM (
            SELECT channel_id FROM channel_participants
             WHERE user_id IN (SELECT id FROM users WHERE person_id = ?)
            UNION
            SELECT DISTINCT channel_id FROM messages
             WHERE sender_id IN (SELECT id FROM users WHERE person_id = ?)
        )
        """,
        (person_id, person_id),
    )
    return [
        row["channel_id"]
        for row in candidates
        if _counterpart(_members(row["channel_id"])) == person_id
    ]


def _thread_card(thread_id: int) -> dict | None:
    """The heading facts about a thread, for the chat switcher in the sidebar."""
    found = rows(
        """
        SELECT c.id, c.platform, c.name, c.avatar_sha256, s.type AS kind,
               COUNT(m.message_id) AS messages,
               MIN(m.timestamp)    AS first_ts,
               MAX(m.timestamp)    AS last_ts
        FROM channels c
        LEFT JOIN servers  s ON s.id = c.server
        LEFT JOIN messages m ON m.channel_id = c.id
        WHERE c.id = ?
        GROUP BY c.id
        """,
        (thread_id,),
    )
    if not found or not found[0]["messages"]:
        return None
    card = found[0]
    card["participants"] = _members(thread_id, counts=True)
    return card


@app.get("/api/threads/{thread_id}")
def thread_detail(thread_id: int) -> dict:
    found = rows(
        """
        SELECT c.id, c.platform, c.name, c.avatar_sha256, s.type AS kind
        FROM channels c LEFT JOIN servers s ON s.id = c.server
        WHERE c.id = ?
        """,
        (thread_id,),
    )
    if not found:
        raise HTTPException(404, "thread not found")
    thread = found[0]
    members = _members(thread_id, counts=True)
    thread["participants"] = members

    # Sibling chats with the same person, so the viewer can show them side by
    # side. A thread with no mapped counterpart is a group of one.
    person_id = _counterpart(members)
    sibling_ids = _person_threads(person_id) if person_id is not None else []
    if thread_id not in sibling_ids:
        sibling_ids.append(thread_id)
    siblings = [card for card in map(_thread_card, sibling_ids) if card]
    siblings.sort(key=lambda card: -card["messages"])
    thread["group"] = {
        "person_id": person_id,
        "person": next(
            (m["person"] for m in members if m["person_id"] == person_id), None
        ),
        "threads": siblings,
    }

    # Month histogram powers the date scrubber, so a 125k-message thread can be
    # navigated without loading it. Split by author, and covering every sibling
    # chat, so the sidebar can total whichever ones are on show.
    slots = ",".join("?" * len(sibling_ids))
    thread["months"] = rows(
        f"""
        SELECT m.channel_id,
               strftime('%Y-%m', m.timestamp / 1000, 'unixepoch') AS month,
               SUM(CASE WHEN COALESCE(p.is_self, 0) THEN 1 ELSE 0 END) AS mine,
               SUM(CASE WHEN COALESCE(p.is_self, 0) THEN 0 ELSE 1 END) AS theirs,
               COUNT(*) AS messages,
               MIN(m.timestamp) AS first_ts
        FROM messages m
        JOIN users u ON u.id = m.sender_id
        LEFT JOIN people p ON p.person_id = u.person_id
        WHERE m.channel_id IN ({slots})
        GROUP BY m.channel_id, month
        ORDER BY month
        """,
        tuple(sibling_ids),
    )
    stats = rows(
        "SELECT COUNT(*) AS messages, MIN(timestamp) AS first_ts, MAX(timestamp) AS last_ts "
        "FROM messages WHERE channel_id = ?",
        (thread_id,),
    )[0]
    thread.update(stats)
    return thread


def _decorate(messages: list[dict]) -> list[dict]:
    """Attach senders, attachments, reactions and embeds to a page of messages."""
    if not messages:
        return messages
    ids = [m["message_id"] for m in messages]
    slots = ",".join("?" * len(ids))
    by_id = {m["message_id"]: m for m in messages}
    for message in messages:
        message["attachments"] = []
        message["reactions"] = []
        message["embeds"] = []

    for row in rows(
        f"""
        SELECT ma.message_id, a.attachment_id, a.name, a.type, a.size,
               a.width, a.height, a.sha256, a.normalized_url
        FROM message_attachments ma
        JOIN attachments a ON a.attachment_id = ma.attachment_id
        WHERE ma.message_id IN ({slots})
        """,
        tuple(ids),
    ):
        by_id[row.pop("message_id")]["attachments"].append(row)

    for row in rows(
        f"""
        SELECT r.message_id, r.emoji_name, r.count,
               (SELECT GROUP_CONCAT(COALESCE(pu.display, u.display_name, u.name), ', ')
                  FROM message_reaction_actors ra
                  JOIN users u ON u.id = ra.user_id
                  LEFT JOIN people pu ON pu.person_id = u.person_id
                 WHERE ra.message_id = r.message_id AND ra.emoji_name = r.emoji_name) AS actors
        FROM message_reactions r WHERE r.message_id IN ({slots})
        """,
        tuple(ids),
    ):
        by_id[row.pop("message_id")]["reactions"].append(row)

    for row in rows(
        f"SELECT message_id, json FROM message_embeds WHERE message_id IN ({slots})",
        tuple(ids),
    ):
        try:
            by_id[row["message_id"]]["embeds"].append(json.loads(row["json"]))
        except ValueError:
            pass
    return messages


_MESSAGE_COLUMNS = """
    m.message_id, m.channel_id, m.timestamp, m.text, m.platform, m.is_unsent,
    m.sender_id, COALESCE(p.display, u.display_name, u.name) AS sender,
    u.avatar_sha256 AS sender_avatar, COALESCE(p.is_self, 0) AS is_self,
    rt.replied_to_id
"""

_MESSAGE_JOINS = """
    FROM messages m
    JOIN users u ON u.id = m.sender_id
    LEFT JOIN people p ON p.person_id = u.person_id
    LEFT JOIN message_replied_to rt ON rt.message_id = m.message_id
"""


@app.get("/api/threads/{thread_id}/messages")
def thread_messages(
    thread_id: int,
    before: int | None = None,
    after: int | None = None,
    at: int | None = None,
    ts: int | None = None,
    limit: int = Query(150, le=500),
) -> dict:
    """Keyset pagination - never OFFSET; the largest thread has 125k messages.

    `before`/`after` page outwards from a timestamp cursor; `at` centres the
    page on a message id (used by search hits); `ts` centres it on a moment in
    time (used by the month scrubber).
    """
    if at is not None or ts is not None:
        if at is not None:
            anchor = rows("SELECT timestamp FROM messages WHERE message_id = ?", (at,))
            if not anchor:
                raise HTTPException(404, "message not found")
            start = anchor[0]["timestamp"]
        else:
            start = ts
        older = rows(
            f"SELECT {_MESSAGE_COLUMNS} {_MESSAGE_JOINS} "
            "WHERE m.channel_id = ? AND m.timestamp < ? "
            "ORDER BY m.timestamp DESC LIMIT ?",
            (thread_id, start, limit // 2),
        )
        newer = rows(
            f"SELECT {_MESSAGE_COLUMNS} {_MESSAGE_JOINS} "
            "WHERE m.channel_id = ? AND m.timestamp >= ? "
            "ORDER BY m.timestamp ASC LIMIT ?",
            (thread_id, start, limit // 2),
        )
        page = list(reversed(older)) + newer
    elif after is not None:
        page = rows(
            f"SELECT {_MESSAGE_COLUMNS} {_MESSAGE_JOINS} "
            "WHERE m.channel_id = ? AND m.timestamp > ? "
            "ORDER BY m.timestamp ASC LIMIT ?",
            (thread_id, after, limit),
        )
    else:
        cursor = before if before is not None else (1 << 62)
        page = list(
            reversed(
                rows(
                    f"SELECT {_MESSAGE_COLUMNS} {_MESSAGE_JOINS} "
                    "WHERE m.channel_id = ? AND m.timestamp < ? "
                    "ORDER BY m.timestamp DESC LIMIT ?",
                    (thread_id, cursor, limit),
                )
            )
        )

    _decorate(page)
    return {
        "messages": page,
        "oldest": page[0]["timestamp"] if page else None,
        "newest": page[-1]["timestamp"] if page else None,
        "has_more": len(page) >= (limit // 2 if at is not None else limit),
    }


# ----------------------------------------------------------------- search


@app.get("/api/search")
def search(
    q: str,
    platform: str | None = None,
    thread: int | None = None,
    threads: str | None = None,
    sender: int | None = None,
    limit: int = Query(60, le=200),
    offset: int = 0,
) -> dict:
    try:
        built = query_mod.build_query(q, czech.lexicon())
    except query_mod.QueryError as exc:
        raise HTTPException(400, str(exc)) from exc
    expression = built.expression
    # Every word it widened, so the UI can say what it looked for besides.
    widened = [
        {"word": term.word, "lemmas": list(term.lemmas), "forms": len(term.forms)}
        for term in built.terms
    ]
    if not expression:
        return {"total": 0, "hits": [], "query": q, "terms": widened}

    where, params = ["messages_fts MATCH ?"], [expression]
    if platform and platform != "all":
        where.append("m.platform = ?")
        params.append(platform)
    if thread:
        where.append("m.channel_id = ?")
        params.append(thread)
    if threads is not None:
        # The in-conversation search covers every chat with the same person;
        # an explicitly empty list is a scope with nothing in it, not "all".
        wanted = [int(part) for part in threads.split(",") if part.strip()]
        if not wanted:
            return {"total": 0, "hits": [], "query": q, "terms": widened}
        where.append(f"m.channel_id IN ({','.join('?' * len(wanted))})")
        params.extend(wanted)
    if sender:
        where.append("m.sender_id = ?")
        params.append(sender)
    clause = " AND ".join(where)

    try:
        total = ro().execute(
            f"""
            SELECT COUNT(*) FROM messages_fts
            JOIN messages m ON m.message_id = messages_fts.rowid
            WHERE {clause}
            """,
            tuple(params),
        ).fetchone()[0]
        hits = rows(
            f"""
            SELECT m.message_id, m.channel_id, m.timestamp, m.platform, m.text,
                   c.name AS thread_name,
                   COALESCE(p.display, u.display_name, u.name) AS sender,
                   snippet(messages_fts, 0, '<mark>', '</mark>', '…', 14) AS snippet
            FROM messages_fts
            JOIN messages m ON m.message_id = messages_fts.rowid
            JOIN users    u ON u.id = m.sender_id
            LEFT JOIN people p ON p.person_id = u.person_id
            JOIN channels c ON c.id = m.channel_id
            WHERE {clause}
            ORDER BY m.timestamp DESC
            LIMIT ? OFFSET ?
            """,
            tuple(params) + (limit, offset),
        )
    except sqlite3.OperationalError as exc:
        raise HTTPException(400, f"bad search: {exc}") from exc
    return {
        "total": total,
        "hits": hits,
        "query": q,
        "expression": expression,
        "terms": widened,
    }


# ------------------------------------------------------------- statistics
#
# The Stats tab draws one person's history as a line per month: every message
# they exchanged, and - once a word is searched - only the messages that used
# it, in any inflected form. Counting here rather than in the browser means the
# chart never has to page through tens of thousands of hits to plot them.

_MONTH_COLUMNS = """
    strftime('%Y-%m', m.timestamp / 1000, 'unixepoch') AS month,
    SUM(CASE WHEN COALESCE(p.is_self, 0) THEN 1 ELSE 0 END) AS mine,
    SUM(CASE WHEN COALESCE(p.is_self, 0) THEN 0 ELSE 1 END) AS theirs,
    COUNT(*) AS messages
"""

_MONTH_JOINS = """
    JOIN users u ON u.id = m.sender_id
    LEFT JOIN people p ON p.person_id = u.person_id
"""


@app.get("/api/stats/months")
def stats_months(threads: str, q: str | None = None) -> dict:
    """Monthly message counts over a set of chats, optionally only matching ones.

    `threads` is the comma-separated scope - every chat with one person, which
    is what the Stats list offers. Without `q` the series is the whole history;
    with it, the same FTS expression the search page runs, counted per month.
    """
    try:
        wanted = [int(part) for part in threads.split(",") if part.strip()]
    except ValueError as exc:
        raise HTTPException(400, "bad thread id") from exc
    if not wanted:
        raise HTTPException(400, "threads required")

    expression, widened = "", []
    if q and q.strip():
        try:
            built = query_mod.build_query(q, czech.lexicon())
        except query_mod.QueryError as exc:
            raise HTTPException(400, str(exc)) from exc
        expression = built.expression
        widened = [
            {"word": term.word, "lemmas": list(term.lemmas), "forms": len(term.forms)}
            for term in built.terms
        ]
        # A search that reduces to nothing matched nothing, which is a real
        # answer: a flat line, not an error.
        if not expression:
            return {"months": [], "total": 0, "mine": 0, "theirs": 0, "query": q, "terms": []}

    slots = ",".join("?" * len(wanted))
    if expression:
        sql = (
            f"SELECT {_MONTH_COLUMNS} FROM messages_fts "
            "JOIN messages m ON m.message_id = messages_fts.rowid "
            f"{_MONTH_JOINS} "
            f"WHERE messages_fts MATCH ? AND m.channel_id IN ({slots})"
        )
        params: tuple = (expression, *wanted)
    else:
        sql = f"SELECT {_MONTH_COLUMNS} FROM messages m {_MONTH_JOINS} WHERE m.channel_id IN ({slots})"
        params = tuple(wanted)

    try:
        months = rows(sql + " GROUP BY month ORDER BY month", params)
    except sqlite3.OperationalError as exc:
        raise HTTPException(400, f"bad search: {exc}") from exc
    return {
        "months": months,
        "total": sum(m["messages"] for m in months),
        "mine": sum(m["mine"] for m in months),
        "theirs": sum(m["theirs"] for m in months),
        "query": q,
        "expression": expression,
        "terms": widened,
    }


# ------------------------------------------------------------------ media


@app.get("/api/media/{sha256}")
def media(sha256: str):
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise HTTPException(400, "bad hash")
    found = rows(
        "SELECT name, type, local_path FROM attachments "
        "WHERE sha256 = ? AND local_path IS NOT NULL LIMIT 1",
        (sha256,),
    )
    relpath = found[0]["local_path"] if found else None
    if relpath is None:
        # Avatars live in the vault but are not attachments; probe directly.
        for suffix in (".webp", ".png", ".jpg", ".gif", ".jpeg"):
            candidate = vault.relpath_for(sha256, suffix)
            if vault.exists(candidate):
                relpath = candidate
                break
    if relpath is None or not vault.exists(relpath):
        raise HTTPException(404, "not in vault")

    path = vault.abspath(relpath)
    return FileResponse(
        path,
        media_type=(found[0]["type"] if found else None) or "application/octet-stream",
        headers={"Cache-Control": "public, max-age=31536000, immutable", "ETag": sha256},
    )


# ----------------------------------------------------------------- people
#
# The `people` table is authoritative and these endpoints edit it directly, so
# the mapping lives in the archive file and travels with it.


def _reset_reader() -> None:
    """Drop the cached read-only connection so the next read sees the write."""
    global _ro
    if _ro:
        _ro.close()
        _ro = None


def _people_write(action):
    con = db.connect()
    try:
        result = action(con)
    except people_mod.PeopleError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        con.close()
    _reset_reader()
    return result


def _ints(values) -> list[int]:
    try:
        return [int(value) for value in values or []]
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "bad id") from exc


@app.get("/api/people")
def get_people() -> dict:
    con = ro()
    people = [
        dict(r)
        for r in con.execute(
            """
            SELECT p.person_id, p.display, p.is_self, p.notes,
                   COUNT(DISTINCT u.id) AS identities,
                   COUNT(m.message_id)  AS messages
            FROM people p
            LEFT JOIN users    u ON u.person_id = p.person_id
            LEFT JOIN messages m ON m.sender_id = u.id
            GROUP BY p.person_id
            ORDER BY p.display
            """
        )
    ]
    return {"people": people, "identities": people_mod.identities(con)}


@app.post("/api/people")
def create_person(payload: dict) -> dict:
    payload = payload or {}
    user_ids = _ints(payload.get("user_ids"))
    person_id = _people_write(
        lambda con: people_mod.create(
            con,
            display=payload.get("display") or "",
            is_self=bool(payload.get("is_self")),
            notes=payload.get("notes"),
            user_ids=user_ids,
        )
    )
    return {"person_id": person_id, "linked": len(user_ids)}


@app.patch("/api/people/{person_id}")
def update_person(person_id: int, payload: dict) -> dict:
    payload = payload or {}
    is_self = payload.get("is_self")
    _people_write(
        lambda con: people_mod.update(
            con,
            person_id,
            display=payload.get("display"),
            is_self=None if is_self is None else bool(is_self),
            notes=payload.get("notes"),
        )
    )
    return {"person_id": person_id}


@app.delete("/api/people/{person_id}")
def delete_person(person_id: int) -> dict:
    """Deleting a person only unlinks - no message or identity is removed."""
    return {"unlinked": _people_write(lambda con: people_mod.delete(con, person_id))}


@app.post("/api/people/link")
def link_identities(payload: dict) -> dict:
    """Attach identities to a person, or detach them when person_id is null."""
    payload = payload or {}
    raw = payload.get("person_id")
    person_id = None if raw in (None, "", "null") else _ints([raw])[0]
    user_ids = _ints(payload.get("user_ids"))
    if not user_ids:
        raise HTTPException(400, "user_ids required")
    return {"linked": _people_write(lambda con: people_mod.link(con, person_id, user_ids))}


# --------------------------------------------------------------- database
# The archive itself is not part of this repository, so a fresh checkout starts
# with nothing connected. These endpoints are what the UI offers instead of a
# blank page: pick an existing .sqlite, or create an empty one.


def _describe(path: Path | None) -> dict:
    """What we can say about a database file without trusting it."""
    if path is None:
        return {"path": None, "exists": False}
    info = {"path": str(path), "exists": path.is_file()}
    if not info["exists"]:
        return info
    info["size"] = path.stat().st_size
    try:
        probe = db.connect_ro(path)
        try:
            info["messages"] = probe.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            info["threads"] = probe.execute(
                "SELECT COUNT(*) FROM channels"
            ).fetchone()[0]
        finally:
            probe.close()
    except sqlite3.Error as exc:
        info["error"] = f"not a usable archive: {exc}"
    return info


@app.get("/api/db")
def db_status() -> dict:
    """Which archive is connected, if any."""
    return {
        "connected": config.is_connected(),
        "vault_path": str(config.VAULT_DIR),
        "settings_path": str(config.SETTINGS_FILE),
        **_describe(config.DB_PATH),
    }


@app.post("/api/db/pick")
def db_pick(payload: dict | None = None) -> dict:
    """Open a native file dialog and report what was chosen, without connecting."""
    create = bool((payload or {}).get("create"))
    try:
        path = picker.ask_new_database() if create else picker.ask_database()
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc
    if not path:
        return {"path": None}
    return {"create": create, **_describe(Path(path))}


@app.post("/api/db/connect")
def db_connect(payload: dict) -> dict:
    """Use this database from now on, creating an empty one if asked."""
    raw = (payload or {}).get("path")
    if not raw:
        raise HTTPException(400, "path required")
    path = Path(str(raw).strip('"')).expanduser()
    try:
        if (payload or {}).get("create"):
            migrate_mod.create_archive(path, verbose=False)
        elif not path.is_file():
            raise HTTPException(400, f"No such database file: {path}")
        else:
            # An archive made by an older version, or a raw DHT mirror, is
            # missing our columns - add them rather than refusing to open it.
            con = db.connect(path)
            try:
                migrate_mod.ensure_schema(con)
            finally:
                con.close()
        config.connect_db(path)
    except FileExistsError as exc:
        raise HTTPException(400, str(exc)) from exc
    except config.NoDatabase as exc:
        raise HTTPException(400, str(exc)) from exc
    except sqlite3.Error as exc:
        raise HTTPException(400, f"That file is not a usable archive: {exc}") from exc
    _reset_reader()
    return db_status()


# ----------------------------------------------------------------- ingest


def _picked(ask) -> dict:
    """Run a native dialog, then report what is importable at what it returned."""
    try:
        path = ask()
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc
    if not path:
        return {"path": None, "sources": []}
    try:
        return {"path": path, "sources": [s.summary() for s in detect_exports(path)]}
    except ValueError as exc:
        return {"path": path, "sources": [], "error": str(exc)}


@app.post("/api/ingest/pick-folder")
def pick_folder() -> dict:
    return _picked(picker.ask_directory)


@app.post("/api/ingest/pick-dht")
def pick_dht() -> dict:
    return _picked(picker.ask_dht)


@app.post("/api/ingest/inspect")
def inspect(payload: dict) -> dict:
    path = (payload or {}).get("path")
    if not path:
        raise HTTPException(400, "path required")
    try:
        return {"path": path, "sources": [s.summary() for s in detect_exports(path)]}
    except ValueError as exc:
        return {"path": path, "sources": [], "error": str(exc)}


@app.post("/api/ingest/run")
def run_ingest(payload: dict):
    path = (payload or {}).get("path")
    if not path:
        raise HTTPException(400, "path required")

    def stream() -> Iterator[str]:
        for event in runner.stream_ingest(path):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        global _ro
        if _ro:
            _ro.close()
            _ro = None

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/ingest/history")
def ingest_history() -> dict:
    return {
        "ingest": rows(
            "SELECT * FROM ingest_log ORDER BY run_id DESC LIMIT 50"
        ),
        "sync": rows(
            "SELECT run_id, started_at, finished_at, new_messages, new_attachments, new_blobs "
            "FROM sync_log ORDER BY run_id DESC LIMIT 20"
        ),
    }


@app.get("/api/stats")
def stats() -> dict:
    platforms = rows(
        """
        SELECT platform, COUNT(*) AS messages,
               COUNT(DISTINCT channel_id) AS threads,
               COUNT(DISTINCT sender_id)  AS senders,
               MIN(timestamp) AS first_ts, MAX(timestamp) AS last_ts
        FROM messages GROUP BY platform ORDER BY messages DESC
        """
    )
    media_stats = rows(
        "SELECT COUNT(*) AS total, SUM(sha256 IS NOT NULL) AS stored FROM attachments"
    )[0]
    return {
        "platforms": platforms,
        "total": sum(p["messages"] for p in platforms),
        "media": media_stats,
        "db_path": str(config.DB_PATH),
        "vault_path": str(config.VAULT_DIR),
    }


# ------------------------------------------------------------------ pages

class SpaStaticFiles(StaticFiles):
    """Serve the built app, falling back to index.html for client-side routes."""

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise


if config.WEB_BUILD.is_dir():
    app.mount("/", SpaStaticFiles(directory=str(config.WEB_BUILD), html=True), name="web")
else:

    @app.get("/")
    def missing_build() -> Any:
        return JSONResponse(
            {
                "error": "web UI not built",
                "fix": "cd app/web && npm install && npm run build",
                "api": "/api/stats",
            },
            status_code=503,
        )

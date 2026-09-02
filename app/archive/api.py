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

from . import config, db, migrate as migrate_mod, picker, vault
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
               MAX(m.timestamp)    AS last_ts
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
        thread["participants"] = [
            r["name"]
            for r in rows(
                """
                SELECT DISTINCT COALESCE(pp.display, u.display_name, u.name) AS name
                FROM users u
                LEFT JOIN people pp ON pp.person_id = u.person_id
                WHERE u.id IN (SELECT user_id FROM channel_participants WHERE channel_id = ?)
                   OR u.id IN (SELECT DISTINCT sender_id FROM messages WHERE channel_id = ?)
                ORDER BY name
                """,
                (thread["id"], thread["id"]),
            )
        ]
        last = rows(
            "SELECT text FROM messages WHERE channel_id = ? ORDER BY timestamp DESC LIMIT 1",
            (thread["id"],),
        )
        thread["preview"] = (last[0]["text"] if last else "")[:160]
    return threads


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
    thread["participants"] = rows(
        """
        SELECT u.id, COALESCE(p.display, u.display_name, u.name) AS name,
               u.avatar_sha256, u.person_id, p.display AS person, p.is_self,
               (SELECT COUNT(*) FROM messages WHERE sender_id = u.id AND channel_id = ?) AS messages
        FROM users u
        LEFT JOIN people p ON p.person_id = u.person_id
        WHERE u.id IN (SELECT user_id FROM channel_participants WHERE channel_id = ?)
           OR u.id IN (SELECT DISTINCT sender_id FROM messages WHERE channel_id = ?)
        ORDER BY messages DESC
        """,
        (thread_id, thread_id, thread_id),
    )
    # Month histogram powers the date scrubber, so a 125k-message thread can be
    # navigated without loading it.
    thread["months"] = rows(
        """
        SELECT strftime('%Y-%m', timestamp / 1000, 'unixepoch') AS month,
               COUNT(*) AS messages, MIN(timestamp) AS first_ts
        FROM messages WHERE channel_id = ?
        GROUP BY month ORDER BY month
        """,
        (thread_id,),
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

_TOKEN_RE = re.compile(r'"[^"]*"|\S+')


def to_fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 expression.

    Users type words, not FTS syntax, so every token is quoted. A trailing *
    is preserved as a prefix search.
    """
    parts = []
    for token in _TOKEN_RE.findall(text.strip()):
        if token.startswith('"') and token.endswith('"') and len(token) > 1:
            inner = token[1:-1].replace('"', '""')
            parts.append(f'"{inner}"')
            continue
        prefix = token.endswith("*")
        word = token.rstrip("*").replace('"', '""')
        if word:
            parts.append(f'"{word}"*' if prefix else f'"{word}"')
    return " AND ".join(parts)


@app.get("/api/search")
def search(
    q: str,
    platform: str | None = None,
    thread: int | None = None,
    sender: int | None = None,
    limit: int = Query(60, le=200),
    offset: int = 0,
) -> dict:
    expression = to_fts_query(q)
    if not expression:
        return {"total": 0, "hits": [], "query": q}

    where, params = ["messages_fts MATCH ?"], [expression]
    if platform and platform != "all":
        where.append("m.platform = ?")
        params.append(platform)
    if thread:
        where.append("m.channel_id = ?")
        params.append(thread)
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
    return {"total": total, "hits": hits, "query": q, "expression": expression}


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
# The database is authoritative and these endpoints edit it directly; every
# mutation rewrites people.yaml afterwards so the file stays a readable mirror
# that can still be hand-edited and re-applied.


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
    return {
        "people": people,
        "identities": people_mod.identities(con),
        "yaml_path": str(config.PEOPLE_YAML),
    }


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


@app.post("/api/people/apply")
def apply_people() -> dict:
    """Read the YAML file back in - the other half of the round trip."""
    con = db.connect()
    try:
        count, linked = people_mod.apply(con)
    finally:
        con.close()
    _reset_reader()
    return {"people": count, "linked": linked}


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


@app.post("/api/ingest/pick-folder")
def pick_folder() -> dict:
    try:
        path = picker.ask_directory()
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc
    if not path:
        return {"path": None, "sources": []}
    try:
        sources = [s.summary() for s in detect_exports(path)]
    except ValueError as exc:
        return {"path": path, "sources": [], "error": str(exc)}
    return {"path": path, "sources": sources}


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

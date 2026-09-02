"""End-to-end checks against the real archive.

    py smoke_test.py

Exercises every API endpoint plus the invariants that matter: ingest is
idempotent, ids survive the round trip, encoding is repaired, and the full-text
index stays in step with the messages table.

Read-only apart from the ingest re-runs, which by design must change nothing,
the people-editing section, and the pluggable-database section - both of which
put everything back as they found it.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

from archive import config, db  # noqa: E402
from archive.api import app  # noqa: E402
from archive.ids import demojibake, message_source_key, synth_id  # noqa: E402
from archive.ingest.runner import ingest_path  # noqa: E402

client = TestClient(app)
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{f' - {detail}' if detail else ''}")
    if not condition:
        failures.append(label)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def db_tables(path: Path) -> list[str]:
    con = sqlite3.connect(str(path))
    try:
        return [r[0] for r in con.execute("SELECT name FROM sqlite_master")]
    finally:
        con.close()


# ---------------------------------------------------------------- database
section("database")
con = db.connect_ro()
total = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
fts = con.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
check("full-text index covers every message", total == fts, f"{total} messages, {fts} indexed")

by_platform = dict(con.execute("SELECT platform, COUNT(*) FROM messages GROUP BY platform"))
check("all three platforms present", set(by_platform) == {"discord", "facebook", "instagram"},
      json.dumps(by_platform))

check(
    "Meta ids are negative, Discord ids positive",
    con.execute(
        "SELECT COUNT(*) FROM messages WHERE (platform = 'discord') != (message_id > 0)"
    ).fetchone()[0] == 0,
)

check(
    "no mojibake left in message text",
    con.execute(
        "SELECT COUNT(*) FROM messages WHERE platform <> 'discord' "
        "AND (text LIKE '%Ã%' OR text LIKE '%Å¾%' OR text LIKE '%Ä%')"
    ).fetchone()[0] == 0,
)

check(
    "every Meta message has a dedup key",
    con.execute(
        "SELECT COUNT(*) FROM messages WHERE platform <> 'discord' AND source_key IS NULL"
    ).fetchone()[0] == 0,
)

orphans = con.execute(
    "SELECT COUNT(*) FROM messages m LEFT JOIN users u ON u.id = m.sender_id WHERE u.id IS NULL"
).fetchone()[0]
check("every message has a sender", orphans == 0, f"{orphans} orphans")

# ------------------------------------------------------------------- vault
section("media vault")
from archive import vault  # noqa: E402

stored = con.execute(
    "SELECT sha256, local_path FROM attachments WHERE local_path IS NOT NULL"
).fetchall()
missing = [row["local_path"] for row in stored if not vault.exists(row["local_path"])]
check("every stored attachment exists on disk", not missing, f"{len(missing)} missing")
check("vault holds files", len(stored) > 0, f"{len(stored)} attachments")

# --------------------------------------------------------------------- ids
section("id synthesis")
check("synthetic ids are always negative", all(synth_id("x", i) < 0 for i in range(2000)))
check(
    "synthetic ids are deterministic",
    synth_id("instagram", "inbox/foo") == synth_id("instagram", "inbox/foo"),
)
check("demojibake repairs Meta text", demojibake('DobrÃ¡ zprÃ¡va, mÅ¯Å¾eÅ¡') == 'Dobrá zpráva, můžeš')
check("demojibake leaves clean text alone", demojibake("už čeština") == "už čeština")
check(
    "source keys separate identical text at different times",
    message_source_key("instagram", "t", {"sender_name": "A", "timestamp_ms": 1, "content": "x"})
    != message_source_key("instagram", "t", {"sender_name": "A", "timestamp_ms": 2, "content": "x"}),
)

# --------------------------------------------------------------------- api
section("api endpoints")
stats = client.get("/api/stats").json()
check("GET /api/stats", stats["total"] == total, f"{stats['total']}")

threads = client.get("/api/threads").json()
check("GET /api/threads", len(threads) > 0, f"{len(threads)} threads")
check("thread ids are strings", all(isinstance(t["id"], str) for t in threads))
check(
    "no id lost precision",
    all(str(int(t["id"])) == t["id"] for t in threads),
)

biggest = max(threads, key=lambda t: t["messages"])
detail = client.get(f"/api/threads/{biggest['id']}").json()
check("GET /api/threads/{id}", detail["messages"] == biggest["messages"])
check("month histogram present", len(detail["months"]) > 0, f"{len(detail['months'])} months")

page = client.get(f"/api/threads/{biggest['id']}/messages?limit=50").json()
check("GET messages (latest page)", len(page["messages"]) == 50)
check("messages ascend by time",
      all(a["timestamp"] <= b["timestamp"] for a, b in zip(page["messages"], page["messages"][1:])))

older = client.get(
    f"/api/threads/{biggest['id']}/messages?before={page['oldest']}&limit=50"
).json()
check("keyset paging backwards", len(older["messages"]) == 50)
check(
    "pages do not overlap",
    not ({m["message_id"] for m in older["messages"]} & {m["message_id"] for m in page["messages"]}),
)

first_month = detail["months"][0]
jump = client.get(
    f"/api/threads/{biggest['id']}/messages?ts={first_month['first_ts']}&limit=40"
).json()
check("jump to a month", len(jump["messages"]) > 0, f"{len(jump['messages'])} messages")

search = client.get("/api/search?q=necekal").json()
plain = client.get("/api/search?q=nečekal").json()
check("diacritics-insensitive search", search["total"] == plain["total"] > 0,
      f"{search['total']} hits both ways")
check("search spans platforms",
      len({h["platform"] for h in search["hits"]}) > 1,
      str({h["platform"] for h in search["hits"]}))
check("search injection is neutralised", client.get('/api/search?q=" OR 1=1 --').status_code == 200)
check("empty search is handled", client.get("/api/search?q=   ").json()["total"] == 0)

hit = search["hits"][0]
context = client.get(f"/api/threads/{hit['channel_id']}/messages?at={hit['message_id']}&limit=20")
check("jump to a search hit", context.status_code == 200)
check(
    "the hit is inside the returned context",
    hit["message_id"] in {m["message_id"] for m in context.json()["messages"]},
)

sha = next(
    (a["sha256"] for m in page["messages"] for a in m["attachments"] if a["sha256"]),
    stored[0]["sha256"] if stored else None,
)
if sha:
    media = client.get(f"/api/media/{sha}")
    check("GET /api/media/{sha256}", media.status_code == 200, f"{len(media.content)} bytes")
check("bad media hash rejected", client.get("/api/media/nope").status_code == 400)
check("unknown media hash is 404", client.get(f"/api/media/{'0' * 64}").status_code == 404)

people = client.get("/api/people").json()
check("GET /api/people", "identities" in people, f"{len(people['identities'])} identities")
check("someone is marked as self", any(p["is_self"] for p in people["people"]))

check("GET /api/ingest/history", "ingest" in client.get("/api/ingest/history").json())

inspect = client.post("/api/ingest/inspect", json={"path": str(config.PROJECT_ROOT)}).json()
check("POST /api/ingest/inspect finds both exports", len(inspect["sources"]) == 2,
      str([s["label"] for s in inspect["sources"]]))
check(
    "inspect reports a useful error for a non-export folder",
    "error" in client.post("/api/ingest/inspect", json={"path": r"C:\Windows"}).json(),
)
check("inspect requires a path", client.post("/api/ingest/inspect", json={}).status_code == 400)

check("SPA deep link serves the app", client.get("/search").status_code == 200)

# ---------------------------------------------------------- people editing
section("people editing (creates a person, then removes it again)")
NAME = "SMOKE Zkouška ěščř"
RENAMED = NAME + " 2"
before_people = client.get("/api/people").json()
was_self = [p["person_id"] for p in before_people["people"] if p["is_self"]]
victim = next(
    r for r in before_people["identities"] if r["person_id"] is None and r["messages"] > 0
)

created = client.post("/api/people", json={"display": NAME, "user_ids": [victim["id"]]})
check("POST /api/people creates and links", created.status_code == 200, created.text)
person_id = created.json()["person_id"]
check("duplicate name is refused", client.post("/api/people", json={"display": NAME}).status_code == 400)
check("empty name is refused", client.post("/api/people", json={"display": " "}).status_code == 400)



def linked_row(payload: dict) -> dict:
    return next(r for r in payload["identities"] if r["id"] == victim["id"])


state = client.get("/api/people").json()
check("the identity now carries the custom name", linked_row(state)["person"] == NAME)
check("the custom name replaces the platform name in threads",
      any(NAME in t["participants"] for t in client.get("/api/threads").json()))
check("people.yaml mirrors the change", NAME in config.PEOPLE_YAML.read_text(encoding="utf-8"))

client.patch(f"/api/people/{person_id}", json={"display": RENAMED})
check("rename propagates", linked_row(client.get("/api/people").json())["person"] == RENAMED)

client.patch(f"/api/people/{person_id}", json={"is_self": True})
selves = [p["person_id"] for p in client.get("/api/people").json()["people"] if p["is_self"]]
check("only one person can be self", selves == [person_id], str(selves))

check("unlink detaches an identity",
      client.post("/api/people/link", json={"person_id": None, "user_ids": [victim["id"]]})
      .json()["linked"] == 1)
check("the detached identity falls back to its platform name",
      linked_row(client.get("/api/people").json())["person"] is None)
check("linking to a missing person is refused",
      client.post("/api/people/link", json={"person_id": 999999, "user_ids": [victim["id"]]})
      .status_code == 400)

check("delete removes the person", client.delete(f"/api/people/{person_id}").status_code == 200)
final = client.get("/api/people").json()
check("the archive is back where it started",
      len(final["people"]) == len(before_people["people"])
      and linked_row(final)["person_id"] is None)
for pid in was_self:
    client.patch(f"/api/people/{pid}", json={"is_self": True})
check("the original self flag is restored",
      [p["person_id"] for p in client.get("/api/people").json()["people"] if p["is_self"]] == was_self)


# ------------------------------------------------------ pluggable database
# The archive is not part of the repository, so a fresh checkout has nothing to
# read. Prove the app can be handed a database - including one it just made.
section("pluggable database (creates a throwaway archive, then reconnects)")
original_db = config.DB_PATH
original_settings = config.SETTINGS_FILE.read_text(encoding="utf-8") if config.SETTINGS_FILE.is_file() else None

status = client.get("/api/db").json()
check("GET /api/db reports the connection", status["connected"] is True, status["path"])
check("it counts what is inside", status["messages"] == total, str(status.get("messages")))

try:
    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp) / "fresh.sqlite"
        made = client.post("/api/db/connect", json={"path": str(fresh), "create": True})
        check("POST /api/db/connect --create builds an archive", made.status_code == 200, made.text)
        check("the new archive is connected and empty",
              made.json()["connected"] and made.json()["messages"] == 0, made.text)
        check("every table is there", len(db_tables(fresh)) >= 20, f"{len(db_tables(fresh))} objects")
        check("the viewer serves the empty archive", client.get("/api/threads").json() == [])
        check("creating over an existing file is refused",
              client.post("/api/db/connect", json={"path": str(fresh), "create": True}).status_code == 400)
        check("connecting to a missing file is refused",
              client.post("/api/db/connect", json={"path": str(Path(tmp) / "nope.sqlite")}).status_code == 400)

        back = client.post("/api/db/connect", json={"path": str(original_db)})
        check("reconnecting to the real archive works", back.status_code == 200, back.text)
        check("all the messages are back", back.json()["messages"] == total, back.text)
finally:
    # Whatever happened above, this machine must end up on its own archive.
    if original_settings is None:
        config.forget()
    else:
        config.SETTINGS_FILE.write_text(original_settings, encoding="utf-8")
        config._resolve()

check("the original database is connected again", config.DB_PATH == original_db, str(config.DB_PATH))


# --------------------------------------------------------------- idempotency
section("ingest idempotency (re-running must change nothing)")
before_counts = (
    total,
    con.execute("SELECT COUNT(*) FROM attachments").fetchone()[0],
    len(list(config.VAULT_DIR.rglob("*"))) if config.VAULT_DIR.is_dir() else 0,
)

for folder in ("your_instagram_activity", "your_facebook_activity"):
    path = config.PROJECT_ROOT / folder
    if not path.is_dir():
        continue
    for kind, stats_row in ingest_path(path):
        check(f"re-ingest {kind}: no new messages", stats_row.new_msgs == 0,
              f"{stats_row.dup_msgs} duplicates skipped")
        check(f"re-ingest {kind}: no new media", stats_row.new_media == 0)
        check(f"re-ingest {kind}: nothing missing", stats_row.missing_media == 0)

con.close()
con = db.connect_ro()
after_counts = (
    con.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
    con.execute("SELECT COUNT(*) FROM attachments").fetchone()[0],
    len(list(config.VAULT_DIR.rglob("*"))) if config.VAULT_DIR.is_dir() else 0,
)
check("row and file counts unchanged", before_counts == after_counts,
      f"{before_counts} -> {after_counts}")
check(
    "full-text index still in step",
    con.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0] == after_counts[0],
)

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")

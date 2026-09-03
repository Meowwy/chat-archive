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

from archive import config, db, picker  # noqa: E402
from archive.api import app  # noqa: E402
from archive.ids import demojibake, message_source_key, synth_id  # noqa: E402
from archive.ingest.detect import detect  # noqa: E402
from archive.ingest.runner import ingest_path  # noqa: E402
from archive.noise import is_reaction_notice  # noqa: E402
from archive import czech, query  # noqa: E402

CZECH_LETTERS = "áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ"

client = TestClient(app)
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{f' - {detail}' if detail else ''}")
    if not condition:
        failures.append(label)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def refused(call) -> bool:
    """True when detect() turned something down rather than importing it."""
    try:
        call()
    except ValueError:
        return True
    return False


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
check(
    "the month jump lands in that month",
    jump["messages"][0]["timestamp"] <= first_month["first_ts"] <= jump["messages"][-1]["timestamp"]
    or jump["messages"][0]["timestamp"] == first_month["first_ts"],
)

section("one person, many chats")
check(
    "months are split by author",
    all(m["mine"] + m["theirs"] == m["messages"] for m in detail["months"]),
)
check(
    "months carry the chat they belong to",
    {m["channel_id"] for m in detail["months"]}
    <= {t["id"] for t in detail["group"]["threads"]},
)
check(
    "the thread is in its own group",
    biggest["id"] in {t["id"] for t in detail["group"]["threads"]},
)
grouped = [t for t in threads if t["person_id"] is not None]
check(
    "a mapped counterpart files the chat under a person",
    all(t["person"] for t in grouped),
    f"{len({t['person_id'] for t in grouped})} people over {len(grouped)} chats",
)
for candidate in sorted(threads, key=lambda t: -t["messages"]):
    if candidate["person_id"] is None:
        check(
            "a chat with no mapped counterpart stands alone",
            client.get(f"/api/threads/{candidate['id']}").json()["group"]["threads"] == []
            or len(client.get(f"/api/threads/{candidate['id']}").json()["group"]["threads"]) == 1,
        )
        break

search = client.get("/api/search?q=necekal").json()
plain = client.get("/api/search?q=nečekal").json()
check("diacritics-insensitive search", search["total"] == plain["total"] > 0,
      f"{search['total']} hits both ways")
check("search spans platforms",
      len({h["platform"] for h in search["hits"]}) > 1,
      str({h["platform"] for h in search["hits"]}))
check("search injection is neutralised", client.get('/api/search?q=" OR 1=1 --').status_code == 200)
check("empty search is handled", client.get("/api/search?q=   ").json()["total"] == 0)

section("Czech morphology")

# The one invariant the whole feature rests on: czech.fold() has to agree with
# the FTS tokenizer exactly. If it ever drifts, expansion silently stops
# matching and nothing else here would notice.
probe = sqlite3.connect(":memory:")
probe.execute('CREATE VIRTUAL TABLE t USING fts5(x, tokenize="unicode61 remove_diacritics 2")')
probe.execute("CREATE VIRTUAL TABLE v USING fts5vocab(t, 'row')")
probe.execute("INSERT INTO t VALUES (?)", (" ".join(f"x{c}x" for c in CZECH_LETTERS),))
indexed = sorted(r[0] for r in probe.execute("SELECT term FROM v"))
check(
    "fold() matches the FTS tokenizer on every Czech letter",
    indexed == sorted({czech.fold(f"x{c}x") for c in CZECH_LETTERS}),
    f"{len(indexed)} distinct terms",
)
probe.close()

lexicon = czech.lexicon()
check(
    "the Czech dictionary is built",
    lexicon is not None,
    str(config.LEXICON_PATH) if lexicon else "run: py -m archive czech-dict",
)

if lexicon is not None:
    def expand(word):
        found = lexicon.expand(word)
        return set(found.forms) if found else set()

    hospoda, cekat, necekal = expand("hospoda"), expand("cekal"), expand("necekal")
    check("a noun widens to its whole paradigm",
          {"hospoda", "hospody", "hospode", "hospodu"} <= hospoda, f"{len(hospoda)} forms")
    check("the typed form is diacritic-blind", expand("hospodě") == hospoda)
    check("negation is kept apart",
          "necekal" not in cekat and "cekal" not in necekal and "cekal" in cekat,
          f"{len(cekat)} positive, {len(necekal)} negated")
    check("the two halves never overlap", not (cekat & necekal))
    check("a superlative stays with its lemma", "nejvetsi" in expand("velky"))
    check("a lemma that starts with ne- is left whole",
          expand("nevim") == expand("nevím") and "nevim" in expand("nevim"))
    check("an unknown word has no paradigm", lexicon.expand("zzzqx") is None)

    czech_search = client.get("/api/search?q=hospoda").json()
    exact = client.get('/api/search?q="hospoda"').json()
    check("searching a word finds its other forms",
          czech_search["total"] > exact["total"] > 0,
          f"{czech_search['total']} widened vs {exact['total']} exact")
    check("a quoted word is not widened", exact["terms"] == [])
    check("the response says what it widened",
          czech_search["terms"][0]["lemmas"] == ["hospoda"]
          and czech_search["terms"][0]["forms"] > 1)
    check("an inflected form finds the same messages",
          client.get("/api/search?q=hospody").json()["total"] == czech_search["total"])

section("the search query language")


def hits_for(q):
    return client.get("/api/search", params={"q": q}).json()["total"]


beer, pub = hits_for("pivo"), hits_for("hospoda")
check("OR takes the union", hits_for("pivo OR hospoda") >= max(beer, pub) > 0,
      f"{hits_for('pivo OR hospoda')} >= max({beer}, {pub})")
check("juxtaposition still means AND", hits_for("pivo hospoda") <= min(beer, pub))
check("AND can be spelled out", hits_for("pivo AND hospoda") == hits_for("pivo hospoda"))
check("| is a shorthand for OR", hits_for("pivo | hospoda") == hits_for("pivo OR hospoda"))
# (A or B) and B is just B, while A or (B and B) is the union - so these two
# only agree if the brackets were ignored.
check("brackets group",
      hits_for("(pivo OR hospoda) hospoda") == pub
      and hits_for("pivo OR hospoda hospoda") == hits_for("pivo OR hospoda") > pub,
      f"{hits_for('(pivo OR hospoda) hospoda')} vs {hits_for('pivo OR hospoda hospoda')}")
check("a minus excludes", hits_for("pivo -hospoda") == beer - hits_for("pivo hospoda"),
      f"{hits_for('pivo -hospoda')} = {beer} - {hits_for('pivo hospoda')}")
check("NOT spells the same thing", hits_for("pivo NOT hospoda") == hits_for("pivo -hospoda"))
check("a prefix search still works", hits_for("hospod*") > 0)
check("lowercase or is an ordinary word, not an operator",
      '"or"' in query.build_query("pivo or hospoda", None).expression
      and hits_for("pivo or hospoda") < hits_for("pivo OR hospoda"))
check("a quoted OR is searched for, not obeyed",
      '"OR"' in query.build_query('"OR"', None).expression)

for bad, why in [
    ("-hospoda", "only exclusions"),
    ("(pivo", "an unclosed bracket"),
    ("pivo)", "a stray bracket"),
    ("NOT", "a dangling NOT"),
]:
    response = client.get("/api/search", params={"q": bad})
    check(f"{why} is refused politely", response.status_code == 400, f"{bad!r} -> {response.status_code}")

check("an unknown word searches literally, without erroring",
      client.get("/api/search?q=zzzqx").json()["total"] == 0)
check("search still works with no dictionary",
      query.build_query("hospoda", None).expression.startswith("("))

hit = search["hits"][0]
context = client.get(f"/api/threads/{hit['channel_id']}/messages?at={hit['message_id']}&limit=20")
check("jump to a search hit", context.status_code == 200)
check(
    "the hit is inside the returned context",
    hit["message_id"] in {m["message_id"] for m in context.json()["messages"]},
)

section("reaction notices and search normalisation")
notices = [
    row["text"]
    for row in con.execute(
        "SELECT text FROM messages "
        "WHERE text LIKE '%to your message%' OR text LIKE '%liked a message%'"
    )
    if is_reaction_notice(row["text"])
]
check("no reaction pseudo-messages left in the archive", not notices, f"{len(notices)} found")
check(
    "the notice filter knows a notice from a sentence",
    is_reaction_notice("Reacted 👍 to your message")
    and is_reaction_notice("someone liked a message")
    and not is_reaction_notice("I really liked that message you sent"),
)

apostrophe = client.get("/api/search?q=don't").json()
no_apostrophe = client.get("/api/search?q=dont").json()
check(
    "apostrophes are ignored the way diacritics are",
    apostrophe["total"] == no_apostrophe["total"] > 0,
    f"{apostrophe['total']} hits either way",
)

group_ids = [t["id"] for t in detail["group"]["threads"]]
scoped = client.get("/api/search?q=a&threads=" + ",".join(group_ids)).json()
check(
    "in-conversation search stays inside the group",
    all(h["channel_id"] in group_ids for h in scoped["hits"]),
    f"{scoped['total']} hits across {len(group_ids)} chat(s)",
)
check(
    "an empty thread filter finds nothing",
    client.get("/api/search?q=a&threads=").json()["total"] == 0,
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


# ------------------------------------------------------------ file dialogs
# The native dialog runs in a subprocess, and its answer used to come back
# decoded with the console codepage: "G:\Můj disk" arrived as "G:\MĹŻj disk",
# a path that does not exist. Ask that subprocess for a fixed non-ASCII path -
# the expression stands in for the dialog, so nothing opens.
section("native file dialogs")
DIALOG_ANSWER = "G:/Můj disk/Archív"
check(
    "a non-ASCII path survives the picker subprocess",
    picker._ask(f"askdirectory.__name__ and {DIALOG_ANSWER!r}", 60) == DIALOG_ANSWER,
)
check(
    "a cancelled dialog reads as nothing chosen",
    picker._ask("askdirectory.__name__ and ''", 60) is None,
)

# ------------------------------------------------------------ .dht import
# A tracker file is a SQLite database of its own, so importing it is a copy. One
# is built here from scratch, in DHT's exact table shapes and without any of the
# columns this archive adds, then imported into a throwaway archive twice.
section("discord .dht import (builds a tracker file, imports it twice)")

DHT_SCHEMA = """
CREATE TABLE servers (id INTEGER PRIMARY KEY NOT NULL, name TEXT NOT NULL, type TEXT NOT NULL);
CREATE TABLE channels (id INTEGER PRIMARY KEY NOT NULL, server INTEGER NOT NULL, name TEXT NOT NULL,
    parent_id INTEGER, position INTEGER, topic TEXT, nsfw INTEGER);
CREATE TABLE users (id INTEGER PRIMARY KEY NOT NULL, name TEXT NOT NULL, display_name TEXT,
    avatar_url TEXT, discriminator TEXT);
CREATE TABLE messages (message_id INTEGER PRIMARY KEY NOT NULL, sender_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL, text TEXT NOT NULL, timestamp INTEGER NOT NULL);
CREATE TABLE attachments (attachment_id INTEGER PRIMARY KEY NOT NULL, name TEXT NOT NULL,
    type TEXT, normalized_url TEXT NOT NULL, download_url TEXT, size INTEGER NOT NULL,
    width INTEGER, height INTEGER);
CREATE TABLE message_attachments (message_id INTEGER NOT NULL, attachment_id INTEGER NOT NULL,
    PRIMARY KEY (message_id, attachment_id));
CREATE TABLE download_metadata (normalized_url TEXT NOT NULL PRIMARY KEY,
    download_url TEXT NOT NULL, status INTEGER NOT NULL, type TEXT, size INTEGER);
CREATE TABLE download_blobs (normalized_url TEXT NOT NULL PRIMARY KEY, blob BLOB NOT NULL);
CREATE TABLE message_reactions (message_id INTEGER NOT NULL, emoji_id INTEGER, emoji_name TEXT,
    emoji_flags INTEGER NOT NULL, count INTEGER NOT NULL);
CREATE TABLE message_embeds (message_id INTEGER NOT NULL, json TEXT NOT NULL);
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT);
"""

# A real 1x1 PNG, so what comes back out of the vault can be compared byte for byte.
PIXEL = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c63000100000500010d0a2db4000000"
    "0049454e44ae426082"
)
PIXEL_URL = "https://cdn.discordapp.com/attachments/1/2/pixel.png"


def build_dht(path: Path) -> None:
    """A miniature tracker file: two people, three messages, one attachment."""
    src = sqlite3.connect(str(path))
    with src:
        src.executescript(DHT_SCHEMA)
        src.execute("INSERT INTO servers VALUES (900001, 'DM', 'DM')")
        src.execute(
            "INSERT INTO channels VALUES (900002, 900001, 'smoke-dm', NULL, NULL, NULL, NULL)"
        )
        src.executemany(
            "INSERT INTO users VALUES (?, ?, ?, ?, ?)",
            [
                (900003, "smoketester", "Smoke Tester", None, None),
                (900004, "othersmoke", "Other Smoke", None, None),
            ],
        )
        src.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?)",
            [
                (900010, 900003, 900002, "smoketest hello", 1700000000000),
                (900011, 900004, 900002, "smoketest odpověď", 1700000001000),
                (900012, 900003, 900002, "", 1700000002000),
            ],
        )
        src.execute(
            "INSERT INTO attachments VALUES (900020, 'pixel.png', 'image/png', ?, ?, ?, 1, 1)",
            (PIXEL_URL, PIXEL_URL, len(PIXEL)),
        )
        src.execute("INSERT INTO message_attachments VALUES (900012, 900020)")
        src.execute(
            "INSERT INTO download_metadata VALUES (?, ?, 200, 'image/png', ?)",
            (PIXEL_URL, PIXEL_URL, len(PIXEL)),
        )
        src.execute("INSERT INTO download_blobs VALUES (?, ?)", (PIXEL_URL, PIXEL))
        src.execute("INSERT INTO message_reactions VALUES (900010, NULL, 'thumbsup', 0, 1)")
        src.execute('INSERT INTO message_embeds VALUES (900011, \'{"url": "x"}\')')
        src.execute("INSERT INTO metadata VALUES ('version', '1')")
    src.close()


original_db, original_vault = config.DB_PATH, config.VAULT_DIR
try:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        tracker = folder / "smoke.dht"
        build_dht(tracker)
        target = folder / "target.sqlite"
        target.touch()
        config.use(target, folder / "vault")

        found = detect(tracker)
        check("a .dht file is recognised", [s.kind for s in found] == ["discord"], str(found))
        check(
            "its contents are reported before importing",
            (found[0].summary()["threads"], found[0].summary()["messages"]) == (1, 3),
            str(found[0].summary()),
        )
        check("picking the folder finds it too", [s.kind for s in detect(folder)] == ["discord"])
        check("the connected archive is not an import", refused(lambda: detect(target)))
        decoy = folder / "notes.txt"
        decoy.write_text("not a database", encoding="utf-8")
        check("a file that is not a tracker is refused", refused(lambda: detect(decoy)))

        first = dict(ingest_path(tracker))["discord"]
        check("every message is imported", first.new_msgs == 3, f"{first.new_msgs} new")
        check("the embedded attachment lands in the vault", first.new_media == 1,
              f"{first.new_media} stored")

        dest = sqlite3.connect(str(target))
        dest.row_factory = sqlite3.Row
        one = lambda sql: dest.execute(sql).fetchone()[0]  # noqa: E731
        check("imported rows are labelled as Discord",
              one("SELECT COUNT(*) FROM messages WHERE platform = 'discord'") == 3)
        check("the child tables come along",
              (one("SELECT COUNT(*) FROM message_reactions"),
               one("SELECT COUNT(*) FROM message_embeds"),
               one("SELECT COUNT(*) FROM message_attachments")) == (1, 1, 1))
        check("the triggers build the full-text index as it goes",
              one("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'smoketest'") == 2)
        check("diacritics fold on imported text too",
              one("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'odpoved'") == 1)
        stored = dest.execute(
            "SELECT sha256, local_path FROM attachments WHERE attachment_id = 900020"
        ).fetchone()
        check("the attachment row points at its bytes",
              bool(stored["sha256"] and stored["local_path"]))
        check("and the bytes in the vault are the ones from the file",
              (config.VAULT_DIR / stored["local_path"]).read_bytes() == PIXEL)
        dest.close()

        vault_before = sorted(f.name for f in config.VAULT_DIR.rglob("*"))
        second = dict(ingest_path(tracker))["discord"]
        check("re-importing the same file adds nothing",
              (second.new_msgs, second.new_media) == (0, 0),
              f"{second.new_msgs} messages, {second.new_media} media")
        check("and it says why", second.dup_msgs == 3, f"{second.dup_msgs} duplicates")
        check("the vault is untouched",
              sorted(f.name for f in config.VAULT_DIR.rglob("*")) == vault_before)

        dest = sqlite3.connect(str(target))
        check("nothing was duplicated",
              dest.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3)
        check("both runs are logged",
              [r[0] for r in dest.execute("SELECT status FROM ingest_log ORDER BY run_id")]
              == ["ok", "ok"])
        dest.close()
finally:
    config.use(original_db, original_vault)

check("the real archive is connected again", config.DB_PATH == original_db, str(config.DB_PATH))


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
        if kind == "instagram":
            check(
                "re-ingest drops Instagram's reaction notices",
                stats_row.skipped_notices > 0,
                f"{stats_row.skipped_notices} ignored",
            )

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

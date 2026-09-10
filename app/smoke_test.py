"""End-to-end checks against a throwaway archive built for the purpose.

    py smoke_test.py [-v]

`fixture.py` writes a Facebook export, an Instagram export, a Messenger
encrypted-chat download and a Discord History Tracker file into a temp folder,
ingests all four, and links the identities into people. Everything below then
runs against *that* archive: ingest is idempotent, ids survive the round trip,
encoding is repaired, and the full-text index stays in step with the messages
table.

Nothing here touches the archive you actually use, and nothing has to be put
back afterwards - the temp folder is deleted at the end. That is what the
`Archive` handle buys: the app is handed an archive, and so is this file.

The Czech dictionary is the one optional part. It is derived data, so a fresh
clone may not have it; the checks that need it are skipped with a note rather
than failing, exactly as search itself degrades.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

import fixture  # noqa: E402
from archive import api, config, czech, picker, query  # noqa: E402
from archive.api import app  # noqa: E402
from archive.archive import Archive  # noqa: E402
from archive.czech import Lexicon  # noqa: E402
from archive.ids import demojibake, media_type, message_source_key, synth_id  # noqa: E402
from archive.ingest import runner, secure  # noqa: E402
from archive.ingest.detect import detect  # noqa: E402
from archive.noise import is_reaction_notice  # noqa: E402

CZECH_LETTERS = "áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ"
VERBOSE = "-v" in sys.argv

failures: list[str] = []
skipped: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{f' - {detail}' if detail else ''}")
    if not condition:
        failures.append(label)


def skip(label: str, why: str) -> None:
    print(f"  SKIP  {label} - {why}")
    skipped.append(label)


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


# ------------------------------------------------------------------- setup
print("building a fixture archive...")
fx = fixture.build(verbose=VERBOSE)
# The "connect a database" checks remember their choice; aim that at the
# fixture so this machine's own connection is never written to.
fixture.isolate_settings(fx.root)
api.use(fx.archive)
client = TestClient(app)


def sql(query_text: str, params: tuple = ()):
    """Read straight from the fixture archive, re-opening if it was closed."""
    return fx.archive.read().execute(query_text, params)


def one(query_text: str, params: tuple = ()):
    return sql(query_text, params).fetchone()[0]


# ---------------------------------------------------------------- database
section("database")
total = one("SELECT COUNT(*) FROM messages")
check("the fixture archive was built", total > 200, f"{total} messages")
check(
    "full-text index covers every message",
    total == one("SELECT COUNT(*) FROM messages_fts"),
    f"{total} messages, {one('SELECT COUNT(*) FROM messages_fts')} indexed",
)

by_platform = dict(sql("SELECT platform, COUNT(*) FROM messages GROUP BY platform"))
check("all three platforms present", set(by_platform) == {"discord", "facebook", "instagram"},
      json.dumps(by_platform))
check("the encrypted chats landed under facebook, not a fourth platform",
      by_platform["facebook"] > 150, str(by_platform["facebook"]))

check(
    "Meta ids are negative, Discord ids positive",
    one("SELECT COUNT(*) FROM messages WHERE (platform = 'discord') != (message_id > 0)") == 0,
)
check(
    "no mojibake left in message text",
    one(
        "SELECT COUNT(*) FROM messages WHERE platform <> 'discord' "
        "AND (text LIKE '%Ã%' OR text LIKE '%Å¾%' OR text LIKE '%Ä%')"
    ) == 0,
)
check(
    "the repaired text is exactly what was written",
    one("SELECT COUNT(*) FROM messages WHERE text = 'ahoj, jak se máš?'") > 0,
)
check(
    "every Meta message has a dedup key",
    one("SELECT COUNT(*) FROM messages WHERE platform <> 'discord' AND source_key IS NULL") == 0,
)
check(
    "source keys are unique",
    one("SELECT COUNT(*) FROM (SELECT source_key FROM messages "
        "WHERE source_key IS NOT NULL GROUP BY source_key HAVING COUNT(*) > 1)") == 0,
)
orphans = one(
    "SELECT COUNT(*) FROM messages m LEFT JOIN users u ON u.id = m.sender_id WHERE u.id IS NULL"
)
check("every message has a sender", orphans == 0, f"{orphans} orphans")
check(
    "an empty conversation is not given a thread of its own",
    one("SELECT COUNT(*) FROM channels WHERE topic = 'secure/Nikdo_1'") == 0,
)
check(
    "an unsent message is marked, not invented",
    one("SELECT COUNT(*) FROM messages WHERE is_unsent = 1") == 1,
)

# ------------------------------------------------------------------- vault
section("media vault")
stored = sql(
    "SELECT sha256, local_path FROM attachments WHERE local_path IS NOT NULL"
).fetchall()
missing = [row["local_path"] for row in stored if not fx.archive.vault.exists(row["local_path"])]
check("every stored attachment exists on disk", not missing, f"{len(missing)} missing")
check("the vault holds one file per platform", len(stored) == 4, f"{len(stored)} attachments")
check(
    "identical bytes are stored once",
    len({row["sha256"] for row in stored}) == 1,
    "four references, one file",
)
check(
    "a file Meta could not export is kept as an unavailable attachment",
    one("SELECT COUNT(*) FROM attachments WHERE sha256 IS NULL") == 1,
)
check(
    "and the bytes in the vault are the ones that went in",
    fx.archive.vault.abspath(stored[0]["local_path"]).read_bytes() == fixture.PIXEL,
)

# --------------------------------------------------------------------- ids
section("id synthesis")
check("synthetic ids are always negative", all(synth_id("x", i) < 0 for i in range(2000)))
check(
    "synthetic ids are deterministic",
    synth_id("instagram", "inbox/foo") == synth_id("instagram", "inbox/foo"),
)
check("demojibake repairs Meta text", demojibake('DobrÃ¡ zprÃ¡va, mÅ¯Å¾eÅ¡') == 'Dobrá zpráva, můžeš')
check("demojibake leaves clean text alone", demojibake("už čeština") == "už čeština")
check("the fixture writes real mojibake", fixture.mojibake("máš") == "mÃ¡Å¡")
check(
    "source keys separate identical text at different times",
    message_source_key("instagram", "t", {"sender_name": "A", "timestamp_ms": 1, "content": "x"})
    != message_source_key("instagram", "t", {"sender_name": "A", "timestamp_ms": 2, "content": "x"}),
)
check(
    "media types do not depend on the machine's registry",
    (media_type("a.webp"), media_type("a.jpeg"), media_type("a.mp4"))
    == ("image/webp", "image/jpeg", "video/mp4"),
)

# ------------------------------------------------- messenger encrypted chats
section("Messenger encrypted-chat export")
SECURE_THREAD = {
    "participants": ["Me", "Jana Nováková"],
    "threadName": "Jana Nováková_7",
    "messages": [
        {"isUnsent": False, "media": [], "reactions": [{"actor": "Me", "reaction": "❤"}],
         "senderName": "Jana Nováková", "text": "ahoj", "timestamp": 1700000000000, "type": "text"},
        {"isUnsent": False, "media": [{"uri": "./media/a.webp"}], "reactions": [],
         "senderName": "Me", "text": "", "timestamp": 1700000000001, "type": "media"},
        {"isUnsent": True, "media": [], "reactions": [], "senderName": "Me",
         "text": "User unsent a message", "timestamp": 1700000000002, "type": "placeholder"},
    ],
}
normalized = secure.normalize_thread(SECURE_THREAD, "Jana Nováková_7")
check("the index suffix is dropped from the title", normalized["title"] == "Jana Nováková")
check("thread_path is unique per file", normalized["thread_path"] == "secure/Jana Nováková_7")
check("participants become Meta-shaped",
      normalized["participants"] == [{"name": "Me"}, {"name": "Jana Nováková"}])
check("text, sender and timestamp are renamed",
      (normalized["messages"][0]["content"], normalized["messages"][0]["sender_name"],
       normalized["messages"][0]["timestamp_ms"]) == ("ahoj", "Jana Nováková", 1700000000000))
check("reactions pass through unchanged",
      normalized["messages"][0]["reactions"] == [{"actor": "Me", "reaction": "❤"}])
check("media lands in the bucket its type implies",
      normalized["messages"][1]["photos"] == [{"uri": "./media/a.webp"}])
check("an unsent message carries no content",
      normalized["messages"][2].get("is_unsent") is True
      and "content" not in normalized["messages"][2])
check("the shape is recognised", secure.looks_like_thread(SECURE_THREAD))
check("a DYI settings file is not", not secure.looks_like_thread({"media": [], "label_values": []}))

found = [s for s in detect(fx.exports["messenger"]) if getattr(s, "layout", None) == "secure"]
check("the messages folder is detected", len(found) == 1)
source = found[0]
check("it is ingested as facebook", source.platform == "facebook", source.kind)
check("media resolves beside it, not inside it",
      source.media_root == fx.exports["messenger"].parent)
check("encrypted chats reached the archive",
      one("SELECT COUNT(*) FROM channels WHERE topic LIKE 'secure/%'") == 1)
check("they carry the facebook platform tag",
      one("""SELECT COUNT(*) FROM messages m JOIN channels c ON c.id = m.channel_id
             WHERE c.topic LIKE 'secure/%' AND m.platform <> 'facebook'""") == 0)
check(
    "a counterpart known from a group chat stays one identity",
    one("SELECT COUNT(*) FROM users WHERE platform = 'facebook' AND name = ?", (fixture.JANA,)) == 1,
)
check(
    "a DYI export's own messages folder is not mistaken for one",
    not [s for s in detect(fx.exports["facebook"]) if getattr(s, "layout", None) == "secure"],
)

# --------------------------------------------------------------------- api
section("api endpoints")
stats = client.get("/api/stats").json()
check("GET /api/stats", stats["total"] == total, f"{stats['total']}")
check("it names the archive it is serving", stats["db_path"] == str(fx.archive.path))

threads = client.get("/api/threads").json()
check("GET /api/threads", len(threads) == 6, f"{len(threads)} threads")
check("a conversation with nothing in it is not listed",
      all(int(t["messages"]) > 0 for t in threads))
check("thread ids are strings", all(isinstance(t["id"], str) for t in threads))
check("no id lost precision", all(str(int(t["id"])) == t["id"] for t in threads))

biggest = max(threads, key=lambda t: int(t["messages"]))
detail = client.get(f"/api/threads/{biggest['id']}").json()
check("GET /api/threads/{id}", detail["messages"] == int(biggest["messages"]))
check("month histogram present", len(detail["months"]) > 0, f"{len(detail['months'])} months")
check("a missing thread is 404", client.get("/api/threads/12345").status_code == 404)

page = client.get(f"/api/threads/{biggest['id']}/messages?limit=50").json()
check("GET messages (latest page)", len(page["messages"]) == 50)
check("messages ascend by time",
      all(a["timestamp"] <= b["timestamp"] for a, b in zip(page["messages"], page["messages"][1:])))

older = client.get(f"/api/threads/{biggest['id']}/messages?before={page['oldest']}&limit=50").json()
check("keyset paging backwards", len(older["messages"]) == 50)
check(
    "pages do not overlap",
    not ({m["message_id"] for m in older["messages"]} & {m["message_id"] for m in page["messages"]}),
)

first_month = next(m for m in detail["months"] if m["channel_id"] == biggest["id"])
jump = client.get(
    f"/api/threads/{biggest['id']}/messages?ts={first_month['first_ts']}&limit=40"
).json()
check("jump to a month", len(jump["messages"]) > 0, f"{len(jump['messages'])} messages")
check(
    "the month jump lands in that month",
    jump["messages"][0]["timestamp"] <= first_month["first_ts"] <= jump["messages"][-1]["timestamp"]
    or jump["messages"][0]["timestamp"] == first_month["first_ts"],
)
check("attachments come with their messages",
      any(m["attachments"] for m in client.get(
          f"/api/threads/{biggest['id']}/messages?limit=200").json()["messages"]))

section("one person, many chats")
check("months are split by author",
      all(m["mine"] + m["theirs"] == m["messages"] for m in detail["months"]))
check(
    "months carry the chat they belong to",
    {m["channel_id"] for m in detail["months"]} <= {t["id"] for t in detail["group"]["threads"]},
)
check("the thread is in its own group",
      biggest["id"] in {t["id"] for t in detail["group"]["threads"]})
check(
    "one person's chats on every platform are one group",
    len(detail["group"]["threads"]) == 4,
    f"{len(detail['group']['threads'])} chats under {detail['group']['person']}",
)
check(
    "the group spans all three platforms",
    {t["platform"] for t in detail["group"]["threads"]} == {"discord", "facebook", "instagram"},
)
grouped = [t for t in threads if t["person_id"] is not None]
check(
    "a mapped counterpart files the chat under a person",
    all(t["person"] for t in grouped),
    f"{len({t['person_id'] for t in grouped})} people over {len(grouped)} chats",
)
alone = next(t for t in threads if t["person_id"] is None)
check(
    "a chat with no mapped counterpart stands alone",
    len(client.get(f"/api/threads/{alone['id']}").json()["group"]["threads"]) == 1,
    alone["name"],
)
check(
    "a group chat has no single counterpart",
    client.get(f"/api/threads/{alone['id']}").json()["group"]["person_id"] is None,
)

section("search")
search = client.get("/api/search?q=necekal").json()
plain = client.get("/api/search?q=nečekal").json()
check("diacritics-insensitive search", search["total"] == plain["total"] > 0,
      f"{search['total']} hits both ways")
check("search spans platforms",
      len({h["platform"] for h in search["hits"]}) > 1,
      str({h["platform"] for h in search["hits"]}))
check("hits carry a highlighted snippet", "<mark>" in search["hits"][0]["snippet"])
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

lexicon = Lexicon.open(config.lexicon_path())
if lexicon is None:
    skip("the Czech dictionary widens a search", "not built - run: py -m archive czech-dict")
else:
    check("the archive found the dictionary", fx.archive.lexicon is not None,
          str(config.lexicon_path()))

    def expand(word):
        result = lexicon.expand(word)
        return set(result.forms) if result else set()

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
    check("searching one polarity never returns the other",
          client.get("/api/search?q=necekal").json()["total"]
          + client.get("/api/search?q=cekal").json()["total"]
          == client.get("/api/search?q=necekal OR cekal").json()["total"])

section("the search query language")


def hits_for(q):
    return client.get("/api/search", params={"q": q}).json()["total"]


beer, pub = hits_for("pivo"), hits_for("hospoda")
check("both words are in the archive to begin with", beer > 0 and pub > 0, f"{beer} / {pub}")
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
check("jumping to a message that is not there is 404",
      client.get(f"/api/threads/{biggest['id']}/messages?at=1").status_code == 404)

# ------------------------------------------------------- monthly statistics
section("monthly statistics")

scope = ",".join(t["id"] for t in detail["group"]["threads"])
timeline = client.get("/api/stats/months", params={"threads": scope}).json()
check("GET /api/stats/months", len(timeline["months"]) > 0, f"{len(timeline['months'])} months")
check("monthly totals split by author",
      all(m["mine"] + m["theirs"] == m["messages"] for m in timeline["months"]))
check("months come back in order",
      [m["month"] for m in timeline["months"]] == sorted(m["month"] for m in timeline["months"]))
check(
    "the timeline totals the thread group",
    timeline["total"] == sum(int(t["messages"]) for t in threads if t["id"] in scope.split(",")),
    f"{timeline['total']} messages",
)

counted = client.get("/api/stats/months", params={"threads": scope, "q": "hospoda"}).json()
scoped_hits = client.get("/api/search", params={"q": "hospoda", "threads": scope}).json()
check(
    "counting a word agrees with searching for it",
    counted["total"] == scoped_hits["total"],
    f"{counted['total']} counted vs {scoped_hits['total']} found",
)
if lexicon is not None:
    check("a counted word is widened like a search",
          bool(counted["terms"]) and counted["terms"][0]["forms"] > 1)
check("a word never outnumbers the messages carrying it", counted["total"] <= timeline["total"])
check(
    "counted months are a subset of the whole history",
    {m["month"] for m in counted["months"]} <= {m["month"] for m in timeline["months"]},
)
check("a word nobody said is an empty timeline",
      client.get("/api/stats/months", params={"threads": scope, "q": "zzzqx"}).json()["total"] == 0)
check("a broken query is refused politely",
      client.get("/api/stats/months", params={"threads": scope, "q": "("}).status_code == 400)
check("an unparseable scope is refused politely",
      client.get("/api/stats/months", params={"threads": "nonsense"}).status_code == 400)
check("an empty scope is refused politely",
      client.get("/api/stats/months", params={"threads": ""}).status_code == 400)

section("reaction notices and search normalisation")
# A notice is only ever dropped when it carries nothing else, so the ones left
# behind must all have an attachment, an embed or a reaction of their own.
notices = [
    row["message_id"]
    for row in sql(
        """
        SELECT message_id, text FROM messages m
        WHERE (text LIKE '%to your message%' OR text LIKE '%liked a message%')
          AND NOT EXISTS(SELECT 1 FROM message_attachments WHERE message_id = m.message_id)
          AND NOT EXISTS(SELECT 1 FROM message_embeds      WHERE message_id = m.message_id)
          AND NOT EXISTS(SELECT 1 FROM message_reactions   WHERE message_id = m.message_id)
        """
    )
    if is_reaction_notice(row["text"])
]
check("no empty reaction pseudo-messages left in the archive", not notices, f"{len(notices)} found")
check(
    "but a notice carrying something of its own is kept",
    one("SELECT COUNT(*) FROM messages WHERE text = 'Liked a message'") == 1,
)
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
scoped = client.get("/api/search?q=pivo&threads=" + ",".join(group_ids)).json()
check(
    "in-conversation search stays inside the group",
    all(h["channel_id"] in group_ids for h in scoped["hits"]) and scoped["total"] > 0,
    f"{scoped['total']} hits across {len(group_ids)} chat(s)",
)
check("an empty thread filter finds nothing",
      client.get("/api/search?q=pivo&threads=").json()["total"] == 0)

sha = stored[0]["sha256"]
media = client.get(f"/api/media/{sha}")
check("GET /api/media/{sha256}", media.status_code == 200, f"{len(media.content)} bytes")
check("the bytes come back unchanged", media.content == fixture.PIXEL)
check("bad media hash rejected", client.get("/api/media/nope").status_code == 400)
check("unknown media hash is 404", client.get(f"/api/media/{'0' * 64}").status_code == 404)

people = client.get("/api/people").json()
check("GET /api/people", "identities" in people, f"{len(people['identities'])} identities")
check("someone is marked as self", any(p["is_self"] for p in people["people"]))
check("one person can hold three identities",
      any(p["identities"] == 3 for p in people["people"]))

check("GET /api/ingest/history", "ingest" in client.get("/api/ingest/history").json())
check("every ingest run is logged as ok",
      {r["status"] for r in client.get("/api/ingest/history").json()["ingest"]} == {"ok"})

inspect = client.post("/api/ingest/inspect", json={"path": str(fx.root)}).json()
check("POST /api/ingest/inspect finds every export in the root",
      {s["label"] for s in inspect["sources"]}
      == {"Facebook", "Instagram", "Messenger (encrypted chats)"},
      str([s["label"] for s in inspect["sources"]]))
check("it counts what it found before importing",
      all(s["messages"] > 0 and s["threads"] > 0 for s in inspect["sources"]))
nothing = fx.root / "nothing"
nothing.mkdir(exist_ok=True)
check(
    "inspect reports a useful error for a non-export folder",
    "error" in client.post("/api/ingest/inspect", json={"path": str(nothing)}).json(),
)
check("inspect requires a path", client.post("/api/ingest/inspect", json={}).status_code == 400)
check(
    "the connected archive is not offered as an import",
    "error" in client.post(
        "/api/ingest/inspect", json={"path": str(fx.archive.path)}
    ).json(),
)

if config.WEB_BUILD.is_dir():
    check("SPA deep link serves the app", client.get("/search").status_code == 200)
else:
    skip("SPA deep link serves the app", "web UI not built - run: cd web && npm run build")

# ---------------------------------------------------------- people editing
section("people editing")
NAME = "SMOKE Zkouška ěščř"
RENAMED = NAME + " 2"
before_people = client.get("/api/people").json()
was_self = [p["person_id"] for p in before_people["people"] if p["is_self"]]
victim = next(r for r in before_people["identities"] if r["person_id"] is None and r["messages"] > 0)

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
check("deleting a person only unlinks - no identity is lost",
      len(final["identities"]) == len(before_people["identities"]))
check("the archive is back where it started",
      len(final["people"]) == len(before_people["people"])
      and linked_row(final)["person_id"] is None)
for pid in was_self:
    client.patch(f"/api/people/{pid}", json={"is_self": True})
check("the original self flag is restored",
      [p["person_id"] for p in client.get("/api/people").json()["people"] if p["is_self"]] == was_self)

# ------------------------------------------------------ pluggable database
section("pluggable database")
# The archive is not part of the repository, so a fresh checkout has nothing to
# read. Prove the app can be handed a database - including one it just made.
status = client.get("/api/db").json()
check("GET /api/db reports the connection", status["connected"] is True, status["path"])
check("it counts what is inside", status["messages"] == total, str(status.get("messages")))

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
    check("connecting to something that is not a database is refused",
          client.post("/api/db/connect", json={"path": str(Path(tmp))}).status_code == 400)

    back = client.post("/api/db/connect", json={"path": str(fx.archive.path)})
    check("reconnecting to the fixture archive works", back.status_code == 200, back.text)
    check("all the messages are back", back.json()["messages"] == total, back.text)

check("the choice was remembered", config.load_settings().get("db_path") == str(fx.archive.path))
check("and it was remembered in the fixture's settings, not this machine's",
      config.SETTINGS_FILE.parent == fx.root)

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
# A tracker file is a SQLite database of its own, so importing it is a copy.
# The fixture already built one; import it into a *second* archive here, twice,
# where the counts can be exact.
section("discord .dht import (a second archive, imported into twice)")
tracker = fx.exports["discord"]

found = detect(tracker)
check("a .dht file is recognised", [s.kind for s in found] == ["discord"], str(found))
check(
    "its contents are reported before importing",
    (found[0].summary()["threads"], found[0].summary()["messages"]) == (1, 3),
    str(found[0].summary()),
)
check("picking the folder finds it too", [s.kind for s in detect(tracker.parent)] == ["discord"])
check("the connected archive is not an import",
      refused(lambda: detect(fx.archive.path, connected=fx.archive.path)))
decoy = fx.root / "notes.txt"
decoy.write_text("not a database", encoding="utf-8")
check("a file that is not a tracker is refused", refused(lambda: detect(decoy)))

with tempfile.TemporaryDirectory() as tmp:
    second = Archive.create(Path(tmp) / "target.sqlite", Path(tmp) / "vault", verbose=False)
    check("a second archive can be open at the same time", second.path != fx.archive.path)

    first = dict(runner.ingest_path(second, tracker))["discord"]
    check("every message is imported", first.new_msgs == 3, f"{first.new_msgs} new")
    check("the embedded attachment lands in the vault", first.new_media == 1,
          f"{first.new_media} stored")

    two = second.read()
    at = lambda q: two.execute(q).fetchone()[0]  # noqa: E731
    check("imported rows are labelled as Discord",
          at("SELECT COUNT(*) FROM messages WHERE platform = 'discord'") == 3)
    check("the child tables come along",
          (at("SELECT COUNT(*) FROM message_reactions"),
           at("SELECT COUNT(*) FROM message_embeds"),
           at("SELECT COUNT(*) FROM message_attachments")) == (1, 1, 1))
    check("the triggers build the full-text index as it goes",
          at("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'hospoda'") == 1)
    check("diacritics fold on imported text too",
          at("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'necekal'") == 1)
    blob = two.execute(
        "SELECT sha256, local_path FROM attachments WHERE attachment_id = 900020"
    ).fetchone()
    check("the attachment row points at its bytes", bool(blob["sha256"] and blob["local_path"]))
    check("and the bytes in the vault are the ones from the file",
          second.vault.abspath(blob["local_path"]).read_bytes() == fixture.PIXEL)

    vault_before = sorted(f.name for f in second.vault.root.rglob("*"))
    again = dict(runner.ingest_path(second, tracker))["discord"]
    check("re-importing the same file adds nothing",
          (again.new_msgs, again.new_media) == (0, 0),
          f"{again.new_msgs} messages, {again.new_media} media")
    check("and it says why", again.dup_msgs == 3, f"{again.dup_msgs} duplicates")
    check("the vault is untouched",
          sorted(f.name for f in second.vault.root.rglob("*")) == vault_before)
    check("nothing was duplicated", second.read().execute(
        "SELECT COUNT(*) FROM messages").fetchone()[0] == 3)
    check("both runs are logged",
          [r[0] for r in second.read().execute(
              "SELECT status FROM ingest_log ORDER BY run_id")] == ["ok", "ok"])
    second.close()

check("the fixture archive was not touched by any of that",
      one("SELECT COUNT(*) FROM messages") == total)

# `py -m archive discord-media` sweeps the whole archive rather than one import.
# It makes no network calls: the only Discord attachments that survive are the
# ones the tracker embedded, and those are already in the vault by now.
recovered = runner.ingest_discord_media(fx.archive)
check("discord-media finds the embedded blob", recovered.media_seen == 1,
      f"{recovered.media_seen} seen")
check("and re-storing it is a no-op", recovered.new_media == 0 and recovered.dup_media == 1)
check("it adds no messages and loses none", one("SELECT COUNT(*) FROM messages") == total)

# --------------------------------------------------------------- idempotency
section("ingest idempotency (re-running must change nothing)")
before_counts = (
    total,
    one("SELECT COUNT(*) FROM attachments"),
    len(list(fx.archive.vault.root.rglob("*"))),
)

for kind, stats_row in runner.ingest_path(fx.archive, fx.root):
    check(f"re-ingest {kind}: no new messages", stats_row.new_msgs == 0,
          f"{stats_row.dup_msgs} duplicates skipped")
    check(f"re-ingest {kind}: no new media", stats_row.new_media == 0)
    if kind != "messenger":
        # Meta writes the literal "Failed to download media" in place of a URI
        # for media it could not export; those stay unresolvable.
        check(f"re-ingest {kind}: nothing missing", stats_row.missing_media == 0)
    if kind == "instagram":
        check("re-ingest drops Instagram's reaction notices", stats_row.skipped_notices == 3,
              f"{stats_row.skipped_notices} ignored")

for kind, stats_row in runner.ingest_path(fx.archive, fx.exports["discord"]):
    check("re-ingest discord: no new messages", stats_row.new_msgs == 0)

after_counts = (
    one("SELECT COUNT(*) FROM messages"),
    one("SELECT COUNT(*) FROM attachments"),
    len(list(fx.archive.vault.root.rglob("*"))),
)
check("row and file counts unchanged", before_counts == after_counts,
      f"{before_counts} -> {after_counts}")
check("full-text index still in step", one("SELECT COUNT(*) FROM messages_fts") == after_counts[0])

# ------------------------------------------------------------------- done
fx.close()

print()
if skipped:
    print(f"{len(skipped)} skipped: {', '.join(skipped)}")
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print(f"all checks passed ({fx.root.name} cleaned up)")

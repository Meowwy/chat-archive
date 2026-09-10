"""A whole archive, built from nothing, for `smoke_test.py` to run against.

The checks used to read this author's own archive, so nobody else could run
them. Everything they assert is a property of the *code*, not of those
particular messages - so this module writes the exports instead: a Facebook and
an Instagram Download Your Information folder, a Messenger encrypted-chat
download, and a Discord History Tracker file. Then it ingests all four the way
the app does and links the identities into people.

What comes back is an `Archive` in a temp directory. That is the whole point of
the handle: the app is handed one, and so is the test, and neither knows the
difference.

Everything the checks lean on is deliberate and named below:

    mojibake            Meta's latin-1/UTF-8 double encoding, for demojibake()
    Czech text          inflected forms, negation, and diacritics, for search
    reaction notices    Instagram's "Reacted 👍 to your message" pseudo-messages
    a failed media URI  the literal string Meta writes when it cannot export one
    one person, three   Jana is a Facebook, an Instagram and a Discord identity
    an unmapped person  Petr, so the People page has something to link
    a group chat        three participants, so it has no single counterpart
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from archive import config
from archive.archive import Archive
from archive.ids import synth_id
from archive.ingest import people as people_mod
from archive.ingest import runner

# A real 1x1 PNG, so bytes that come back out of the vault can be compared.
PIXEL = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c63000100000500010d0a2db4000000"
    "0049454e44ae426082"
)

ME = "Me"
JANA = "Jana Nováková"
JANA_IG = "jana.novakova"
PETR = "Petr Svoboda"

# Discord ids come from the tracker file, so they are ordinary snowflake-shaped
# positive integers rather than anything this package mints.
DISCORD_ME = 900003
DISCORD_JANA = 900004

DAY = 86_400_000
MONTH = 30 * DAY
START = 1_600_000_000_000  # Sep 2020, so the timeline spans several years

# Sentences the search checks are asked about. Between them they carry: two
# inflections of one lemma (hospoda/hospody), a negation pair (čekal/nečekal),
# a word that appears without the other (pivo alone), and an apostrophe.
CZECH = [
    "ahoj, jak se máš?",
    "byli jsme v hospodě a bylo to fajn",
    "dáme pivo?",
    "hospoda byla zavřená",
    "nečekal jsem to, ale bylo to hezké",
    "čekal jsem na tebe u hospody",
    "pivo bylo studené a dobré",
    "to je hospoda, kterou znám",
    "dont worry, uz to mam",
    "a co ty, uz jsi doma?",
    "zítra jdeme do hospody",
    "víno nebo pivo?",
]


def mojibake(text: str) -> str:
    """Write `text` the way Meta's DYI export writes it: UTF-8 read as latin-1."""
    return text.encode("utf-8").decode("latin-1")


# ------------------------------------------------------------------ writing


def _message(sender: str, at: int, content: str | None = None, **extra) -> dict:
    """One message in Download Your Information shape, mojibake included."""
    message: dict = {"sender_name": mojibake(sender), "timestamp_ms": at}
    if content is not None:
        message["content"] = mojibake(content)
    message.update(extra)
    return message


def _conversation(names: tuple[str, str], start: int, count: int, step: int) -> list[dict]:
    """`count` messages alternating between two people, spread over time."""
    return [
        _message(names[i % 2], start + i * step, CZECH[i % len(CZECH)])
        for i in range(count)
    ]


def _dyi_thread(
    marker: Path,
    folder: str,
    title: str,
    participants: list[str],
    messages: list[dict],
    image: str | None = None,
) -> None:
    """Write one `messages/inbox/<thread>/message_1.json`."""
    thread = marker / "messages" / "inbox" / folder
    thread.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "title": mojibake(title),
        "thread_path": f"inbox/{folder}",
        "participants": [{"name": mojibake(name)} for name in participants],
        "messages": messages,
    }
    if image:
        payload["image"] = {"uri": image}
    (thread / "message_1.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _photo(marker: Path, folder: str, name: str) -> str:
    """Put a real image where a DYI export would, and return its URI."""
    photos = marker / "messages" / "inbox" / folder / "photos"
    photos.mkdir(parents=True, exist_ok=True)
    (photos / name).write_bytes(PIXEL)
    # URIs are relative to the marker folder's parent, and are prefixed with it.
    return f"{marker.name}/messages/inbox/{folder}/photos/{name}"


def _facebook(root: Path) -> Path:
    """A Download Your Information export: a busy DM, a group chat, a stranger."""
    marker = root / "your_facebook_activity"

    # The DYI export ships a settings file in the same `messages/` folder an
    # encrypted-chat download uses for conversations. detect() must not confuse
    # the two - this is what makes that check mean something.
    (marker / "messages").mkdir(parents=True, exist_ok=True)
    (marker / "messages" / "autofill_information.json").write_text(
        json.dumps({"media": [], "label_values": []}), encoding="utf-8"
    )

    photo = _photo(marker, "jana_1", "pixel.png")
    messages = _conversation((ME, JANA), START, 130, 4 * DAY)
    # A photo, a reaction, and a shared link, so every child table is exercised.
    messages[3] = _message(JANA, START + 3 * 4 * DAY, "", photos=[{"uri": photo}])
    messages[5]["reactions"] = [
        {"reaction": mojibake("😂"), "actor": mojibake(ME)},
        {"reaction": mojibake("❤"), "actor": mojibake(JANA)},
    ]
    messages[7] = _message(
        ME,
        START + 7 * 4 * DAY,
        "koukni na tohle",
        share={"link": "https://example.com/a", "share_text": mojibake("hospoda roku")},
    )
    _dyi_thread(marker, "jana_1", JANA, [ME, JANA], messages)

    _dyi_thread(
        marker,
        "parta_2",
        "Parta",
        [ME, JANA, PETR],
        _conversation((JANA, PETR), START + 6 * MONTH, 20, 3 * DAY),
    )
    _dyi_thread(
        marker,
        "petr_3",
        PETR,
        [ME, PETR],
        _conversation((ME, PETR), START + MONTH, 12, 5 * DAY),
    )

    # Meta ships a file for every conversation ever opened, empty ones included.
    _dyi_thread(marker, "prazdny_4", "Prázdný", [ME, "Nobody"], [])
    return marker


def _instagram(root: Path) -> Path:
    """An Instagram export, with the reaction pseudo-messages it really contains."""
    marker = root / "your_instagram_activity"
    photo = _photo(marker, "jana_ig_1", "pixel.png")

    messages = _conversation((ME, JANA_IG), START + 3 * MONTH, 40, 9 * DAY)
    messages[2] = _message(JANA_IG, START + 3 * MONTH + 2 * 9 * DAY, "", photos=[{"uri": photo}])
    # Instagram writes a reaction into the thread twice - once on the message it
    # belongs to, once as a standalone message. These are dropped at ingest.
    for i, notice in enumerate(
        [f"{ME} reacted 👍 to your message", "Liked a message", f"{JANA_IG} reacted 😂 to your message"]
    ):
        messages.append(_message(JANA_IG, START + 4 * MONTH + i * DAY, notice))
    # ...but never when the notice carries something of its own.
    messages.append(
        _message(
            JANA_IG,
            START + 4 * MONTH + 5 * DAY,
            "Liked a message",
            photos=[{"uri": photo}],
        )
    )
    _dyi_thread(marker, "jana_ig_1", JANA_IG, [ME, JANA_IG], messages)
    return marker


def _messenger(root: Path) -> Path:
    """An encrypted-chat download: a flat messages/ folder beside media/."""
    folder = root / "messages"
    folder.mkdir(parents=True, exist_ok=True)
    media = root / "media"
    media.mkdir(parents=True, exist_ok=True)
    (media / "e2ee.jpeg").write_bytes(PIXEL)

    # This download is clean UTF-8 and uses its own names for everything.
    thread = {
        "threadName": f"{JANA}_7",
        "participants": [ME, JANA],
        "messages": [
            {
                "senderName": JANA,
                "timestamp": START + 20 * MONTH,
                "text": "tajná zpráva, hospoda v pět",
                "media": [],
                "reactions": [{"actor": ME, "reaction": "❤"}],
                "isUnsent": False,
                "type": "text",
            },
            {
                "senderName": ME,
                "timestamp": START + 20 * MONTH + DAY,
                "text": "",
                "media": [{"uri": "./media/e2ee.jpeg"}],
                "reactions": [],
                "isUnsent": False,
                "type": "media",
            },
            {
                "senderName": ME,
                "timestamp": START + 20 * MONTH + 2 * DAY,
                "text": "User unsent a message",
                "media": [],
                "reactions": [],
                "isUnsent": True,
                "type": "placeholder",
            },
            {
                # Where Meta could not retrieve an attachment it writes this
                # literal string in place of the URI. The message really did
                # carry one, so it is kept and shows as unavailable.
                "senderName": JANA,
                "timestamp": START + 20 * MONTH + 3 * DAY,
                "text": "",
                "media": [{"uri": "Failed to download media"}],
                "reactions": [],
                "isUnsent": False,
                "type": "media",
            },
        ],
    }
    (folder / f"{JANA}_7.json").write_text(
        json.dumps(thread, ensure_ascii=False), encoding="utf-8"
    )
    # An empty conversation, which is skipped rather than given an identity.
    (folder / "Nikdo_1.json").write_text(
        json.dumps({"threadName": "Nikdo_1", "participants": [ME, "Nikdo"], "messages": []}),
        encoding="utf-8",
    )
    return folder


# Discord History Tracker's own schema, without any of the columns this archive
# adds - a real tracker file has exactly these shapes.
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

PIXEL_URL = "https://cdn.discordapp.com/attachments/1/2/pixel.png"

# Three messages, one of them carrying the attachment whose bytes the tracker
# embedded - the only Discord media that survives, since CDN links expire.
DHT_MESSAGES = [
    (900010, DISCORD_ME, "necekal jsem ze to pujde", START + 12 * MONTH),
    (900011, DISCORD_JANA, "hospoda v pet?", START + 12 * MONTH + DAY),
    (900012, DISCORD_ME, "", START + 12 * MONTH + 2 * DAY),
]


def build_dht(path: Path) -> None:
    """A miniature tracker file: two people, three messages, one attachment."""
    src = sqlite3.connect(str(path))
    with src:
        src.executescript(DHT_SCHEMA)
        src.execute("INSERT INTO servers VALUES (900001, 'DM', 'DM')")
        src.execute(
            "INSERT INTO channels VALUES (900002, 900001, 'jana-dm', NULL, NULL, NULL, NULL)"
        )
        src.executemany(
            "INSERT INTO users VALUES (?, ?, ?, ?, ?)",
            [
                (DISCORD_ME, "smoketester", "Smoke Tester", None, None),
                (DISCORD_JANA, "janan", "Jana N", None, None),
            ],
        )
        src.executemany("INSERT INTO messages VALUES (?, ?, 900002, ?, ?)", DHT_MESSAGES)
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


# ------------------------------------------------------------------- people


def _link_people(archive: Archive) -> dict[str, int]:
    """Tie the identities together the way the People page would.

    Jana is one person with three identities on three platforms; Petr is left
    unmapped, which is what an archive looks like before anyone has tidied it.
    """
    with archive.write() as con:
        me = people_mod.create(
            con,
            display="Me",
            is_self=True,
            user_ids=[
                synth_id("facebook", "user", ME),
                synth_id("instagram", "user", ME),
                DISCORD_ME,
            ],
        )
        jana = people_mod.create(
            con,
            display="Jana",
            user_ids=[
                synth_id("facebook", "user", JANA),
                synth_id("instagram", "user", JANA_IG),
                DISCORD_JANA,
            ],
        )
    return {"me": me, "jana": jana}


# -------------------------------------------------------------------- build


@dataclass
class Fixture:
    """A temp directory holding the exports, and the archive built from them."""

    root: Path
    archive: Archive
    exports: dict[str, Path]
    people: dict[str, int]

    def close(self) -> None:
        self.archive.close()
        shutil.rmtree(self.root, ignore_errors=True)


def build(root: Path | None = None, *, verbose: bool = False) -> Fixture:
    """Write the exports, ingest them, link the people. Returns the archive."""
    root = Path(root or tempfile.mkdtemp(prefix="chat-archive-fixture-"))
    root.mkdir(parents=True, exist_ok=True)

    exports = {
        "facebook": _facebook(root),
        "instagram": _instagram(root),
        "messenger": _messenger(root),
    }
    # Kept out of the root so that detecting the root finds the Meta exports
    # only - a tracker file is picked separately, as it is in real use.
    tracker_dir = root / "tracker"
    tracker_dir.mkdir(exist_ok=True)
    exports["discord"] = tracker_dir / "smoke.dht"
    build_dht(exports["discord"])

    archive = Archive.create(root / "archive.sqlite", root / "vault", verbose=verbose)
    progress = print if verbose else None
    runner.ingest_path(archive, root, progress)
    runner.ingest_path(archive, exports["discord"], progress)

    return Fixture(root, archive, exports, _link_people(archive))


def isolate_settings(root: Path) -> None:
    """Point `settings.local.json` at the fixture for the rest of the process.

    The checks exercise "connect a database", which remembers its choice. Aiming
    that at a throwaway file is what keeps this machine's own connection out of
    it - there is nothing to put back afterwards.
    """
    config.SETTINGS_FILE = root / "settings.local.json"

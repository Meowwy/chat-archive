"""Rescue the attachments Discord's official data package still serves.

The `.dht` scrape recorded every attachment's URL but almost none of its bytes,
because Discord CDN links are signed and expire about a day after they are
issued (see "Discord attachments: what survives" in DOCUMENTATION.md). The
official data package hands out the *same* attachments under links signed with
`ex=0` - no expiry - so wherever a conversation appears in both, the bytes are
downloadable again, years later.

The package holds one folder per conversation, named `c<channel id>`, and that
id *is* the archive's `channels.id`: both come from Discord. So finding the
export folder for a conversation is a lookup, not a search. Each attachment URL
carries its snowflake the same way - `/attachments/<channel>/<attachment>/<name>`
- and that is the archive's `attachments.attachment_id`, which makes every link
exact rather than a guess. Before touching a conversation the script still
checks that the two agree on at least one message id.

For every Discord conversation the archive already holds, this:

  * downloads each attachment the archive does not already have,
  * stores the bytes in the media vault, addressed by sha256,
  * points the existing `attachments` row at them, and replaces its dead
    `download_url` with the one that works,
  * creates the row and the `message_attachments` link where the scrape never
    saw the attachment at all, and
  * inserts the messages the export has and the scrape missed, so those
    attachments have a message to hang off.

Nothing is overwritten: bytes already in the vault are not fetched again, an
`attachments` row that already has a `local_path` is left alone, and every
insert is `OR IGNORE`. Interrupting it is safe - re-running picks up where it
stopped, and retries anything that failed.

The package only contains messages *you* sent, so it can never complete the
other side of a conversation. What it cannot rescue keeps its row and still
renders as an unavailable-attachment card.

    py tools/discord_export_media.py <export folder> --dry-run
    py tools/discord_export_media.py <export folder>
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sqlite3
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from archive.archive import Archive  # noqa: E402
from archive.vault import Vault  # noqa: E402

# Discord's CDN refuses a bare urllib user-agent.
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) chat-archive/1.0"
TIMEOUT = 60
RETRIES = 3
COMMIT_EVERY = 100

CHANNEL_DIR = re.compile(r"^c(\d+)$")


# ------------------------------------------------------- finding the export


def messages_root(given: Path) -> Path:
    """The folder holding the `c<id>` conversation folders.

    Discord localises the package, so it is `Zprávy` here and `Messages`
    elsewhere - take either the package root or that folder itself, and look
    for the shape rather than the name.
    """
    given = given.expanduser().resolve()
    if not given.is_dir():
        raise SystemExit(f"no such folder: {given}")
    if any(CHANNEL_DIR.match(child.name) for child in given.iterdir() if child.is_dir()):
        return given
    for child in sorted(given.iterdir()):
        if child.is_dir() and any(
            CHANNEL_DIR.match(grandchild.name)
            for grandchild in child.iterdir() if grandchild.is_dir()
        ):
            return child
    raise SystemExit(
        f"{given} does not look like a Discord data package - no c<channel id> folders"
    )


# --------------------------------------------------------------- the export


@dataclass
class Media:
    """One attachment named by the export, and where it belongs."""

    attachment_id: int
    message_id: int
    channel_id: int
    name: str
    url: str          # the working, non-expiring link
    normalized: str   # the same link without its signature - the archive's key


@dataclass
class Message:
    """One message of the export: everything needed to insert it if it is new."""

    message_id: int
    channel_id: int
    sender_id: int
    text: str
    timestamp: int


def parse_media_url(url: str, message_id: int) -> Media | None:
    """.../attachments/<channel>/<attachment>/<name>?ex=0&... -> Media."""
    split = urllib.parse.urlsplit(url)
    path = split.path.split("/")
    if len(path) < 5 or path[1] != "attachments":
        return None
    try:
        channel_id, attachment_id = int(path[2]), int(path[3])
    except ValueError:
        return None
    return Media(
        attachment_id=attachment_id,
        message_id=message_id,
        channel_id=channel_id,
        name=urllib.parse.unquote(path[4]) or "unknown",
        url=url,
        normalized=split._replace(query="", fragment="").geturl(),
    )


def utc_ms(stamp: str) -> int:
    """'2022-03-08 07:00:09' -> epoch ms (Discord writes these in UTC)."""
    return int(datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


def read_conversation(folder: Path, channel_id: int, sender_id: int):
    """Every message and every attachment of one exported conversation."""
    raw = (folder / "messages.json").read_text(encoding="utf-8")
    exported = json.loads(raw) if raw.strip() else []
    media, messages = [], []
    for entry in exported:
        message_id = int(entry["ID"])
        messages.append(Message(message_id, channel_id, sender_id,
                                entry.get("Contents") or "", utc_ms(entry["Timestamp"])))
        for url in (entry.get("Attachments") or "").split():
            found = parse_media_url(url, message_id)
            if found:
                media.append(found)
    return messages, media


def conversations(con: sqlite3.Connection, root: Path, quiet: bool = False):
    """Pair every Discord channel in the archive with its export folder.

    The folder name carries the channel id, so this is a lookup. It is still
    checked: the conversation has to be a DM, and the two have to agree on at
    least one message id before anything is downloaded on its behalf.
    """
    paired, skipped = [], []
    for row in con.execute(
        "SELECT id, name FROM channels WHERE platform = 'discord' ORDER BY name"
    ):
        channel_id, name = row["id"], row["name"]
        folder = root / f"c{channel_id}"
        if not (folder / "channel.json").is_file():
            skipped.append((name, "not in this export"))
            continue

        meta = json.loads((folder / "channel.json").read_text(encoding="utf-8"))
        if meta.get("type") != "DM":
            skipped.append((name, f"export calls it {meta.get('type')}, not a DM"))
            continue

        exported_ids = {
            int(entry["ID"])
            for entry in json.loads((folder / "messages.json").read_text(encoding="utf-8"))
        } if (folder / "messages.json").is_file() else set()
        if not exported_ids:
            skipped.append((name, "export holds no messages"))
            continue
        stored_ids = {
            message_id for (message_id,) in con.execute(
                "SELECT message_id FROM messages WHERE channel_id = ?", (channel_id,))
        }
        shared = len(exported_ids & stored_ids)
        if not shared:
            skipped.append((name, "shares no message with the export - not the same chat"))
            continue

        paired.append((channel_id, name, folder, meta, len(exported_ids), shared))

    if not quiet:
        for channel_id, name, _folder, _meta, total, shared in paired:
            print(f"  {name:<14} c{channel_id}  {total:>7,} exported, "
                  f"{shared:>7,} of them already in the archive")
        for name, why in skipped:
            print(f"  {name:<14} skipped - {why}")
    return paired


def self_discord_id(con: sqlite3.Connection, paired: list) -> int:
    """The account the export belongs to: you.

    Every DM lists its two participants, and the archive already knows which
    identity is yours. Cross-checking the two is what stops a package belonging
    to someone else being written into your archive as if you had sent it.
    """
    common: set[str] | None = None
    for _cid, _name, _folder, meta, _t, _s in paired:
        recipients = set(meta.get("recipients") or [])
        common = recipients if common is None else common & recipients

    row = con.execute(
        "SELECT u.id FROM users u JOIN people p ON p.person_id = u.person_id "
        "WHERE p.is_self = 1 AND u.platform = 'discord'"
    ).fetchone()
    if row:
        if common and str(row[0]) not in common:
            raise SystemExit(
                f"this export belongs to Discord user(s) {sorted(common)}, but the "
                f"archive's own Discord identity is {row[0]} - wrong package?"
            )
        return row[0]
    if common and len(common) == 1:
        return int(next(iter(common)))
    raise SystemExit(
        "cannot tell which Discord identity is yours. Mark yourself on the People "
        "page, or run: py -m archive people"
    )


# ------------------------------------------------------------- downloading


def dimensions(blob: bytes) -> tuple[int | None, int | None]:
    """Width and height straight out of the file header, for the formats
    Discord actually serves. Only ever shown on an attachment whose bytes are
    missing, so a None here costs nothing."""
    try:
        if blob[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", blob[16:24])
        if blob[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", blob[6:10])
        if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
            if blob[12:16] == b"VP8X":
                return (int.from_bytes(blob[24:27], "little") + 1,
                        int.from_bytes(blob[27:30], "little") + 1)
            if blob[12:16] == b"VP8L":
                bits = int.from_bytes(blob[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if blob[12:16] == b"VP8 ":
                return struct.unpack("<HH", blob[26:30])
        if blob[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(blob):
                if blob[i] != 0xFF:
                    i += 1
                    continue
                marker, length = blob[i + 1], struct.unpack(">H", blob[i + 2:i + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    height, width = struct.unpack(">HH", blob[i + 5:i + 9])
                    return width, height
                i += 2 + length
    except (struct.error, IndexError, ValueError):
        pass
    return None, None


@dataclass
class Fetched:
    media: Media
    sha256: str | None = None
    relpath: str | None = None
    size: int = 0
    width: int | None = None
    height: int | None = None
    mime: str | None = None
    new_bytes: bool = False
    error: str | None = None


def fetch(media: Media, vault: Vault) -> Fetched:
    """Download one attachment into the vault. 404 is final; the rest retries."""
    request = urllib.request.Request(media.url, headers={"User-Agent": UA})
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                blob = response.read()
                mime = (response.headers.get("Content-Type") or "").split(";")[0].strip()
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404, 410) or attempt == RETRIES - 1:
                return Fetched(media, error=f"HTTP {exc.code}")
        except Exception as exc:  # timeouts, resets, DNS
            if attempt == RETRIES - 1:
                return Fetched(media, error=f"{type(exc).__name__}: {exc}")
        time.sleep(2 ** attempt)

    suffix = Path(media.name).suffix or mimetypes.guess_extension(mime or "") or ".bin"
    stored = vault.put_bytes(blob, suffix)
    width, height = dimensions(blob)
    return Fetched(
        media, stored.sha256, stored.relpath, len(blob), width, height,
        mime or mimetypes.guess_type(media.name)[0], stored.is_new,
    )


# ----------------------------------------------------------------- writing


@dataclass
class Tally:
    messages_added: int = 0
    rows_added: int = 0
    rows_linked: int = 0
    rows_filled: int = 0
    urls_refreshed: int = 0
    bytes_new: int = 0
    bytes_duplicate: int = 0
    downloaded_bytes: int = 0
    already_held: int = 0
    failures: list[dict] = field(default_factory=list)


def store(con: sqlite3.Connection, got: Fetched, known: set[int], tally: Tally) -> None:
    """Put one downloaded attachment into the database."""
    media = got.media
    if got.error:
        tally.failures.append({
            "attachment_id": str(media.attachment_id),
            "message_id": str(media.message_id),
            "name": media.name, "url": media.url, "error": got.error,
        })
        return

    tally.downloaded_bytes += got.size
    tally.bytes_new += 1 if got.new_bytes else 0
    tally.bytes_duplicate += 0 if got.new_bytes else 1

    if media.attachment_id in known:
        # The scrape knew about this attachment; give it its bytes back, and a
        # link that has not expired.
        tally.rows_filled += con.execute(
            "UPDATE attachments SET sha256 = ?, local_path = ? "
            "WHERE attachment_id = ? AND (sha256 IS NULL OR local_path IS NULL)",
            (got.sha256, got.relpath, media.attachment_id),
        ).rowcount
        tally.urls_refreshed += con.execute(
            "UPDATE attachments SET download_url = ? "
            "WHERE attachment_id = ? AND download_url IS NOT ?",
            (media.url, media.attachment_id, media.url),
        ).rowcount
    else:
        con.execute(
            """
            INSERT OR IGNORE INTO attachments
                (attachment_id, name, type, normalized_url, download_url,
                 size, width, height, platform, local_path, sha256)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'discord', ?, ?)
            """,
            (media.attachment_id, media.name, got.mime, media.normalized, media.url,
             got.size, got.width, got.height, got.relpath, got.sha256),
        )
        tally.rows_added += 1
        known.add(media.attachment_id)


def link(con: sqlite3.Connection, wanted: dict[int, Media], known: set[int],
         tally: Tally) -> None:
    """Link every attachment the export names to the message that carries it.

    Deliberately a pass of its own, over everything the export names rather than
    over what was just downloaded. An attachment whose bytes the archive already
    holds is never fetched, so if its link were written only alongside a download
    it could never be written at all - which is exactly the case for one the
    tracker recovered from an embedded blob, on a message the scrape missed.
    """
    for media in wanted.values():
        if media.attachment_id not in known:
            continue  # its download failed; the next run will try again
        tally.rows_linked += con.execute(
            "INSERT OR IGNORE INTO message_attachments (message_id, attachment_id) "
            "VALUES (?, ?)",
            (media.message_id, media.attachment_id),
        ).rowcount


# -------------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download the attachments a Discord data package can still serve, "
                    "into the connected archive.")
    parser.add_argument("export", type=Path,
                        help="the unzipped Discord data package, or its messages folder")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be downloaded, touch nothing")
    parser.add_argument("--workers", type=int, default=8,
                        help="parallel downloads (default 8)")
    parser.add_argument("--limit", type=int, help="stop after this many downloads")
    parser.add_argument("--report", type=Path,
                        help="where to write the run report "
                             "(default: beside the archive database)")
    args = parser.parse_args()

    root = messages_root(args.export)
    archive = Archive.connected()
    report_path = args.report or archive.path.parent / "discord_export_media_report.json"

    print(f"archive  {archive.path}")
    print(f"vault    {archive.vault.root}")
    print(f"export   {root}\n")

    reader = archive.read()
    paired = conversations(reader, root)
    if not paired:
        raise SystemExit("\nnone of the archive's Discord conversations are in this export")
    sender_id = self_discord_id(reader, paired)
    print(f"\nexported by Discord user {sender_id} - the archive's own identity")

    # -- what the export offers ---------------------------------------
    wanted: dict[int, Media] = {}
    messages: list[Message] = []
    for channel_id, _name, folder, _meta, _total, _shared in paired:
        found_messages, found_media = read_conversation(folder, channel_id, sender_id)
        messages.extend(found_messages)
        for item in found_media:
            wanted.setdefault(item.attachment_id, item)

    # -- what the archive already has ---------------------------------
    known, held = set(), set()
    for row in reader.execute("SELECT attachment_id, local_path, sha256 FROM attachments"):
        known.add(row["attachment_id"])
        if row["local_path"] and row["sha256"] and archive.vault.exists(row["local_path"]):
            held.add(row["attachment_id"])
    stored_messages = {
        message_id for (message_id,) in reader.execute(
            "SELECT message_id FROM messages WHERE platform = 'discord'")
    }
    linked = {
        (row["message_id"], row["attachment_id"])
        for row in reader.execute("SELECT message_id, attachment_id FROM message_attachments")
    }

    absent = [m for m in messages if m.message_id not in stored_messages]
    todo = [m for attachment_id, m in wanted.items() if attachment_id not in held]
    unlinked = sum(1 for m in wanted.values()
                   if (m.message_id, m.attachment_id) not in linked)

    print(f"\n{len(wanted):,} attachments named by the export")
    print(f"{len(wanted) - len(todo):,} already in the vault, {len(todo):,} to download")
    print(f"  {sum(1 for m in todo if m.attachment_id not in known):,} have no "
          f"attachments row at all")
    print(f"{unlinked:,} are not linked to their message yet")
    print(f"{len(absent):,} messages in the export are not in the archive yet")

    if args.limit:
        todo = todo[:args.limit]
    if args.dry_run:
        print("\n--dry-run: nothing downloaded, nothing written")
        return
    if not todo and not absent and not unlinked:
        print("\nnothing to do")
        return

    # -- do it --------------------------------------------------------
    tally = Tally()
    tally.already_held = len(wanted) - len(todo)
    started = time.time()

    with archive.write() as con:
        for message in absent:
            tally.messages_added += con.execute(
                """
                INSERT OR IGNORE INTO messages
                    (message_id, sender_id, channel_id, text, timestamp,
                     platform, is_unsent)
                VALUES (?, ?, ?, ?, ?, 'discord', 0)
                """,
                (message.message_id, message.sender_id, message.channel_id,
                 message.text, message.timestamp),
            ).rowcount
        con.commit()
        print(f"\n{tally.messages_added:,} message(s) added\n")

        done = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for got in pool.map(lambda media: fetch(media, archive.vault), todo):
                store(con, got, known, tally)
                done += 1
                if done % COMMIT_EVERY == 0:
                    con.commit()
                    print(f"  {done:,}/{len(todo):,}  "
                          f"{tally.downloaded_bytes / 1e6:,.0f} MB  "
                          f"{len(tally.failures)} failed  "
                          f"{done / max(time.time() - started, 1e-9):.1f}/s")
        link(con, wanted, known, tally)
        con.commit()

    elapsed = time.time() - started
    summary = {
        "archive": str(archive.path),
        "vault": str(archive.vault.root),
        "export": str(root),
        "conversations": [name for _cid, name, *_rest in paired],
        "attachments_named_by_export": len(wanted),
        "already_in_vault": tally.already_held,
        "attempted": len(todo),
        "downloaded": len(todo) - len(tally.failures),
        "failed": len(tally.failures),
        "megabytes": round(tally.downloaded_bytes / 1e6, 1),
        "vault_files_new": tally.bytes_new,
        "vault_files_duplicate": tally.bytes_duplicate,
        "messages_added": tally.messages_added,
        "attachment_rows_added": tally.rows_added,
        "attachment_rows_given_bytes": tally.rows_filled,
        "download_urls_refreshed": tally.urls_refreshed,
        "message_links_added": tally.rows_linked,
        "seconds": round(elapsed, 1),
        "failures": tally.failures,
    }
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\ndownloaded {summary['downloaded']:,} of {len(todo):,} "
          f"({summary['megabytes']:,} MB) in {elapsed / 60:.1f} min")
    print(f"  {tally.bytes_new:,} new files in the vault, "
          f"{tally.bytes_duplicate:,} were already there under another name")
    print(f"  {tally.messages_added:,} messages, {tally.rows_added:,} attachment rows and "
          f"{tally.rows_linked:,} message links added")
    print(f"  {tally.rows_filled:,} existing attachments given their bytes back, "
          f"{tally.urls_refreshed:,} dead links replaced")
    if tally.failures:
        print(f"  {len(tally.failures):,} failed - listed in the report, "
              f"re-run to retry them")
    print(f"\nwritten: {report_path}")


if __name__ == "__main__":
    main()

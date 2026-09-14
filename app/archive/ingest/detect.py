"""Work out what the user picked actually contains.

Four kinds of source are understood:

- a Meta export folder - the extracted download root, a `your_*_activity`
  folder, its `messages/` subfolder, or `messages/inbox/` itself;
- a Messenger encrypted-chat download - the flat `messages/` folder of one JSON
  per conversation that Meta's "secure storage" export unzips to, beside the
  `media/` folder its URIs point at (see ingest/secure.py);
- a Microsoft Teams export - the folder it unpacks to, or the `.tar` it arrives
  as, which is read in place (see ingest/teams_export.py);
- a Discord History Tracker `.dht` file, either picked directly or found in a
  folder that was picked.

Media URIs are written relative to the *parent* of the folder we anchor on -
"your_instagram_activity/messages/inbox/x/photos/1.jpg" beside
`your_instagram_activity`, "./media/<uuid>.jpeg" beside `messages` - so locating
that folder is what lets us resolve attachments in either layout.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .. import db
from . import secure
from .teams_export import TeamsExport

MARKERS = {
    "your_facebook_activity": "facebook",
    "your_instagram_activity": "instagram",
}

# A tracker file is a SQLite database; these are the tables it must have for
# the importer to know what to do with it.
DHT_TABLES = {"messages", "channels", "users", "attachments"}
DB_SUFFIXES = {".dht", ".sqlite", ".sqlite3", ".db"}

# What an encrypted-chat download calls its conversation folder. A DYI export
# has a folder of the same name, so the two are told apart by content.
SECURE_DIR = "messages"


@dataclass
class ExportSource:
    kind: str
    marker_dir: Path
    thread_files: list[Path] = field(default_factory=list)
    layout: str = "dyi"

    @property
    def path(self) -> Path:
        """What the user picked, as the ingest log records it."""
        return self.marker_dir

    @property
    def platform(self) -> str:
        """Whose namespace these rows join.

        An encrypted-chat download holds Facebook Messenger conversations, so it
        shares `facebook`'s threads, users and people links rather than forking a
        fourth platform the rest of the app would have to learn about.
        """
        return "facebook" if self.kind == "messenger" else self.kind

    @property
    def media_root(self) -> Path:
        """URIs in the JSON are relative to this."""
        return self.marker_dir.parent

    @property
    def label(self) -> str:
        return {
            "facebook": "Facebook",
            "instagram": "Instagram",
            "messenger": "Messenger (encrypted chats)",
        }.get(self.kind, self.kind)

    def thread_label(self, path: Path) -> str:
        """How one thread file is named in progress output."""
        return path.stem if self.layout == "secure" else path.parent.name

    def summary(self) -> dict:
        threads = len(self.thread_files)
        messages = 0
        for f in self.thread_files:
            try:
                messages += len(json.loads(f.read_bytes()).get("messages", []))
            except (OSError, ValueError):
                pass
        return {
            "kind": self.kind,
            "label": self.label,
            "path": str(self.marker_dir),
            "threads": threads,
            "messages": messages,
        }


@dataclass
class TeamsSource:
    """A Microsoft Teams export - an unpacked folder, or the .tar it came as."""

    export: TeamsExport
    kind: str = "teams"
    label: str = "Microsoft Teams"

    @property
    def path(self) -> Path:
        return self.export.path

    def summary(self) -> dict:
        """What is inside, counting only what would actually be imported.

        Teams lists every thread the account has ever been attached to, empty
        ones and its own internal "stream" threads included, so counting raw
        conversations would promise more than the import delivers.
        """
        from .teams import importable  # local: teams.py imports this module

        threads = messages = 0
        try:
            index = self.export.read_index()
        except (OSError, ValueError):
            index = {}
        for conversation in index.get("conversations") or []:
            found = sum(1 for m in conversation.get("MessageList") or [] if importable(m))
            if found:
                threads += 1
                messages += found
        return {
            "kind": self.kind,
            "label": self.label,
            "path": str(self.path),
            "threads": threads,
            "messages": messages,
        }


@dataclass
class DhtSource:
    """A Discord History Tracker file, ready to be copied into the archive."""

    path: Path
    kind: str = "discord"
    label: str = "Discord"

    def summary(self) -> dict:
        channels = messages = 0
        con = _open_ro(self.path)
        if con is not None:
            try:
                channels = con.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
                messages = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            except sqlite3.Error:
                pass
            finally:
                con.close()
        return {
            "kind": self.kind,
            "label": self.label,
            "path": str(self.path),
            "threads": channels,
            "messages": messages,
        }


def _open_ro(path: Path) -> sqlite3.Connection | None:
    """Open a file as a read-only SQLite database, or None if it is not one."""
    try:
        con = sqlite3.connect(db.ro_uri(path), uri=True)
        con.execute("PRAGMA busy_timeout = 5000")
        con.execute("SELECT COUNT(*) FROM sqlite_master")
        return con
    except sqlite3.Error:
        return None


def is_dht(path: Path) -> bool:
    """True when `path` is a tracker database we can import."""
    if not path.is_file() or path.suffix.lower() not in DB_SUFFIXES:
        return False
    con = _open_ro(path)
    if con is None:
        return False
    try:
        names = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        return False
    finally:
        con.close()
    return DHT_TABLES <= names


def _thread_files(marker: Path) -> list[Path]:
    inbox = marker / "messages" / "inbox"
    if not inbox.is_dir():
        return []
    return sorted(inbox.glob("*/message_*.json"))


def _secure_thread_files(folder: Path) -> list[Path]:
    """The conversation files in an encrypted-chat download, if this is one.

    Judged by content, never by name: a DYI export's `messages/` folder is full
    of settings JSON that must not be mistaken for conversations.
    """
    if not folder.is_dir() or folder.name != SECURE_DIR or folder.parent.name in MARKERS:
        return []
    files = []
    for candidate in sorted(folder.glob("*.json")):
        try:
            if secure.looks_like_thread(json.loads(candidate.read_bytes())):
                files.append(candidate)
        except (OSError, ValueError):
            continue
    return files


def _find_secure(path: Path) -> list[Path]:
    """Folders at, or just below, `path` that could be an encrypted-chat download."""
    found: list[Path] = []
    seen: set[Path] = set()
    for candidate in [path, path / SECURE_DIR, *sorted(path.glob(f"*/{SECURE_DIR}"))]:
        if not candidate.is_dir():
            continue
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        found.append(candidate)
    return found


def _find_teams(path: Path) -> list[TeamsExport]:
    """Teams exports at, or just below, `path`.

    A download that has been unpacked is a folder holding `messages.json`; one
    that has not is the `.tar` itself, and either is read the same way. Looking
    one level down means the folder the tar was extracted *into* works as well
    as the folder it produced.
    """
    found: list[TeamsExport] = []
    seen: set[Path] = set()
    candidates = [path, *sorted(path.glob("*")), *sorted(path.glob("*.tar"))]
    for candidate in candidates:
        if not (candidate.is_dir() or candidate.suffix.lower() == ".tar"):
            continue
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        export = TeamsExport.at(candidate)
        if export is not None:
            found.append(export)
    return found


def _find_markers(path: Path) -> list[Path]:
    """Locate `your_*_activity` folders at, above, or just below `path`."""
    found: list[Path] = []

    for candidate in [path, *path.parents]:
        if candidate.name in MARKERS and candidate.is_dir():
            found.append(candidate)
            break  # the nearest enclosing marker wins

    if not found:
        for name in MARKERS:
            for depth in ("", "*/", "*/*/"):
                found.extend(p for p in path.glob(f"{depth}{name}") if p.is_dir())

    # de-duplicate, preserving order
    seen: set[Path] = set()
    unique = []
    for p in found:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            unique.append(p)
    return unique


def detect(
    path: str | Path, *, connected: Path | str | None = None
) -> list[ExportSource | DhtSource]:
    """Return every importable source found at (or around) `path`.

    `connected` is the archive being imported *into*, if there is one. An
    archive is a SQLite database with all of DHT's tables, so it would happily
    detect as a tracker file and import into itself; naming it here is what
    turns that into a clear refusal.

    Raises ValueError with an actionable message when nothing usable is found.
    """
    path = Path(path).expanduser().resolve()

    if path.is_file():
        if connected is not None and path == Path(connected).resolve():
            raise ValueError("That file is the archive you are connected to, not an import.")
        if is_dht(path):
            return [DhtSource(path=path)]
        export = TeamsExport.at(path)
        if export is not None:
            return [TeamsSource(export=export)]
        raise ValueError(
            f"{path.name} is not an export this app can read.\n"
            "Pick the .dht file Discord History Tracker writes, the .tar a "
            "Microsoft Teams export arrives as, or a folder holding a "
            "Facebook, Instagram or Messenger export."
        )

    if not path.is_dir():
        raise ValueError(f"No such file or folder: {path}")

    sources: list[ExportSource | DhtSource] = []
    for marker in _find_markers(path):
        files = _thread_files(marker)
        if files:
            sources.append(ExportSource(kind=MARKERS[marker.name], marker_dir=marker, thread_files=files))
    for folder in _find_secure(path):
        files = _secure_thread_files(folder)
        if files:
            sources.append(
                ExportSource(
                    kind="messenger", marker_dir=folder, thread_files=files, layout="secure"
                )
            )
    sources.extend(TeamsSource(export=export) for export in _find_teams(path))
    # A folder can also simply hold tracker files - picking Archives/ works.
    sources.extend(DhtSource(path=found) for found in sorted(path.glob("*.dht")) if is_dht(found))

    if not sources:
        raise ValueError(
            f"Nothing importable found in {path}.\n"
            "Pick the folder containing 'your_facebook_activity' or "
            "'your_instagram_activity' (or one of those folders itself), the "
            "'messages' folder from a Messenger encrypted-chat download, a "
            "Microsoft Teams export (its folder or its .tar), or a "
            "Discord History Tracker .dht file."
        )
    return sources

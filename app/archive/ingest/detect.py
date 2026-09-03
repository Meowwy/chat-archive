"""Work out what the user picked actually contains.

Two kinds of source are understood:

- a Meta export folder - the extracted download root, a `your_*_activity`
  folder, its `messages/` subfolder, or `messages/inbox/` itself;
- a Discord History Tracker `.dht` file, either picked directly or found in a
  folder that was picked.

Media URIs inside the Meta JSON are written relative to the *parent* of the
`your_*_activity` folder (e.g. "your_instagram_activity/messages/inbox/x/photos/1.jpg"),
so locating that marker folder is what lets us resolve attachments.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .. import config, db

MARKERS = {
    "your_facebook_activity": "facebook",
    "your_instagram_activity": "instagram",
}

# A tracker file is a SQLite database; these are the tables it must have for
# the importer to know what to do with it.
DHT_TABLES = {"messages", "channels", "users", "attachments"}
DB_SUFFIXES = {".dht", ".sqlite", ".sqlite3", ".db"}


@dataclass
class ExportSource:
    kind: str
    marker_dir: Path
    thread_files: list[Path] = field(default_factory=list)

    @property
    def path(self) -> Path:
        """What the user picked, as the ingest log records it."""
        return self.marker_dir

    @property
    def media_root(self) -> Path:
        """URIs in the JSON are relative to this."""
        return self.marker_dir.parent

    @property
    def label(self) -> str:
        return {"facebook": "Facebook", "instagram": "Instagram"}.get(self.kind, self.kind)

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


def detect(path: str | Path) -> list[ExportSource | DhtSource]:
    """Return every importable source found at (or around) `path`.

    Raises ValueError with an actionable message when nothing usable is found.
    """
    path = Path(path).expanduser().resolve()

    if path.is_file():
        if config.DB_PATH is not None and path == Path(config.DB_PATH).resolve():
            raise ValueError("That file is the archive you are connected to, not an import.")
        if is_dht(path):
            return [DhtSource(path=path)]
        raise ValueError(
            f"{path.name} is not a Discord History Tracker file.\n"
            "Pick the .dht file the tracker writes, or a folder holding a "
            "Facebook or Instagram export."
        )

    if not path.is_dir():
        raise ValueError(f"No such file or folder: {path}")

    sources: list[ExportSource | DhtSource] = []
    for marker in _find_markers(path):
        files = _thread_files(marker)
        if files:
            sources.append(ExportSource(kind=MARKERS[marker.name], marker_dir=marker, thread_files=files))
    # A folder can also simply hold tracker files - picking Archives/ works.
    sources.extend(DhtSource(path=found) for found in sorted(path.glob("*.dht")) if is_dht(found))

    if not sources:
        raise ValueError(
            f"Nothing importable found in {path}.\n"
            "Pick the folder containing 'your_facebook_activity' or "
            "'your_instagram_activity' (or one of those folders itself), or a "
            "Discord History Tracker .dht file."
        )
    return sources

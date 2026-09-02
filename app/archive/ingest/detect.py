"""Work out what a folder the user picked actually contains.

Accepts anything reasonable: the extracted download root, a `your_*_activity`
folder, its `messages/` subfolder, or `messages/inbox/` itself.

Media URIs inside the JSON are written relative to the *parent* of the
`your_*_activity` folder (e.g. "your_instagram_activity/messages/inbox/x/photos/1.jpg"),
so locating that marker folder is what lets us resolve attachments.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

MARKERS = {
    "your_facebook_activity": "facebook",
    "your_instagram_activity": "instagram",
}


@dataclass
class ExportSource:
    kind: str
    marker_dir: Path
    thread_files: list[Path] = field(default_factory=list)

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


def detect(path: str | Path) -> list[ExportSource]:
    """Return every Meta export found under (or around) `path`.

    Raises ValueError with an actionable message when nothing usable is found.
    """
    path = Path(path).resolve()
    if not path.is_dir():
        raise ValueError(f"Not a folder: {path}")

    sources = []
    for marker in _find_markers(path):
        files = _thread_files(marker)
        if files:
            sources.append(ExportSource(kind=MARKERS[marker.name], marker_dir=marker, thread_files=files))

    if not sources:
        raise ValueError(
            f"No Facebook or Instagram export found in {path}.\n"
            "Pick the folder containing 'your_facebook_activity' or "
            "'your_instagram_activity' (or one of those folders itself)."
        )
    return sources

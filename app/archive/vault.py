"""Content-addressed media store.

Layout: <root>/<sha256[:2]>/<sha256><ext>

Content addressing is what makes re-ingesting an overlapping export free: Meta
repeats every media file in every export, but identical bytes hash to the same
name and are stored once.

A `Vault` is bound to a directory when it is built, so nothing here has to ask
where the archive lives - the `Archive` that owns the vault already decided.
That is what lets a test point one at a temp folder without touching the
machine's own.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterable, NamedTuple

_CHUNK = 1024 * 1024


class Stored(NamedTuple):
    """Where a file ended up, and whether these bytes were new to the vault."""

    sha256: str
    relpath: str
    size: int
    is_new: bool


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def relpath_for(sha256: str, suffix: str) -> str:
    return f"{sha256[:2]}/{sha256}{suffix.lower()}"


class Vault:
    """Every image, video and file the archive holds, addressed by its hash."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def __repr__(self) -> str:  # shows up in ingest progress and test failures
        return f"Vault({str(self.root)!r})"

    def abspath(self, relpath: str) -> Path:
        return self.root / relpath

    def exists(self, relpath: str) -> bool:
        return self.abspath(relpath).is_file()

    def find(self, sha256: str, suffixes: Iterable[str]) -> str | None:
        """The path of a file stored under this hash, if one of these suffixes fits.

        Avatars live in the vault but are not attachments, so there is no row to
        read their filename off - the suffix has to be guessed. Callers ask the
        vault rather than rebuilding the layout themselves.
        """
        for suffix in suffixes:
            candidate = relpath_for(sha256, suffix)
            if self.exists(candidate):
                return candidate
        return None

    def put(self, src: Path) -> Stored:
        """Copy `src` into the vault.

        Copying is atomic: written to a temp file in the destination directory,
        then os.replace'd, so a crash can never leave a truncated file under a
        valid content hash.
        """
        src = Path(src)
        size = src.stat().st_size
        sha = hash_file(src)
        rel = relpath_for(sha, src.suffix)
        if self.exists(rel):
            return Stored(sha, rel, size, False)
        self._write(rel, lambda tmp: shutil.copyfile(src, tmp))
        return Stored(sha, rel, size, True)

    def put_bytes(self, blob: bytes, suffix: str) -> Stored:
        """Same as `put`, for data already in memory (DHT-embedded blobs)."""
        sha = hashlib.sha256(blob).hexdigest()
        rel = relpath_for(sha, suffix)
        if self.exists(rel):
            return Stored(sha, rel, len(blob), False)
        self._write(rel, lambda tmp: Path(tmp).write_bytes(blob))
        return Stored(sha, rel, len(blob), True)

    def _write(self, relpath: str, fill) -> None:
        """Fill a temp file beside the destination, then move it into place."""
        dest = self.abspath(relpath)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".part")
        os.close(fd)
        try:
            fill(tmp)
            os.replace(tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

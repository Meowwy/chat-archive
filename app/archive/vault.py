"""Content-addressed media store.

Layout: <VAULT>/<sha256[:2]>/<sha256><ext>

Content addressing is what makes re-ingesting an overlapping export free: Meta
repeats every media file in every export, but identical bytes hash to the same
name and are stored once.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from . import config

_CHUNK = 1024 * 1024


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def relpath_for(sha256: str, suffix: str) -> str:
    return f"{sha256[:2]}/{sha256}{suffix.lower()}"


def abspath(relpath: str) -> Path:
    return config.VAULT_DIR / relpath


def exists(relpath: str) -> bool:
    return abspath(relpath).is_file()


def put(src: Path) -> tuple[str, str, int, bool]:
    """Copy `src` into the vault.

    Returns (sha256, vault-relative path, size, was_new). Copying is atomic:
    written to a temp file in the destination directory, then os.replace'd, so a
    crash can never leave a truncated file under a valid content hash.
    """
    src = Path(src)
    size = src.stat().st_size
    sha = hash_file(src)
    rel = relpath_for(sha, src.suffix)
    dest = abspath(rel)
    if dest.is_file():
        return sha, rel, size, False

    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".part")
    os.close(fd)
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return sha, rel, size, True


def put_bytes(blob: bytes, suffix: str) -> tuple[str, str, int, bool]:
    """Same as `put`, for data already in memory (DHT-embedded blobs)."""
    sha = hashlib.sha256(blob).hexdigest()
    rel = relpath_for(sha, suffix)
    dest = abspath(rel)
    if dest.is_file():
        return sha, rel, len(blob), False

    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return sha, rel, len(blob), True

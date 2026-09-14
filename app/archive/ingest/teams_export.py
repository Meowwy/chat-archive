"""Reading a Microsoft Teams data export - from a folder, or straight from the .tar.

Teams hands back a single uncompressed tar with everything at its root:

    messages.json       every conversation and every message, one JSON document
    endpoints.json      the devices signed in; not imported
    invites.json        pending contact invites; not imported
    media/              one file per shared object, named <doc_id>.<n>.<ext>

`media/` is flat and keyed by the document ids a message lists in its
`amsreferences`. The index after the doc id says which rendition a file is:

    <doc_id>.1.jpeg     the object itself - the only one worth storing
    <doc_id>.2.jpeg     a video's poster frame
    <doc_id>.3.vtt      a voice message's transcript

Both shapes are read through the same object, because the tar is worth reading
in place: these exports run to many gigabytes and unpacking one needs that much
again before a single byte reaches the archive. `tarfile` indexes the members
once and then seeks, so a file is fetched without walking the archive again.

Nothing here writes: the reader hands out a stream and the vault decides what to
do with it, which is what lets `detect` open an export cheaply just to count what
is inside.
"""

from __future__ import annotations

import json
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

INDEX_NAME = "messages.json"
MEDIA_DIR = "media"

# <doc_id>.<rendition>.<ext> - see the module docstring.
_MEDIA_NAME = re.compile(r"^(?P<doc>.+)\.(?P<index>\d+)\.(?P<ext>[A-Za-z0-9]+)$")

# The rendition that is the object itself rather than a thumbnail or transcript.
PRIMARY_RENDITION = 1

# messages.json is tens of megabytes; a stray large file must not be parsed just
# to find out it was never an export in the first place.
_MAX_INDEX_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class MediaFile:
    """One stored object: where its bytes are, and what to call the file."""

    doc_id: str
    member: str  # path inside the export, e.g. "media/0-weu-d6-....1.jpeg"
    suffix: str  # ".jpeg"
    size: int


def looks_like_index(raw: object) -> bool:
    """True for the JSON document a Teams export puts at its root.

    Shape-based on purpose. Plenty of exports ship a file called
    `messages.json`; only this one pairs an account id with a list of
    conversations.
    """
    return (
        isinstance(raw, dict)
        and isinstance(raw.get("conversations"), list)
        and isinstance(raw.get("userId"), str)
    )


class TeamsExport:
    """One export, opened either as an unpacked folder or as the .tar itself."""

    def __init__(self, path: Path, *, is_tar: bool):
        self.path = path
        self.is_tar = is_tar
        self._tar: tarfile.TarFile | None = None
        self._members: dict[str, tarfile.TarInfo] | None = None
        self._media: dict[str, MediaFile] | None = None

    # -- opening ---------------------------------------------------------
    @classmethod
    def at(cls, path: Path) -> "TeamsExport | None":
        """An export at `path`, or None when it is not one.

        Accepts the folder holding `messages.json`, or the `.tar` itself. The
        file is read far enough to confirm the shape and no further.
        """
        path = Path(path)
        if path.is_dir():
            index = path / INDEX_NAME
            if not index.is_file() or index.stat().st_size > _MAX_INDEX_BYTES:
                return None
            export = cls(path, is_tar=False)
        elif path.is_file() and path.suffix.lower() == ".tar":
            export = cls(path, is_tar=True)
        else:
            return None
        try:
            if not looks_like_index(export.read_index()):
                return None
        except (OSError, ValueError, tarfile.TarError):
            return None
        return export

    # -- the tar ---------------------------------------------------------
    def _open_tar(self) -> tarfile.TarFile:
        if self._tar is None:
            # "r:" - uncompressed, so members can be seeked to individually
            # rather than the whole archive being streamed for every read.
            self._tar = tarfile.open(self.path, "r:")
        return self._tar

    def _tar_members(self) -> dict[str, tarfile.TarInfo]:
        """Index the tar once; every later lookup is a seek."""
        if self._members is None:
            self._members = {
                m.name.lstrip("./"): m for m in self._open_tar().getmembers() if m.isfile()
            }
        return self._members

    def close(self) -> None:
        if self._tar is not None:
            self._tar.close()
            self._tar = None

    def __enter__(self) -> "TeamsExport":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- reading ---------------------------------------------------------
    def read_index(self) -> dict:
        """Parse `messages.json`. Raises ValueError if it is absent or not JSON."""
        if self.is_tar:
            member = self._tar_members().get(INDEX_NAME)
            if member is None:
                raise ValueError(f"{self.path.name} has no {INDEX_NAME}")
            if member.size > _MAX_INDEX_BYTES:
                raise ValueError(f"{INDEX_NAME} is implausibly large ({member.size} bytes)")
            stream = self._open_tar().extractfile(member)
            if stream is None:
                raise ValueError(f"{INDEX_NAME} could not be read from {self.path.name}")
            with stream:
                return json.loads(stream.read().decode("utf-8"))
        return json.loads((self.path / INDEX_NAME).read_bytes().decode("utf-8"))

    def media(self) -> dict[str, MediaFile]:
        """Every stored object, by doc id. Only the primary rendition is kept."""
        if self._media is not None:
            return self._media

        found: dict[str, MediaFile] = {}
        for name, size in self._media_entries():
            match = _MEDIA_NAME.match(Path(name).name)
            if match is None or int(match["index"]) != PRIMARY_RENDITION:
                continue
            doc = match["doc"]
            found[doc] = MediaFile(doc, name, f".{match['ext'].lower()}", size)
        self._media = found
        return found

    def _media_entries(self) -> Iterator[tuple[str, int]]:
        """(member path, size) for everything under `media/`."""
        if self.is_tar:
            for name, member in self._tar_members().items():
                if name.startswith(f"{MEDIA_DIR}/"):
                    yield name, member.size
            return
        folder = self.path / MEDIA_DIR
        if not folder.is_dir():
            return
        for entry in folder.iterdir():
            if entry.is_file():
                yield f"{MEDIA_DIR}/{entry.name}", entry.stat().st_size

    def open_media(self, doc_id: str) -> BinaryIO | None:
        """A readable stream of one object's bytes, or None if it is not here.

        Teams leaves out a good deal of what its own messages point at, so a
        miss is ordinary and the caller records the attachment without bytes.
        """
        entry = self.media().get(doc_id)
        if entry is None:
            return None
        if self.is_tar:
            member = self._tar_members().get(entry.member)
            return None if member is None else self._open_tar().extractfile(member)
        try:
            return open(self.path / entry.member, "rb")
        except OSError:
            return None

    # -- description -----------------------------------------------------
    @property
    def label(self) -> str:
        return f"{self.path.name}{'' if self.is_tar else '/'}"

"""An archive: one database, its media vault, its Czech dictionary.

Those three things always travel together - a vault belongs to the database
whose `attachments` rows point into it - so one object owns all three and hands
out connections. Everything downstream takes what it needs *as an argument*
rather than reaching for wherever the archive happens to be, which is what makes
a second archive (a test fixture, a database you are inspecting before
connecting to it) possible at all.

    archive = Archive.connected()          # what this machine is set up to use
    archive = Archive.open(path)           # that file, plainly
    archive = Archive.create(path)         # a new, empty, complete archive

    archive.read()                         # cached read-only connection
    with archive.write() as con: ...       # read-write, committed by the caller

Reads and writes are deliberately asymmetric. `read()` hands back one long-lived
read-only connection, because the viewer holds it for the life of the process
and must never be able to modify the archive. `write()` opens a connection for
the duration of the job and drops the cached reader afterwards, so the next read
sees what was just written - nobody has to remember to invalidate anything.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config, db, migrate
from .czech import Lexicon
from .vault import Vault

# Suffixes an avatar might have been stored under. Avatars live in the vault but
# are not attachments, so there is no row naming the file - see Vault.find().
AVATAR_SUFFIXES = (".webp", ".png", ".jpg", ".gif", ".jpeg")


class Archive:
    """The connected archive. Everything that needs one is handed one."""

    def __init__(
        self,
        db_path: Path | str,
        vault_path: Path | str,
        *,
        lexicon_path: Path | str | None = None,
    ):
        self.path = Path(db_path)
        self.vault = Vault(vault_path)
        self.lexicon_path = Path(lexicon_path) if lexicon_path else None
        self._reader: sqlite3.Connection | None = None
        self._lexicon: Lexicon | None = None
        self._lexicon_loaded = False

    def __repr__(self) -> str:
        return f"Archive({str(self.path)!r})"

    # -- opening ---------------------------------------------------------

    @classmethod
    def open(
        cls,
        db_path: Path | str,
        vault_path: Path | str | None = None,
        *,
        lexicon_path: Path | str | None = None,
    ) -> "Archive":
        """Handle for an existing archive file.

        `vault_path` defaults to whichever vault that database is remembered
        with, so connecting to an archive twice does not move its media.
        """
        path = Path(db_path).expanduser().resolve()
        if not path.is_file():
            raise config.NoDatabase(f"No such database file: {path}")
        return cls(
            path,
            vault_path or config.vault_for(path),
            lexicon_path=lexicon_path or config.lexicon_path(),
        )

    @classmethod
    def create(
        cls,
        db_path: Path | str,
        vault_path: Path | str | None = None,
        *,
        lexicon_path: Path | str | None = None,
        verbose: bool = True,
    ) -> "Archive":
        """Build an empty but complete archive and return a handle on it."""
        path = migrate.create_archive(db_path, verbose=verbose)
        return cls(
            path,
            vault_path or config.default_vault_for(path),
            lexicon_path=lexicon_path or config.lexicon_path(),
        )

    @classmethod
    def connected(cls) -> "Archive":
        """Whichever archive this machine is set up to use.

        Raises NoDatabase - with a message that names the way out - when there
        is none, or when the one that was remembered has since gone missing.
        """
        found = config.resolve()
        if found is None:
            raise config.NoDatabase(
                "No archive database is connected. Open the app and use Connect a "
                "database, or run: py -m archive db <path to .sqlite>"
            )
        if not found.db_path.is_file():
            raise config.NoDatabase(f"The connected database is missing: {found.db_path}")
        return cls(found.db_path, found.vault_path, lexicon_path=found.lexicon_path)

    def remember(self) -> None:
        """Use this archive from now on, and next time the app starts."""
        config.remember(self.path, self.vault.root)

    def upgrade(self) -> None:
        """Bring an older archive - or a raw DHT mirror - up to this schema.

        Only ever adds columns and tables, so it is safe on every open and is
        what lets you hand the app a tracker file's own database.
        """
        with self.write() as con:
            migrate.ensure_schema(con)

    # -- connections -----------------------------------------------------

    def read(self) -> sqlite3.Connection:
        """The shared read-only connection. Opened on first use, then reused."""
        if self._reader is None:
            self._reader = db.connect_ro(self.path)
        return self._reader

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """A read-write connection for the duration of one job.

        Transactions belong to the caller - ingest wants one per source, and the
        .dht importer has to run its own because ATTACH cannot happen inside
        one. Closing drops the cached reader, so the next read sees the write.
        """
        con = db.connect(self.path)
        try:
            yield con
        finally:
            con.close()
            self._drop_reader()

    def _drop_reader(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None

    def close(self) -> None:
        self._drop_reader()
        if self._lexicon is not None:
            self._lexicon.close()
        self._lexicon, self._lexicon_loaded = None, False

    # -- the dictionary --------------------------------------------------

    @property
    def lexicon(self) -> Lexicon | None:
        """The Czech dictionary, or None when it has not been built.

        Loaded once, on the first search. None is a normal answer: search then
        stops widening and matches literal words instead.
        """
        if not self._lexicon_loaded:
            self._lexicon = Lexicon.open(self.lexicon_path)
            self._lexicon_loaded = True
        return self._lexicon

    # -- what it holds ---------------------------------------------------

    def summary(self) -> dict:
        """Counts for the status endpoint and the CLI, from a read-only peek."""
        con = self.read()
        return {
            "messages": con.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "threads": con.execute("SELECT COUNT(*) FROM channels").fetchone()[0],
        }

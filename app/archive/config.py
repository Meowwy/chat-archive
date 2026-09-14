"""Where to look for an archive, and what to remember about the last one.

This module answers one question - *which archive should this machine use?* - and
answers it as a value, not as state. Nothing here holds an open archive; that is
`archive.Archive`, which takes a `Location` and owns everything downstream of it.
Keeping the two apart is what lets a test build an archive in a temp folder
without disturbing the one you actually use.

The database is *pluggable*: this repository carries no archive of its own, so on
a fresh clone there is simply nothing connected yet. `Archive.create()` makes an
empty one, `remember()` writes the choice to `settings.local.json`, and that file
stays out of version control because it names paths that only make sense here.

Resolution order, highest first:

    1. the ARCHIVE_DB / ARCHIVE_VAULT / ARCHIVE_LEXICON environment variables
    2. settings.local.json, written by `remember()`
    3. nothing - the UI then asks for a database instead of erroring

There is deliberately no built-in default archive location. Nobody's archive is
in a place this file could guess, so guessing would only ever be right on the
machine the guess was written on.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# app/archive/config.py -> app/archive -> app -> project root
APP_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = APP_DIR.parent

# Untracked, and next to the checkout rather than inside it: this is where an
# archive, its media and the compiled dictionary land unless you say otherwise.
ARCHIVES_DIR = PROJECT_ROOT / "Archives"
SETTINGS_FILE = APP_DIR / "settings.local.json"

# What a new archive is called when you do not name it yourself.
NEW_DB_NAME = "chatArchive.sqlite"
VAULT_NAME = "chat_media_vault"

# The Czech dictionary: three source files in the repository, compiled once
# into a lookup database by `py -m archive czech-dict`. The compiled file is
# derived data and stays out of version control.
CZECH_DATA = PROJECT_ROOT / "data" / "czech"
DEFAULT_LEXICON = ARCHIVES_DIR / "czech_lemmas.sqlite"

WEB_BUILD = APP_DIR / "web" / "build"

HOST = os.environ.get("ARCHIVE_HOST", "127.0.0.1")
PORT = int(os.environ.get("ARCHIVE_PORT", "8765"))


class NoDatabase(RuntimeError):
    """Raised when something needs the archive but none is connected."""


@dataclass(frozen=True)
class Location:
    """Everything an archive needs to know about where its parts are kept."""

    db_path: Path
    vault_path: Path
    lexicon_path: Path


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def default_vault_for(db_path: Path) -> Path:
    """Where a freshly connected database keeps its media: right beside it."""
    return Path(db_path).parent / VAULT_NAME


def lexicon_path() -> Path:
    """Where the compiled Czech dictionary is looked for."""
    env = os.environ.get("ARCHIVE_LEXICON")
    return Path(env) if env else DEFAULT_LEXICON


def resolve() -> Location | None:
    """Which archive this machine should use, or None when there is no answer.

    None is not a failure: a fresh clone has no archive, and the UI turns that
    into the "Connect a database" screen rather than an error.
    """
    settings = load_settings()

    env_db = os.environ.get("ARCHIVE_DB")
    if env_db:
        db_path = Path(env_db)
    elif settings.get("db_path"):
        db_path = Path(settings["db_path"])
    else:
        return None

    return Location(db_path, vault_for(db_path, settings), lexicon_path())


def vault_for(db_path: Path | str, settings: dict | None = None) -> Path:
    """Which vault belongs to this database.

    An explicit ARCHIVE_VAULT wins. A database we have connected to before keeps
    the vault it was remembered with, wherever that is - moving an archive must
    not strand its media. Anything else gets a vault beside it.
    """
    db_path = Path(db_path)
    settings = load_settings() if settings is None else settings

    env_vault = os.environ.get("ARCHIVE_VAULT")
    if env_vault:
        return Path(env_vault)

    remembered, seen = settings.get("vault_path"), settings.get("db_path")
    if remembered and seen and Path(seen) == db_path:
        return Path(remembered)
    return default_vault_for(db_path)


def remember(db_path: Path, vault_path: Path) -> None:
    """Use this archive from now on, and next time the app starts."""
    settings = load_settings()
    settings["db_path"] = str(db_path)
    settings["vault_path"] = str(vault_path)
    save_settings(settings)


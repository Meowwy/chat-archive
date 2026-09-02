"""Filesystem locations and server settings.

Every path is resolved once, here, so the rest of the package never guesses.

The database is *pluggable*: this repository carries no archive of its own, so
on a fresh clone there is simply nothing connected yet. `connect_db()` points
the app at a `.sqlite` file (or creates an empty one) and remembers the choice
in `settings.local.json`, which stays out of version control because it names
paths that only make sense on one machine.

Resolution order, highest first:

    1. the ARCHIVE_DB / ARCHIVE_VAULT environment variables
    2. settings.local.json, written by `connect_db()` or `py -m archive db`
    3. the conventional location next to this checkout, if it exists
    4. nothing - the UI then asks for a database instead of erroring
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# app/archive/config.py -> app/archive -> app -> project root
APP_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = APP_DIR.parent

ARCHIVES_DIR = PROJECT_ROOT / "Archives"
SETTINGS_FILE = APP_DIR / "settings.local.json"

# Where an archive lives if you never say otherwise - the layout this project
# grew up in. Used only when the file is actually there.
DEFAULT_DB = ARCHIVES_DIR / "discord_archive_custom.sqlite"
DEFAULT_VAULT = Path(r"D:\4 Archives\chat_media_vault")

# Pre-existing Discord attachment downloads, folded into the vault on migrate.
LEGACY_DISCORD_MEDIA = [
    Path(r"D:\4 Archives\discord_image_archive"),
    ARCHIVES_DIR / "images",
]

PEOPLE_YAML = APP_DIR / "people.yaml"
WEB_BUILD = APP_DIR / "web" / "build"

HOST = os.environ.get("ARCHIVE_HOST", "127.0.0.1")
PORT = int(os.environ.get("ARCHIVE_PORT", "8765"))

PLATFORMS = ("discord", "facebook", "instagram")

# Filled in by _resolve() below. DB_PATH is None when nothing is connected yet.
DB_PATH: Path | None = None
VAULT_DIR: Path = DEFAULT_VAULT


class NoDatabase(RuntimeError):
    """Raised when something needs the archive but none is connected."""


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def default_vault_for(db_path: Path) -> Path:
    """Where a freshly connected database keeps its media."""
    return db_path.parent / "chat_media_vault"


def _resolve() -> None:
    """Recompute DB_PATH and VAULT_DIR from the environment and settings."""
    global DB_PATH, VAULT_DIR
    settings = load_settings()

    env_db = os.environ.get("ARCHIVE_DB")
    if env_db:
        DB_PATH = Path(env_db)
    elif settings.get("db_path"):
        DB_PATH = Path(settings["db_path"])
    elif DEFAULT_DB.is_file():
        DB_PATH = DEFAULT_DB
    else:
        DB_PATH = None

    env_vault = os.environ.get("ARCHIVE_VAULT")
    if env_vault:
        VAULT_DIR = Path(env_vault)
    elif settings.get("vault_path"):
        VAULT_DIR = Path(settings["vault_path"])
    elif DEFAULT_VAULT.is_dir():
        VAULT_DIR = DEFAULT_VAULT
    elif DB_PATH is not None:
        VAULT_DIR = default_vault_for(DB_PATH)
    else:
        VAULT_DIR = DEFAULT_VAULT


_resolve()


def is_connected() -> bool:
    return DB_PATH is not None and DB_PATH.is_file()


def require_db() -> Path:
    """The connected database, or a clear error naming the way out."""
    if DB_PATH is None:
        raise NoDatabase(
            "No archive database is connected. Open the app and use Connect a "
            "database, or run: py -m archive db <path to .sqlite>"
        )
    if not DB_PATH.is_file():
        raise NoDatabase(f"The connected database is missing: {DB_PATH}")
    return DB_PATH


def use(db_path: Path | str, vault_path: Path | str | None = None) -> None:
    """Point this process at a database without writing any settings."""
    global DB_PATH, VAULT_DIR
    DB_PATH = Path(db_path)
    VAULT_DIR = Path(vault_path) if vault_path else default_vault_for(DB_PATH)


def connect_db(db_path: Path | str, vault_path: Path | str | None = None) -> Path:
    """Remember `db_path` as the archive to use, from now on and next time.

    Asking for a database explicitly beats a launch-time ARCHIVE_DB for the rest
    of this process; set that variable again next start if you want it back.
    """
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise NoDatabase(f"No such database file: {path}")
    vault = Path(vault_path).expanduser().resolve() if vault_path else _vault_beside(path)
    settings = load_settings()
    settings["db_path"] = str(path)
    settings["vault_path"] = str(vault)
    save_settings(settings)
    use(path, vault)
    return path


def _vault_beside(db_path: Path) -> Path:
    """Keep an existing vault if it is already the one in use, else co-locate."""
    if DB_PATH is not None and DB_PATH.resolve() == db_path and VAULT_DIR.is_dir():
        return VAULT_DIR
    if db_path == DEFAULT_DB.resolve() and DEFAULT_VAULT.is_dir():
        return DEFAULT_VAULT
    return default_vault_for(db_path)


def forget() -> None:
    """Drop the remembered database (used by tests to restore the default)."""
    settings = load_settings()
    settings.pop("db_path", None)
    settings.pop("vault_path", None)
    if settings:
        save_settings(settings)
    else:
        SETTINGS_FILE.unlink(missing_ok=True)
    _resolve()

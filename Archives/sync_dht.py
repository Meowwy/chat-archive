"""Append a Discord History Tracker file into the archive.

Kept for the habit of running it from this folder; it does nothing the app does
not. The import itself lives in `app/archive/ingest/dht.py`, which is what the
Import page and `py -m archive ingest` use, so there is one implementation and
one set of rules for what counts as a duplicate.

    py sync_dht.py                    # Archives/discord_archive.dht
    py sync_dht.py <path to .dht>     # anywhere else

The target is whichever archive the app is connected to - not necessarily the
file next door. `py -m archive db` prints it.
"""

from __future__ import annotations

import sys
from pathlib import Path

ARCHIVE_DIR = Path(__file__).resolve().parent
DEFAULT_DHT = ARCHIVE_DIR / "discord_archive.dht"

sys.path.insert(0, str(ARCHIVE_DIR.parent / "app"))

from archive import config  # noqa: E402
from archive.ingest import runner  # noqa: E402


def main(argv: list[str]) -> int:
    source = Path(argv[0]) if argv else DEFAULT_DHT
    if not source.is_file():
        print(f"ERROR: source not found: {source}", file=sys.stderr)
        return 2

    try:
        target = config.require_db()
    except config.NoDatabase as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"[sync] source: {source}")
    print(f"[sync] archive: {target}")

    for _kind, stats in runner.ingest_path(source, lambda line: print(line, flush=True)):
        print(f"[sync] new messages: {stats.new_msgs:,}")
        print(f"[sync] attachments stored: {stats.new_media:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

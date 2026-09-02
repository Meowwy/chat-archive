"""Command line entry point.

    py -m archive db [<path>] [--create] show, connect, or create an archive
    py -m archive setup                 migrate + fold local Discord media in
    py -m archive migrate               apply schema changes only
    py -m archive ingest <folder>       ingest a Facebook/Instagram export
    py -m archive discord-media         recover DHT-embedded blobs and downloads
    py -m archive people scaffold|apply|list
    py -m archive stats                 what is in the archive right now
    py -m archive serve                 run the local web viewer
"""

from __future__ import annotations

import argparse
import sys

from pathlib import Path

from . import config, db, migrate
from .ingest import people as people_mod
from .ingest import runner


def _print(message: str) -> None:
    print(message, flush=True)


def cmd_db(args) -> int:
    """Show, connect or create the archive this app reads."""
    if args.path:
        path = Path(args.path).expanduser()
        if args.create:
            migrate.create_archive(path)
        elif not path.is_file():
            _print(f"[db] no such database: {path}")
            _print("[db] to start an empty one:  py -m archive db <path> --create")
            return 1
        else:
            con = db.connect(path)
            try:
                migrate.ensure_schema(con)  # heal an older or DHT-only archive
            finally:
                con.close()
        config.connect_db(path)
        _print(f"[db] connected: {config.DB_PATH}")
        _print(f"[db] vault:     {config.VAULT_DIR}")
        _print(f"[db] remembered in {config.SETTINGS_FILE.name}")
        return 0

    if not config.is_connected():
        _print("[db] no database connected")
        _print("[db] connect one:  py -m archive db <path to .sqlite>")
        _print("[db] or start one: py -m archive db <path to .sqlite> --create")
        return 1
    con = db.connect_ro()
    try:
        total = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        con.close()
    _print(f"[db] {config.DB_PATH}  ({total:,} messages)")
    _print(f"[db] vault: {config.VAULT_DIR}")
    return 0


def cmd_migrate(_args) -> int:
    migrate.migrate()
    return 0


def cmd_discord_media(_args) -> int:
    con = db.connect()
    try:
        migrate.ensure_schema(con)
        stats = runner.ingest_discord_media(con, _print)
    finally:
        con.close()
    _print(
        f"[discord] vault: {stats.new_media} new, {stats.dup_media} already present, "
        f"{stats.missing_media} missing"
    )
    return 0


def cmd_setup(args) -> int:
    cmd_migrate(args)
    return cmd_discord_media(args)


def cmd_ingest(args) -> int:
    results = runner.ingest_path(args.path, _print if args.verbose else None)
    for kind, stats in results:
        _print(
            f"[{kind}] threads {stats.threads_seen} ({stats.new_threads} new) | "
            f"messages {stats.msgs_seen} ({stats.new_msgs} new, {stats.dup_msgs} duplicate) | "
            f"media {stats.media_seen} ({stats.new_media} new, {stats.dup_media} already stored, "
            f"{stats.missing_media} missing)"
        )
        for example in stats.missing_examples:
            _print(f"    missing: {example}")
    return 0


def cmd_people(args) -> int:
    con = db.connect()
    try:
        migrate.ensure_schema(con)
        if args.action == "scaffold":
            path = people_mod.scaffold(con)
            _print(f"[people] mapping file: {path}")
            _print("[people] edit it, then run: py -m archive people apply")
        elif args.action == "apply":
            count, linked = people_mod.apply(con)
            _print(f"[people] {count} people, {linked} identities linked")
        else:
            for row in people_mod.identities(con):
                mark = " " if row["person_id"] else "?"
                _print(
                    f"{mark} {row['platform']:<10} {row['messages']:>7,}  "
                    f"{row['display_name'] or row['name']}"
                )
    finally:
        con.close()
    return 0


def cmd_stats(_args) -> int:
    con = db.connect_ro()
    try:
        _print(f"database: {config.DB_PATH}")
        _print(f"vault:    {config.VAULT_DIR}")
        _print("")
        _print(f"{'platform':<12}{'threads':>9}{'messages':>12}{'senders':>9}")
        for row in con.execute(
            """
            SELECT m.platform,
                   COUNT(DISTINCT m.channel_id) AS threads,
                   COUNT(*)                     AS messages,
                   COUNT(DISTINCT m.sender_id)  AS senders
            FROM messages m GROUP BY m.platform ORDER BY messages DESC
            """
        ):
            _print(
                f"{row['platform']:<12}{row['threads']:>9,}{row['messages']:>12,}{row['senders']:>9,}"
            )
        total = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        _print(f"{'TOTAL':<12}{'':>9}{total:>12,}")
        _print("")
        stored, missing = con.execute(
            """
            SELECT SUM(sha256 IS NOT NULL), SUM(sha256 IS NULL) FROM attachments
            """
        ).fetchone()
        _print(f"attachments: {stored or 0:,} in vault, {missing or 0:,} unavailable")
    finally:
        con.close()
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    if config.is_connected():
        _print(f"[serve] archive: {config.DB_PATH}")
    else:
        _print("[serve] no database connected - the app will ask for one")
    _print(f"[serve] http://{args.host}:{args.port}")
    uvicorn.run("archive.api:app", host=args.host, port=args.port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="archive", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_db = sub.add_parser("db", help="show, connect or create the archive database")
    p_db.add_argument("path", nargs="?", help="database to connect to")
    p_db.add_argument("--create", action="store_true", help="start an empty archive there")
    p_db.set_defaults(func=cmd_db)

    sub.add_parser("migrate", help="apply schema changes").set_defaults(func=cmd_migrate)
    sub.add_parser("setup", help="migrate and fold local Discord media in").set_defaults(func=cmd_setup)
    sub.add_parser("discord-media", help="recover embedded blobs and downloads").set_defaults(
        func=cmd_discord_media
    )
    sub.add_parser("stats", help="show archive contents").set_defaults(func=cmd_stats)

    p_ingest = sub.add_parser("ingest", help="ingest a Facebook/Instagram export folder")
    p_ingest.add_argument("path")
    p_ingest.add_argument("-q", "--quiet", dest="verbose", action="store_false", default=True)
    p_ingest.set_defaults(func=cmd_ingest)

    p_people = sub.add_parser("people", help="cross-platform identity mapping")
    p_people.add_argument("action", choices=["scaffold", "apply", "list"], nargs="?", default="list")
    p_people.set_defaults(func=cmd_people)

    p_serve = sub.add_parser("serve", help="run the local web viewer")
    p_serve.add_argument("--host", default=config.HOST)
    p_serve.add_argument("--port", type=int, default=config.PORT)
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

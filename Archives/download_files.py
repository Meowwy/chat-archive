"""
Download non-image attachments (PDFs, videos, docs, archives, etc.) from
the Discord archive to D:\\4 Archives\\discord_image_archive\\files\\.

Filename format: YYYYMMDD_<message_id>_<idx>.<ext>
  - YYYYMMDD: UTC date of the message timestamp.
  - <idx>:    1-based index across NON-image attachments of the same message,
              ordered by attachment_id ASC (Discord upload order).
  - <ext>:    from original filename, with MIME-based fallback.

Same skip-success / retry-failed semantics as download_images.py.

Run:  py download_files.py
"""

from __future__ import annotations

import argparse
import hashlib
import mimetypes
import os
import re
import sqlite3
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ARCHIVE_DIR = Path(__file__).resolve().parent
MIRROR_DB = ARCHIVE_DIR / "discord_archive_custom.sqlite"

EXTERNAL_ROOT = Path(r"D:\4 Archives\discord_image_archive")
FILES_DIR = EXTERNAL_ROOT / "files"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DiscordArchive/1.0"
TIMEOUT_SECS = 60   # files can be larger than images
MAX_RETRIES = 3
RETRY_BACKOFF = 1.5
WORKERS = 4         # fewer workers for files; they're bigger

_safe_ext = re.compile(r"[^A-Za-z0-9]")


@dataclass
class Job:
    attachment_id: int
    message_id: int
    channel_id: int
    name: str
    url: str
    type_: str | None
    timestamp_ms: int
    idx: int


@dataclass
class Result:
    job: Job
    file_id: str
    local_path: str
    status: str
    http_status: int | None
    file_size: int | None
    sha256: str | None
    error: str | None


def derive_extension(name: str | None, mime_type: str | None) -> str:
    if name:
        base, dot, ext = name.rpartition(".")
        if dot and ext and len(ext) <= 8:
            ext = _safe_ext.sub("", ext).lower()
            if ext:
                return "." + ext
    if mime_type:
        primary = mime_type.split(";", 1)[0].strip()
        guessed = mimetypes.guess_extension(primary)
        if guessed:
            return guessed.lower()
    return ".bin"


def utc_yyyymmdd(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).strftime("%Y%m%d")


def build_file_id(job: Job) -> str:
    return f"{utc_yyyymmdd(job.timestamp_ms)}_{job.message_id}_{job.idx}"


def fetch_jobs(con: sqlite3.Connection) -> list[Job]:
    """Non-image attachments needing download.

    Index is 1-based, partitioned per message, ordered by attachment_id ASC,
    among non-image attachments only.
    """
    rows = con.execute(
        """
        WITH file_attachments AS (
            SELECT a.attachment_id, ma.message_id, m.channel_id, a.name,
                   COALESCE(a.download_url, a.normalized_url) AS url, a.type,
                   m.timestamp,
                   ROW_NUMBER() OVER (PARTITION BY ma.message_id ORDER BY a.attachment_id) AS idx
            FROM attachments a
            JOIN message_attachments ma ON ma.attachment_id = a.attachment_id
            JOIN messages m ON m.message_id = ma.message_id
            WHERE a.type IS NULL OR a.type NOT LIKE 'image/%'
        )
        SELECT f.attachment_id, f.message_id, f.channel_id, f.name, f.url, f.type, f.timestamp, f.idx
        FROM file_attachments f
        LEFT JOIN downloaded_files d ON d.attachment_id = f.attachment_id
        WHERE d.attachment_id IS NULL OR d.status = 'failed'
        ORDER BY f.timestamp DESC, f.attachment_id ASC
        """
    ).fetchall()
    return [Job(*r) for r in rows]


def download_one(job: Job, ssl_ctx: ssl.SSLContext) -> Result:
    file_id = build_file_id(job)
    ext = derive_extension(job.name, job.type_)
    target_dir = FILES_DIR / str(job.channel_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{file_id}{ext}"
    tmp = target.with_suffix(target.suffix + ".part")

    rel_path = str(target.relative_to(EXTERNAL_ROOT)).replace("\\", "/")

    last_err: str | None = None
    last_http: int | None = None
    backoff = RETRY_BACKOFF

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(job.url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECS, context=ssl_ctx) as resp:
                last_http = getattr(resp, "status", 200)
                hasher = hashlib.sha256()
                size = 0
                with open(tmp, "wb") as f:
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        hasher.update(chunk)
                        size += len(chunk)
            os.replace(tmp, target)
            return Result(
                job=job,
                file_id=file_id,
                local_path=rel_path,
                status="success",
                http_status=last_http,
                file_size=size,
                sha256=hasher.hexdigest(),
                error=None,
            )
        except urllib.error.HTTPError as e:
            last_http = e.code
            last_err = f"HTTP {e.code} {e.reason}"
            if 400 <= e.code < 500:
                break
        except urllib.error.URLError as e:
            last_err = f"URLError: {e.reason}"
        except (TimeoutError, ssl.SSLError, OSError) as e:
            last_err = f"{type(e).__name__}: {e}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"

        if attempt < MAX_RETRIES:
            time.sleep(backoff)
            backoff *= 2

    try:
        if tmp.exists():
            tmp.unlink()
    except OSError:
        pass

    return Result(
        job=job,
        file_id=file_id,
        local_path=rel_path,
        status="failed",
        http_status=last_http,
        file_size=None,
        sha256=None,
        error=last_err,
    )


def upsert_result(con: sqlite3.Connection, r: Result) -> None:
    now = int(time.time() * 1000)
    con.execute(
        """
        INSERT INTO downloaded_files
            (attachment_id, file_id, message_id, channel_id, source_url, local_path,
             mime_type, status, http_status, file_size, sha256, error,
             attempt_count, last_attempt_at, downloaded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(attachment_id) DO UPDATE SET
            file_id         = excluded.file_id,
            source_url      = excluded.source_url,
            local_path      = CASE WHEN excluded.status = 'success'
                                   THEN excluded.local_path
                                   ELSE downloaded_files.local_path END,
            mime_type       = excluded.mime_type,
            status          = excluded.status,
            http_status     = excluded.http_status,
            file_size       = COALESCE(excluded.file_size, downloaded_files.file_size),
            sha256          = COALESCE(excluded.sha256, downloaded_files.sha256),
            error           = excluded.error,
            attempt_count   = downloaded_files.attempt_count + 1,
            last_attempt_at = excluded.last_attempt_at,
            downloaded_at   = COALESCE(excluded.downloaded_at, downloaded_files.downloaded_at)
        """,
        (
            r.job.attachment_id,
            r.file_id,
            r.job.message_id,
            r.job.channel_id,
            r.job.url,
            r.local_path if r.status == "success" else None,
            r.job.type_,
            r.status,
            r.http_status,
            r.file_size,
            r.sha256,
            r.error,
            now,
            now if r.status == "success" else None,
        ),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=WORKERS)
    args = ap.parse_args()

    if not MIRROR_DB.exists():
        print(f"ERROR: mirror DB not found: {MIRROR_DB}", file=sys.stderr)
        print("       run sync_dht.py first.", file=sys.stderr)
        return 2

    if not EXTERNAL_ROOT.parent.exists():
        print(f"ERROR: external drive root not available: {EXTERNAL_ROOT.parent}", file=sys.stderr)
        return 2
    FILES_DIR.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(str(MIRROR_DB))
    try:
        jobs = fetch_jobs(con)
    finally:
        con.close()

    if args.limit:
        jobs = jobs[: args.limit]

    if not jobs:
        print("[files] nothing to do -- all non-image attachments are already downloaded.")
        return 0

    print(f"[files] {len(jobs):,} file(s) to attempt -> {FILES_DIR}")
    print(f"[files] workers={args.workers}")
    ssl_ctx = ssl.create_default_context()

    write_con = sqlite3.connect(str(MIRROR_DB))
    write_con.execute("PRAGMA journal_mode=WAL")
    write_lock = threading.Lock()

    succ = fail = 0
    bytes_written = 0
    t0 = time.time()
    last_commit = time.time()

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = [ex.submit(download_one, j, ssl_ctx) for j in jobs]
            for i, fut in enumerate(as_completed(futures), 1):
                r = fut.result()
                with write_lock:
                    upsert_result(write_con, r)
                    if r.status == "success":
                        succ += 1
                        bytes_written += r.file_size or 0
                    else:
                        fail += 1
                    if time.time() - last_commit > 2.0:
                        write_con.commit()
                        last_commit = time.time()

                if i % 5 == 0 or i == len(jobs):
                    elapsed = time.time() - t0
                    rate = i / elapsed if elapsed else 0
                    print(
                        f"[files] {i}/{len(jobs)}  ok={succ}  fail={fail}  "
                        f"{bytes_written/1e6:.1f} MB  {rate:.1f}/s",
                        flush=True,
                    )
        write_con.commit()
    finally:
        write_con.close()

    print(f"[files] done in {time.time() - t0:.1f}s -- success={succ} failed={fail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# Discord Archive — backup workflow

This folder contains a custom mirror of my Discord History Tracker (DHT) archive plus scripts to back up message attachments to my external drive.

- **Source (read-only, written by the DHT app):** `discord_archive.dht` (+ `-wal`, `-shm`)
- **The archive:** `discord_archive_custom.sqlite` — append-only copy of all DHT data plus the Facebook and Instagram imports, safe to query, customize, and back up.
- **Attachment archive on external drive:** `D:\4 Archives\discord_image_archive\`
  - `images\<channel_id>\YYYYMMDD_<msg_id>_<idx>.<ext>`
  - `files\<channel_id>\YYYYMMDD_<msg_id>_<idx>.<ext>`

The filename stem (`YYYYMMDD_<msg_id>_<idx>`) is also stored in the DB as `image_id` / `file_id` so attachments can be joined back to messages.

## To update the backup, run (in this order)

```
py sync_dht.py
py download_images.py
py download_files.py
```

### What each step does

1. **`sync_dht.py`** — A wrapper around the app's importer (`py -m archive ingest <file.dht>`, or the **Import** page). Takes a safe online-backup snapshot of the live `.dht` and **appends new rows** into whichever archive the app is connected to. Existing rows are never overwritten or deleted, so nothing can be lost even if DHT later loses or rewrites it. Idempotent: running it again with no new messages does nothing, and it now stores whatever attachments DHT embedded into the media vault as it goes.

2. **`download_images.py`** — Downloads every image attachment (`type LIKE 'image/%'`) referenced in the mirror to `D:\4 Archives\discord_image_archive\images\`. Successes are skipped on re-runs; failures (e.g. expired Discord URLs) are retried automatically. Tracks every attempt in the `downloaded_images` table.

3. **`download_files.py`** — Same as above for **non-image** attachments (PDFs, videos, docs, archives, etc.) into `D:\4 Archives\discord_image_archive\files\`. Tracks results in the `downloaded_files` table.

## Notes

- The external drive `D:\` must be mounted before running the download scripts; they fail fast otherwise.
- Discord CDN URLs are signed and expire after ~24h. Old attachments will return HTTP 404 — that's expected. They'll keep getting retried on each run in case the URL ever works again.
- Querying the mirror in TablePlus: open `discord_archive_custom.sqlite` and use the same SQL as before. New helper columns: `downloaded_images.image_id`, `downloaded_files.file_id`, plus `local_path` for joining back to the on-disk file.

## Useful queries

```sql
-- progress overview
SELECT 'images' AS kind, status, COUNT(*) FROM downloaded_images GROUP BY status
UNION ALL
SELECT 'files', status, COUNT(*) FROM downloaded_files GROUP BY status;

-- last sync runs
SELECT run_id,
       datetime(started_at/1000, 'unixepoch') AS started,
       new_messages, new_attachments, new_blobs
FROM sync_log ORDER BY run_id DESC LIMIT 10;

-- messages with their downloaded images
SELECT datetime(m.timestamp/1000, 'unixepoch') AS sent,
       u.display_name, m.text, i.image_id, i.local_path
FROM messages m
JOIN users u ON u.id = m.sender_id
JOIN downloaded_images i ON i.message_id = m.message_id
WHERE i.status = 'success'
ORDER BY m.timestamp DESC LIMIT 50;
```

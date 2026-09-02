# Chat Archive

Your Discord, Facebook Messenger and Instagram conversations, in one place, on your own machine.

Chat exports are unreadable by design: a folder of JSON files per service, each with its own
shape, its own idea of who people are, and media scattered across subfolders. This app ingests
them into a single SQLite database and serves the whole history in a browser — one searchable
timeline, whoever you were talking to and wherever you were talking to them.

- **One database.** Every platform lands in the same tables, told apart by a `platform` column.
- **Safe to re-run.** Meta exports repeat the last few months every time; already-imported
  messages are skipped and already-stored media is not copied again.
- **Search that works in Czech.** Full-text over every message, diacritics-insensitive —
  `necekal` finds `nečekal`.
- **One person, many accounts.** Link someone's Discord, Facebook and Instagram identities and
  give them a name; that name is what the whole app shows.
- **Nothing leaves the machine.** No cloud, no CDN, no telemetry. The archive is a file you own,
  and it is never part of this repository.

## Setup

Requires Python 3.11+ and Node 18+.

```
git clone https://github.com/Meowwy/chat-archive.git
cd chat-archive/app
pip install -r requirements.txt
cd web && npm install && npm run build && cd ..

py -m archive serve          # http://127.0.0.1:8765
```

The first run opens on **Connect a database**, because the archive lives wherever you keep it.
Choose **Start an empty archive…**, pick a location, and you have somewhere to import into. If
you already have one, point the app at that file instead. The choice is remembered.

## Getting your exports

### Facebook Messenger

Go to [facebook.com](https://facebook.com) → click your profile picture → **Settings & privacy**
→ **Settings** → **Accounts Center** in the left sidebar → **Your information and permissions** →
**Export your information** → **Create export** → select **Messages** only, and export.

### Instagram

Go to [instagram.com](https://instagram.com) → **More** in the bottom-left menu → **Settings** →
then exactly as for Facebook Messenger above.

> **Choose JSON, not HTML**, and a date range of all time. The importer reads Meta's JSON format;
> the HTML export cannot be ingested. Meta emails you a download link, usually within a few hours.
> Unzip it and keep the folder — you will point the app at it once.

### Discord

Discord has no official export, so use **[Discord History Tracker](https://dht.chylex.com/)** — a
free, open-source tool that saves your history to a `.dht` file (SQLite) as you browse. In its
settings, turn **attachment downloading on**: Discord's CDN links are signed and expire about a
day after they are issued, so anything not downloaded while it is fresh is gone for good.

To bring that into the archive, put the tracker's file in this project's `Archives/` folder as
`discord_archive.dht` and run:

```
cd Archives
py sync_dht.py                # appends new messages into discord_archive_custom.sqlite
```

Connect that `Archives/discord_archive_custom.sqlite` in the app and it becomes your archive —
import your Meta exports into the same file. `py -m archive discord-media` afterwards recovers
any attachments the tracker embedded. Re-run `sync_dht.py` whenever you scrape more; it only ever
appends.

## Importing an export

Open the **Import** page, press **Choose a folder…**, pick the unzipped export folder, check what
was detected, and import. Or from a terminal:

```
py -m archive ingest "C:/path/to/your_instagram_activity"
```

Either way it is safe to re-run. Once imported, the media has been copied into the archive's
vault, so you can delete the export folder.

## Then

Browse conversations, jump around 100k-message threads by month, search everything at once, and
use the **People** page to tie one person's accounts together under a name of your choosing.

Full write-up of the schema, the deduplication, the encoding repair and everything else:
**[DOCUMENTATION.md](DOCUMENTATION.md)**.

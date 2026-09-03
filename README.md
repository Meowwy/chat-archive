# Chat Archive

Your Discord, Facebook Messenger and Instagram conversations, in one place, on your own machine.

Chat exports are unreadable by design: a folder of JSON files per service, each with its own
shape, its own idea of who people are, and media scattered across subfolders. This app ingests
them into a single SQLite database and serves the whole history in a browser — one searchable
timeline, whoever you were talking to and wherever you were talking to them.

- **One database.** Every platform lands in the same tables, told apart by a `platform` column.
- **Safe to re-run.** Meta exports repeat the last few months every time; already-imported
  messages are skipped and already-stored media is not copied again.
- **Search that works in Czech.** Full-text over every message, diacritics-insensitive, and
  aware of Czech inflection: search `hospoda` and you also get `hospody`, `hospodě`, `hospodu`.
  Combine words with `OR`, `-` and brackets.
- **One person, many accounts.** Link someone's Discord, Facebook and Instagram identities and
  give them a name; that name is what the whole app shows.
- **Words over time.** Pick a person and see every message they exchanged as a line, month by
  month. Type a word and the line becomes the months they used it — in every form, the same
  search, counted instead of listed. Pick up to ten people to compare them on one chart.
- **Nothing leaves the machine.** No cloud, no CDN, no telemetry. The archive is a file you own,
  and it is never part of this repository.

## Setup

Requires Python 3.11+ and Node 18+.

```
git clone https://github.com/Meowwy/chat-archive.git
cd chat-archive/app
pip install -r requirements.txt
cd web && npm install && npm run build && cd ..

py -m archive czech-dict     # build the Czech dictionary search widens words with
py -m archive serve          # http://127.0.0.1:8765
```

`czech-dict` takes about fifteen seconds and writes a 149 MB file next to your archive. Skip it
and everything still works — search just matches words literally instead of in every inflected
form. The dictionary's licence is non-commercial; see `data/czech/NOTICE.md`.

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

The `.dht` file it writes is imported like any other export — leave it wherever the tracker keeps
it. Re-import it whenever you have scraped more; only new messages are added.

## Importing

Open the **Import** page and press **Choose a folder…** for an unzipped Facebook or Instagram
export, or **Choose a .dht file…** for Discord. Check what was detected, and import. Or from a
terminal:

```
py -m archive ingest "C:/path/to/your_instagram_activity"
py -m archive ingest "C:/path/to/discord_archive.dht"
```

Either way it is safe to re-run: anything already in the archive is skipped. Attachments come
along with the messages — for Discord that means whatever the tracker managed to embed, which is
why turning its downloading on matters. Once imported, media has been copied into the archive's
vault, so you can delete the export folder.

## Then

Browse conversations, jump around 100k-message threads by month, search everything at once, use
the **People** page to tie one person's accounts together under a name of your choosing, and open
**Stats** to see how much — and how often a particular word — was said over the years.

Full write-up of the schema, the deduplication, the encoding repair and everything else:
**[DOCUMENTATION.md](DOCUMENTATION.md)**.

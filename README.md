# Chat Archive

Read and search your Discord, Instagram, Facebook Messenger and Microsoft Teams conversations in one place, **offline on device**.

## What is this tool for:

- It processes your exported chat conversations including media and creates local sqlite database.
- Then enables to view the content of the local database and the saved media in browser.
- Has full-text search and the ability to link conversations with one person across platforms.
- Simple statistics for received / sent messages and for used words.

Technical notes of my solution:

- **One database.** Every platform lands in the same tables, told apart by a `platform` column.
- **Safe to re-run.** Already-imported messages are skipped and already-stored media is not copied again.
- **Search that works in Czech.** Full-text over every message, diacritics-insensitive, and
  aware of Czech inflection: search `hospoda` and you also get inflected forms like `hospody`, `hospodě`, `hospodu`.
  More complex queries supported (same logic as in Google search: `OR`, `-` and brackets).
- **One person, many accounts.** Link someone's Discord, Facebook, Instagram and Teams identities
  and give them a name; that name is what the whole app shows.
- **Words over time.** Pick a person and see every message they exchanged as a line, month by
  month. Type a word and the line becomes the months they used it — in every form, the same
  search, counted instead of listed. Pick up to ten people to compare them on one chart.
- **Nothing leaves the machine.** No cloud, no CDN, no telemetry. The archive is a file you own.

## Setup

Requires Python 3.11+ and Node 18+.

```
git clone https://github.com/Meowwy/chat-archive.git
cd chat-archive/app
pip install -r requirements.txt
cd web && npm install && npm run build && cd ..

py -m archive czech-dict     # build the Czech dictionary search widens words with - only if you need it
py -m archive serve          # http://127.0.0.1:8765
```

`czech-dict` takes about fifteen seconds and writes a 149 MB file next to your archive.

## Getting your exports

Messenger needs **two** downloads, and they are not interchangeable. Facebook's own export
carries group and community chats; your private one-to-one chats are encrypted end to end, so Meta
cannot read them and cannot put them in that file. They come from Messenger separately.

### Facebook

Go to [facebook.com](https://facebook.com) → click your profile picture → **Settings & privacy**
→ **Settings** → **Accounts Center** in the left sidebar → **Your information and permissions** →
**Export your information** → **Create export** → select **Messages** only, and export.

What arrives is group and community chats — the ones encryption does not cover. For everything
else, keep going.

### Messenger end-to-end encrypted chats

Go to [messenger.com](https://www.messenger.com) → click your profile at the **bottom left** →
**Privacy & safety** → **End-to-end encrypted chats** → **Message storage** → **Download message
storage data** → set it up and download. Straight there:
[messenger.com/secure_storage/dyi](https://www.messenger.com/secure_storage/dyi)

This one unzips to a flat `messages/` folder of one JSON per conversation, beside a `media/`
folder. **Keep them side by side** — the JSON points at `./media/...`, so the importer needs the
two together. Point the app at the `messages` folder, or at the folder holding both.

These are still Facebook conversations, so they are imported as Facebook and merge with the export
above: someone you know from a group chat stays one identity, not two.

> This only works if message storage was turned on before the chats you want. It is what lets Meta
> keep a copy it can hand back; without it there is nothing on their side to export.

### Instagram

Go to [instagram.com](https://instagram.com) → **More** in the bottom-left menu → **Settings** →
then exactly as for Facebook above.

> **Choose JSON, not HTML**, and a date range of all time. The importer reads Meta's JSON format;
> the HTML export cannot be ingested. Meta emails you a download link, usually within a few hours.
> Unzip it and keep the folder — you will point the app at it once.

### Microsoft Teams

In MS Teams, click on your **profile picture at the top right** → **My Microsoft account** →
**Privacy** tab at the left → scroll down to the section **Find privacy settings in Microsoft
products** → click on **Teams** → set the export and confirm. Direct link:
[teams.live.com/dataexport](https://teams.live.com/dataexport). There is also a video walkthrough:
[youtu.be/hsB08IcyjD8](https://youtu.be/hsB08IcyjD8?si=L3nyDUnihFUMsFrF).

The export is a single **`.tar`** folder. You do not have to unpack it, just point the app straight at the `.tar` and it reads what it needs from inside.

> Microsoft Teams export does not include everything.
> It omits voice messages and ordinary files such as PDFs and spreadsheets.

### Discord

Discord export only exports your messages from the DMs and channels you interacted in, so it is only useful to get the working media links for scraping them.

To get all messages from your DMs, use **[Discord History Tracker](https://dht.chylex.com/)** — a
free, open-source tool that saves your history to a `.dht` file (SQLite) as you browse. Unfortunately images and file stored on discord servers are unreachable by this scraping tool, Discord's CDN links are signed and expire in several hours after they are issued, so anything not downloaded while it is fresh is unreachable from outside the official app and web application.

The `.dht` file it writes is imported like any other export — leave it wherever the tracker keeps
it. Re-import it whenever you have scraped more; only new messages are added.

You can then use the tools in `/tools` directory of this project to scrape at least your media from the official export.

## Importing

Open the **Import** page and press **Choose a folder…** for an **unzipped** Facebook, Instagram,
encrypted-Messenger or Teams export, or **Choose a file…** for Discord's `.dht` or a Teams `.tar`.
Check what was detected, and import. Or from a terminal:

```
py -m archive ingest "C:/path/to/your_instagram_activity"
py -m archive ingest "C:/path/to/your_facebook_activity"
py -m archive ingest "C:/path/to/messages"          # encrypted Messenger chats
py -m archive ingest "C:/path/to/teams_export.tar"  # or the folder, if you unpacked it
py -m archive ingest "C:/path/to/discord_archive.dht"
```

It is safe to re-run: anything already in the archive is skipped.
Once imported, media has been copied into the archive's vault, so you can delete the export folder.

## Where the archive lives

An archive consists of two things:

- **the database** — one `.sqlite` file holding every message, person and conversation
- **the media vault** — a folder holding every image, video, voice message and avatar, each file
  named after the hash of its own contents and filed in one of 256 subfolders

When you start an archive, the vault is created beside the database, so the whole thing is one
folder you can pick up and carry:

```
MyArchive/
  chatArchive.sqlite      the database
  chat_media_vault/       the media, in folders 00 … ff
```

Which archive this machine uses is remembered in `app/settings.local.json` — two paths and nothing
else. It is deliberately untracked, because those paths only make sense on the machine that wrote
them.

### Backing it up, or moving it to another drive

Copy **both** parts, and copy them together:

1. **Stop the server first.** The database runs in WAL mode, so messages that are already saved can
   still be sitting in the `.sqlite-wal` file next to it. Copying the `.sqlite` on its own while the
   app is running can leave them behind. Either shut the server down, or copy the `.sqlite`, `-wal`
   and `-shm` files as a set.
2. **Copy the database and the vault from the same moment.** Messages point into the vault by
   content hash, so a database newer than its vault gives you messages whose media is missing. The
   other way round is harmless — a vault ahead of the database just holds a few unused files.

Old copies of the database are worth keeping for a while, but they are complete archives in their
own right rather than increments — each one is as large as the archive was on the day it was made.

### Reconnecting after a fresh clone

Point the app at the `.sqlite` file:

```
py -m archive db "D:/Backups/MyArchive/chatArchive.sqlite"
```

or run `py -m archive serve`, open **Connect a database** and press **Choose an existing archive…**.
Either way the choice is written to `app/settings.local.json`, and the app opens that archive from
then on.

If the vault sits beside the database, as it does by default, it is found along with it and there is
nothing else to do. **If you keep the vault somewhere else** — a different drive, say — connecting
looks for it beside the database and finds nothing, so name it yourself. Either set the environment
variable:

```
set ARCHIVE_VAULT=E:/media/chat_media_vault     # Windows; export on macOS and Linux
```

or write `app/settings.local.json` by hand before starting the app:

```json
{
  "db_path": "D:/Backups/MyArchive/chatArchive.sqlite",
  "vault_path": "E:/media/chat_media_vault"
}
```

Three environment variables override the remembered settings whenever they are set: `ARCHIVE_DB`,
`ARCHIVE_VAULT` and `ARCHIVE_LEXICON`. They are handy for opening a second archive once without
disturbing the one you normally use.

Full write-up of the schema, the deduplication, the encoding repair and everything else:
**[DOCUMENTATION.md](DOCUMENTATION.md)**.

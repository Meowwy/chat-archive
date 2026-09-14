# Chat Archive

Read and search your Discord, Facebook Messenger, Instagram and Microsoft Teams conversations in one
place, **offline on device**.

## What is this tool for:

- It processes your exported chat conversations and creates local sqlite database.
- Then enables to view the content of the local database in browser.
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

Microsoft emails a link when the export is ready, and what downloads is a single **`.tar`** holding
`messages.json` beside a flat `media/` folder. **You do not have to unpack it.** These exports get
large — mostly video — and unpacking costs that much disk again before a single message reaches the
archive, so point the app straight at the `.tar` and it reads what it needs from inside. If you have
already unpacked it, pick the folder instead; both work and import identically.

> Teams does not put everything in the download. Some attachments are referenced by the messages
> and their bytes are simply absent: voice messages, ordinary files such as PDFs and spreadsheets,
> and a share of the older photos and videos. Those show as an unavailable attachment, with the
> message text completely intact.

### Discord

Discord has no official export, so use **[Discord History Tracker](https://dht.chylex.com/)** — a
free, open-source tool that saves your history to a `.dht` file (SQLite) as you browse. Unfortunately images and file stored on discord servers are unreachable by this scraping tool, Discord's CDN links are signed and expire in several hours after they are issued, so anything not downloaded while it is fresh is gone for good.

The `.dht` file it writes is imported like any other export — leave it wherever the tracker keeps
it. Re-import it whenever you have scraped more; only new messages are added.

## Importing

Open the **Import** page and press **Choose a folder…** for an unzipped Facebook, Instagram,
encrypted-Messenger or Teams export, or **Choose a file…** for Discord's `.dht` or a Teams `.tar`.
Check what was detected, and import. Or from a terminal:

```
py -m archive ingest "C:/path/to/your_instagram_activity"
py -m archive ingest "C:/path/to/your_facebook_activity"
py -m archive ingest "C:/path/to/messages"          # encrypted Messenger chats
py -m archive ingest "C:/path/to/teams_export.tar"  # or the folder, if you unpacked it
py -m archive ingest "C:/path/to/discord_archive.dht"
```

Either way it is safe to re-run: anything already in the archive is skipped. Once imported, media has been copied into the archive's
vault, so you can delete the export folder.

Full write-up of the schema, the deduplication, the encoding repair and everything else:
**[DOCUMENTATION.md](DOCUMENTATION.md)**.

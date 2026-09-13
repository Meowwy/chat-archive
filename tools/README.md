# Tools

Things you run **after** an import, by hand, when you happen to have something the
normal pipeline cannot get. Nothing here is part of `py -m archive ingest`, and nothing
here is needed to use the archive — each one is an occasional rescue job.

Every tool works on the **connected archive** (`py -m archive db` says which one), so
connect the right database first. They are all safe to interrupt and safe to re-run:
they only ever add what is missing.

---

## `discord_export_media.py` — get your Discord attachments back

Discord CDN links are signed and expire about a day after they are issued, so almost
every attachment a Discord History Tracker scrape recorded is now a dead link — 9,740 of
9,805 images in this author's archive returned HTTP 404. Those bytes looked gone for
good.

They are not, for one half of them. Discord's **official data package** hands out the
same attachments under links signed with `ex=0` — *no expiry* — and they still work
years later. This tool downloads them into the media vault and attaches them to the
messages they belong to, so a conversation that showed grey placeholder cards shows the
actual images again.

The catch: **the data package only contains messages you sent.** It can restore your
half of every conversation and nothing of theirs. There is no fix for that; the other
side's attachments stay unavailable.

### Getting the package

Discord → **User Settings** → **Data & Privacy** → **Request all of my data** → tick
**Messages** → request it. It arrives by email as a zip, usually within a few days.

Unzip it. Inside is a `Messages` folder (localised — `Zprávy` in Czech, and so on) with
one folder per conversation named `c<channel id>`, each holding `channel.json` and
`messages.json`.

### Running it

```
py -m archive ingest "C:/path/to/discord_archive.dht"   # scrape first, as usual
py tools/discord_export_media.py "C:/path/to/discord_package" --dry-run
py tools/discord_export_media.py "C:/path/to/discord_package"
```

Point it at the unzipped package or at the `Messages` folder inside it — either works.
Do the `--dry-run` first: it prints exactly what it found and would fetch, and writes
nothing.

| flag | |
|---|---|
| `--dry-run` | report what would happen, touch nothing |
| `--workers N` | parallel downloads, default 8 |
| `--limit N` | stop after N downloads — a good way to try it on a handful first |
| `--report PATH` | where the run report goes (default: beside the archive database) |

Import the `.dht` file **before** running this. The tool fills in attachments for
messages the archive already holds, and adds the ones the export has that the scrape
missed — but a conversation the archive does not know about at all is skipped, not
created.

### How it knows which chat is which

Nothing is matched by name or by guesswork. The export folder is called `c<channel id>`
and that id *is* the archive's `channels.id`, because both come from Discord. Each
attachment URL carries its own snowflake the same way —
`/attachments/<channel>/<attachment>/<name>` — and that is the archive's
`attachments.attachment_id`.

Before touching a conversation the tool still checks the two agree: the export must call
it a DM, and it must share at least one message id with the channel in the archive.
It also refuses to run if the package belongs to a different Discord account than the
one marked as you on the **People** page.

### What it writes

- **The vault** — every downloaded file, addressed by sha256, stored once.
- **`attachments`** — existing rows get their `local_path` and `sha256`, and their dead
  `download_url` is replaced with the link that works. Attachments the scrape never saw
  get a whole new row, with the size and dimensions read out of the file itself.
- **`message_attachments`** — the link from the attachment to its message.
- **`messages`** — the messages the export has and the scrape missed, sent by you, in
  channels the archive already holds. The search index follows automatically.

The one thing the export is worse at than the scrape: it writes timestamps to the second,
not the millisecond. So a message that only the export has is stored a fraction of a
second early. Messages the archive already had keep their scraped timestamp — they are
never rewritten.

### Re-running it

It is idempotent. Run it twice and the second run says `nothing to do`:

- bytes already in the vault are not fetched again,
- an `attachments` row that already has a `local_path` is left alone,
- every insert is `OR IGNORE`, on a primary key that comes from Discord,
- the `download_url` is only rewritten when it actually changed.

It also *converges* rather than merely refusing to repeat itself. Anything missing is
picked up on the next run: a failed download, a conversation the export covers but the
scrape had not reached yet, a link whose attachment was already in the vault, even a
vault file deleted from underneath its row — that one is downloaded and stored again.

So stopping it with Ctrl-C costs nothing but the batch in flight, and there is no state
to clean up before starting over. The run report lists every failure with its URL.

### Back up first

It writes to your archive. There is no undo:

```
py -m archive migrate     # copies the database to <name>.sqlite.bak-<timestamp>
```

### What it did here

4,167 attachments across 9 conversations, 1.77 GB, four minutes, zero failures — some of
them from 2021. Discord attachments in the vault went from 67 to 4,239. Every one still
missing belongs to the other person.

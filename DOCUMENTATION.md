# Documentation

How the archive is built and why it works the way it does. For getting started, see
[README.md](README.md).

| Platform | Source |
|---|---|
| Discord | Discord History Tracker, mirrored by `Archives/sync_dht.py` |
| Instagram | official export JSON |
| Facebook | official export JSON |

The archive itself — the database, the media vault and the exports — is **not** in this
repository. It is personal data, and the app is built to be handed one.

## First run

```
cd app
pip install -r requirements.txt
cd web && npm install && npm run build && cd ..

py -m archive serve          # open http://127.0.0.1:8765
```

With no database connected the app opens on **Connect a database**, which offers two things:
point it at an existing `.sqlite` archive, or create an empty one and import your exports into
it. The same job from a terminal:

```
py -m archive db C:/archives/chat_archive.sqlite --create   # start an empty archive
py -m archive db C:/archives/chat_archive.sqlite            # connect to an existing one
py -m archive db                                            # show what is connected
```

The choice is remembered in `app/settings.local.json` (untracked). `ARCHIVE_DB` and
`ARCHIVE_VAULT` override it, which is how you keep several archives side by side. A database
made by an older version — or a raw Discord History Tracker mirror — is upgraded in place when
you connect it, so nothing needs converting by hand.

## Everyday use

```
py -m archive serve          # open http://127.0.0.1:8765
```

To pull in a new export, use the **Import** page in the browser — pick the folder, check what
was detected, import. Or from the command line:

```
py -m archive ingest "D:\path\to\your_instagram_activity"
```

Either way it is safe to re-run: messages already in the archive are skipped, and media that is
already stored is not copied again.

To pull in new Discord messages, run the existing backup workflow in `Archives/` (see
`Archives/README.md`), then optionally `py -m archive discord-media`.

## Commands

| Command | What it does |
|---|---|
| `py -m archive serve` | Run the web viewer |
| `py -m archive db [<path>] [--create]` | Show, connect or create the archive database |
| `py -m archive ingest <folder>` | Ingest a Facebook/Instagram export |
| `py -m archive stats` | What is in the archive right now |
| `py -m archive migrate` | Apply schema changes (backs the DB up first) |
| `py -m archive discord-media` | Recover Discord attachments that exist locally |
| `py -m archive setup` | `migrate` + `discord-media` |
| `py -m archive people list\|scaffold\|apply` | Cross-platform identity mapping |
| `py smoke_test.py` | End-to-end checks (68 assertions) |

## How it fits together

```
Archives/discord_archive.dht          live DHT file, read-only
        |  sync_dht.py  (append-only)
        v
Archives/discord_archive_custom.sqlite    <-- the one database
        ^
        |  py -m archive ingest
your_facebook_activity/ , your_instagram_activity/

D:\4 Archives\chat_media_vault\       content-addressed media
        ^
        |  every image/video/file, copied once, addressed by sha256
```

Everything lives in **one** database. Facebook and Instagram messages go into the same
`messages`, `channels` and `users` tables as Discord, distinguished by a `platform` column.

### Why Meta ids are negative

Discord snowflakes are always positive. Facebook and Instagram exports contain no message ids at
all, so the ingest mints its own: a 62-bit hash of the message's content, **negated**. A Meta id
therefore *cannot* collide with a Discord id — it is structurally impossible, not just unlikely.

Ids cross the HTTP boundary as **strings**. They are 62–63 bit integers and JavaScript numbers
lose precision above 2^53, so `JSON.parse` would silently corrupt them.

### How duplicate detection works

Meta exports have no message ids and each new export repeats everything from the last few months.
Identity is therefore derived from the message itself:

```
source_key = platform | thread_path | sender | timestamp_ms | sha1(text + media uris + link)
```

Verified unique across every message in the exports it was built against (6,792 of them), with
zero collisions. The message id is a hash of that key, so the primary key does the deduplication and re-importing an
overlapping export is a genuine no-op.

### Text encoding

Meta exports are mojibake — `Dobrá zpráva, můžeš` is written as `DobrÃ¡ zprÃ¡va, mÅ¯Å¾eÅ¡`. UTF-8 bytes were re-encoded as
latin-1. `demojibake()` reverses it losslessly; it is applied to every string, and was verified
against every name and message in the exports with zero failures.

### Search

SQLite FTS5 over every message, indexed with `remove_diacritics 2` — so `necekal` finds
`nečekal`. Triggers keep the index in step with both writers (`sync_dht.py` and the Meta ingest),
so it can never drift. Query text is quoted token by token, so typing `"` or `OR` searches for
those words rather than erroring.

## Discord attachments: what survives

Most do not, and this is not fixable after the fact. Discord CDN links are signed and expire about
24 hours after they are issued:

- Measured on this author's archive: **9,740 of 9,805 images and 179 of 180 files return HTTP
  404.** Those bytes are gone.
- The handful downloaded in time are in the vault.
- Attachments and avatars embedded inside the `.dht` file itself are recovered offline by
  `py -m archive discord-media`.

Dead attachments still render as a card showing the filename, type, size and dimensions — the
message text is completely intact. Meta media is unaffected — every referenced file is stored.

**To stop losing future ones:** turn attachment downloading on inside the Discord History Tracker
app (its `downloads_auto_start` setting is currently `0`). DHT then embeds the bytes as it
scrapes, while the links still work. That is a setting in DHT, not something this code can fix.

## Identity mapping

Discord has stable numeric user ids; Meta exports have only display names. The *People* page ties
them together: tick the identities that belong to one person, give them a name, and that name is
what the whole app shows — thread lists, participants, message senders, search hits and reaction
tooltips — in place of the per-platform names.

Everything on that page edits the database directly: create a person from a selection, drag more
identities in later, rename, mark exactly one person as `is_self` (their messages align right),
or delete a person, which only unlinks — no message or identity is ever removed.

Every person and every link lives in the database (`people`, and `users.person_id`), so the
mapping travels with the archive. `app/people.yaml` is only a readable mirror of it, rewritten
after every change and kept out of version control since it holds real names. It stays
hand-editable: edit it while the app is idle, then press **Load the file and link** (or run
`py -m archive people apply`) to read it back in. `py -m archive people list` prints every identity
with its message count; `?` marks unmapped ones. Unmapped identities keep working — they simply show
under their original platform name.

Caveat worth knowing: Meta gives no stable user ids, so two different people sharing a display
name would merge. That is easy to eyeball on the *People* page, which lists every identity with
its message count.

## Schema

The database keeps Discord History Tracker's original table shapes so `sync_dht.py` and any
existing SQL keep working. Migrations only ever *add* columns, each with a default.

Added to existing tables: `platform` (all), `source_key` + `is_unsent` (messages),
`local_path` + `sha256` (attachments), `person_id` + `avatar_sha256` (users),
`avatar_sha256` (channels).

New tables: `people`, `channel_participants`, `message_reaction_actors` (Meta records reactions
per actor; DHT only aggregates), `ingest_log`, and the `messages_fts` index.

`sync_dht.py` was changed in exactly two ways: it names columns explicitly instead of
`SELECT *` (which would break against the added columns), and it calls
`archive.migrate.ensure_schema` so both halves always agree on the schema.

## Development

```
cd app/web
npm install
npm run dev      # Vite on :5173, proxying /api to the Python server on :8765
npm run build    # writes app/web/build, which `py -m archive serve` then serves
```

Stack: FastAPI + SQLite (stdlib `sqlite3`, no ORM) and SvelteKit 5 with `adapter-static`. No CDN
dependencies — it works with the machine offline.

## Backups

`py -m archive migrate` copies the database to `<name>.sqlite.bak-<timestamp>`
before touching the schema. The query endpoints open SQLite read-only, so browsing the archive
cannot modify it.

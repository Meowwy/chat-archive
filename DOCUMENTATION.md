# Documentation

How the archive is built and why it works the way it does. For getting started, see
[README.md](README.md).

| Platform | Source |
|---|---|
| Discord | Discord History Tracker `.dht` file |
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
| `py -m archive ingest <path>` | Import a Meta export folder or a Discord `.dht` file |
| `py -m archive stats` | What is in the archive right now |
| `py -m archive migrate` | Apply schema changes (backs the DB up first) |
| `py -m archive clean` | Drop Instagram's "Reacted 👍 to your message" pseudo-messages |
| `py -m archive discord-media` | Recover Discord attachments that exist locally |
| `py -m archive setup` | `migrate` + `discord-media` |
| `py -m archive people` | List every identity and the person it belongs to |
| `py smoke_test.py` | End-to-end checks (105 assertions) |

## How it fits together

```
discord_archive.dht                     live tracker file, read-only
your_facebook_activity/                 export folders
your_instagram_activity/
        |
        |  the Import page, or py -m archive ingest   (append-only)
        v
<your archive>.sqlite                   <-- the one database
        ^
        |  every image/video/file, copied once, addressed by sha256
        |
chat_media_vault/                       content-addressed media
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

### Importing a .dht file

A Discord History Tracker file is itself a SQLite database, so importing it is a table-by-table
copy into the archive. Deduplication is free here: every Discord row already carries a snowflake
id, so `INSERT OR IGNORE` on the primary key is the whole rule, and importing the same file twice
— or a newer file that overlaps an older one — adds only what is genuinely new and overwrites
nothing. The two tables DHT keeps without a unique key, `message_embeds` and `message_reactions`,
are deduplicated on their full content instead.

The file is never read directly. SQLite's online backup API takes a consistent snapshot into the
OS temp folder first, which is what makes it safe to import while the tracker has the file open.
Any table or column a newer tracker version added that this archive has no home for is named in
the run's progress output rather than dropped silently.

Attachments come along in the same pass: the bytes DHT embedded in the file go straight into the
media vault, and only those still needed are read — once an attachment or an avatar has been
stored, later imports skip its blob instead of re-hashing it. Blobs that match nothing (emoji, a
user's older avatar) stay in the `download_blobs` table, where `py -m archive discord-media` can
still reach them.

### Text encoding

Meta exports are mojibake — `Dobrá zpráva, můžeš` is written as `DobrÃ¡ zprÃ¡va, mÅ¯Å¾eÅ¡`. UTF-8 bytes were re-encoded as
latin-1. `demojibake()` reverses it losslessly; it is applied to every string, and was verified
against every name and message in the exports with zero failures.

### Search

SQLite FTS5 over every message, indexed with `remove_diacritics 2` — so `necekal` finds
`nečekal`. Triggers keep the index in step with every writer (both importers included),
so it can never drift.

Apostrophes are ignored too: `don't` and `dont` find each other. The tokenizer treats an
apostrophe as a separator, so `don't` is indexed as two tokens and `dont` as one; neither could
find the other. Each word is therefore searched as both spellings — itself, plus the phrases that
put an apostrophe back where a writer plausibly dropped one (`o'clock`, `don't`, `she's`).

#### Czech words are searched in every form

Czech inflects heavily — a noun has about ten forms, a verb about fifty — so matching the exact
spelling that was typed finds a fraction of what was meant. Searching `hospoda` in this archive
finds 6 messages; searching its whole paradigm finds **48**. So each word is looked up in a
Czech dictionary and widened to every form sharing its lemma: `hospody`, `hospodě` and `hospodu`
are all one search, and it does not matter which one you type.

The dictionary is `data/czech/`, compiled once into `Archives/czech_lemmas.sqlite` by
`py -m archive czech-dict` (~15 s, 149 MB, gitignored). It holds 3,370,510 forms over 272,867
lemmas. **If it has not been built, search simply stops widening** and behaves exactly as it did
before — it is never a hard dependency. `data/czech/NOTICE.md` carries the licence, which is
non-commercial.

Expansion happens in the *query*, not the index: one word becomes an `OR` of its forms. Nothing
is re-indexed, no schema changed, and FTS5's own `snippet()` still highlights the inflected form
it actually found. It stays cheap because paradigms are small — median 9 forms, 96 at the very
worst — so a typical query is answered in single-digit milliseconds.

Everything is stored and matched diacritic-folded, because the index is folded too. `fold()` in
`czech.py` is byte-identical to what `unicode61 remove_diacritics 2` does to every Czech letter,
and `smoke_test.py` asserts it — if the two ever drifted, expansion would silently stop matching
and nothing else would notice.

**Negation is kept apart.** Czech folds negation into the lemma, so `nečekal` and `čekal` share
the lemma *čekat* and 28 of its 56 forms are the `ne-` half. They mean opposite things, so
searching one never returns the other. Two traps are handled: a superlative `nej-` comes first
and is not a negation (`největší` stays with *velký*), and a lemma can start with `ne-` on its
own account — *nevím*, *nevěsta*, *nenávidět* — in which case the paradigm stays whole.

Where a form is ambiguous (`ženu` is both *žena* and *hnát*) there is no context to choose from,
so every reading is searched.

#### The query language

`OR`, `AND` and `NOT` are operators only in capitals, so ordinary lowercase text is never
mistaken for one; a literal `OR` can still be found by quoting it. Everything reaching FTS5 is
quoted, so nothing typed can be read as syntax.

| typed | means |
|---|---|
| `pivo hospoda` | both words, in any form (a bare space is still `AND`) |
| `pivo OR hospoda`, `pivo \| hospoda` | either |
| `(pivo OR víno) hospoda` | grouping |
| `pivo -hospoda`, `pivo NOT hospoda` | exclude |
| `"hospody"` | this exact form — quoting turns widening off |
| `hospod*` | prefix, which already spans the paradigm |

FTS5's `NOT` is a binary operator — `a AND NOT b` is a syntax error — so everything excluded is
gathered onto the right of a single `NOT`. A search that is only exclusions has nothing to start
from and is refused with a message saying so, as are unbalanced brackets.

The conversation view has the same search docked beside it (**Open search** in the left panel).
It covers every chat with that person, open or not, and clicking a hit opens the chat it belongs
to and scrolls it to the message.

### Reactions are not messages

Instagram writes a reaction into the thread twice: once on the message it belongs to, and once as
a standalone message reading *"Reacted 😂 to your message"* or *"Liked a message"*. The viewer
already shows reactions under their message, so the standalone copies are dropped at ingest and
`py -m archive clean` (also part of `migrate`) removes any an earlier run let through — 2,482 of
them in this author's archive. A notice is only dropped when it carries nothing else: no media, no
share, no reaction of its own.

## Discord attachments: what survives

Most do not, and this is not fixable after the fact. Discord CDN links are signed and expire about
24 hours after they are issued:

- Measured on this author's archive: **9,740 of 9,805 images and 179 of 180 files return HTTP
  404.** Those bytes are gone.
- The handful downloaded in time are in the vault.
- Attachments and avatars embedded inside the `.dht` file itself are recovered offline — during
  the import, or afterwards with `py -m archive discord-media`.

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

Every person and every link lives in the database (`people`, and `users.person_id`) and nowhere
else, so the mapping travels inside the archive file and there is nothing on the side to keep in
step with it. `py -m archive people` prints every identity with its message count; `?` marks
unmapped ones. Unmapped identities keep working — they simply show under their original platform
name.

Caveat worth knowing: Meta gives no stable user ids, so two different people sharing a display
name would merge. That is easy to eyeball on the *People* page, which lists every identity with
its message count.

## Schema

The database keeps Discord History Tracker's original table shapes, so a `.dht` file copies
straight in and any existing SQL keeps working. Migrations only ever *add* columns, each with a default.

Added to existing tables: `platform` (all), `source_key` + `is_unsent` (messages),
`local_path` + `sha256` (attachments), `person_id` + `avatar_sha256` (users),
`avatar_sha256` (channels).

New tables: `people`, `channel_participants`, `message_reaction_actors` (Meta records reactions
per actor; DHT only aggregates), `ingest_log`, and the `messages_fts` index.

`Archives/sync_dht.py` is now a two-line convenience wrapper around the same importer, kept only
because the habit of running it from that folder predates the Import page.

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

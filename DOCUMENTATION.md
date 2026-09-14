# Documentation

How the archive is built and why it works the way it does. For getting started, see
[README.md](README.md).

| Platform | Source |
|---|---|
| Discord | Discord History Tracker `.dht` file |
| Instagram | official export JSON |
| Facebook | official export JSON |
| Microsoft Teams | official export `.tar` (or the folder it unpacks to) |

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
py -m archive db C:/archives/chatArchive.sqlite --create   # start an empty archive
py -m archive db C:/archives/chatArchive.sqlite            # connect to an existing one
py -m archive db                                           # show what is connected
```

A new archive is called `chatArchive.sqlite` unless you name it something else, and its media
vault is created beside it. There is no built-in default location: until you connect one, the app
has no archive and says so.

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

To pull in new Discord messages, scrape more with Discord History Tracker and import its `.dht`
file again — only what is new is added.

## Commands

| Command | What it does |
|---|---|
| `py -m archive serve` | Run the web viewer |
| `py -m archive db [<path>] [--create]` | Show, connect or create the archive database |
| `py -m archive ingest <path>` | Import a Meta export folder, a Messenger encrypted-chat download, a Microsoft Teams export (folder or `.tar`), or a Discord `.dht` file |
| `py -m archive stats` | What is in the archive right now |
| `py -m archive migrate` | Apply schema changes (backs the DB up first) |
| `py -m archive clean` | Drop Instagram's "Reacted 👍 to your message" pseudo-messages |
| `py -m archive discord-media` | Recover Discord attachments that exist locally |
| `py -m archive setup` | `migrate` + `discord-media` |
| `py -m archive people` | List every identity and the person it belongs to |
| `py smoke_test.py` | End-to-end checks against a throwaway archive (241 assertions) |

## How it fits together

```
discord_archive.dht                     live tracker file, read-only
your_facebook_activity/                 export folders
your_instagram_activity/
messages/ + media/                      Messenger encrypted-chat download
<teams export>.tar                      read in place, never unpacked
        |
        |  the Import page, or py -m archive ingest   (append-only)
        v
<your archive>.sqlite                   <-- the one database
        ^
        |  every image/video/file, copied once, addressed by sha256
        |
chat_media_vault/                       content-addressed media
```

Everything lives in **one** database. Facebook, Instagram and Teams messages go into the same
`messages`, `channels` and `users` tables as Discord, distinguished by a `platform` column. Adding
Teams added no table and no column — the fourth platform fitted the schema the first three had
already settled.

### The three Meta exports, one importer

Facebook's and Instagram's *Download Your Information* archives are laid out as
`your_*_activity/messages/inbox/<thread>/message_1.json`. Messenger's encrypted-chat download is
laid out differently — a flat `messages/` folder of one JSON per conversation, beside the `media/`
folder its `./media/...` URIs point at — and names the same things differently:

| Download Your Information | Encrypted-chat download |
|---|---|
| `participants: [{"name": …}]` | `participants: ["…"]` |
| `title` / `thread_path` | `threadName` (with a `_<n>` suffix Meta adds for uniqueness) |
| `sender_name` `timestamp_ms` `content` | `senderName` `timestamp` `text` |
| `is_unsent` | `isUnsent`, with `text` set to a UI placeholder |
| `photos` / `videos` / `audio_files` / `files` | `media: [{uri}]`, one flat list |
| `reactions: [{reaction, actor}]` | identical |
| latin-1 mojibake | clean UTF-8 |

Rather than a second importer, `ingest/secure.py` rewrites a conversation into the first shape and
`MetaIngest` does the rest unchanged. Encrypted chats are Facebook Messenger conversations, so
they are stored as `platform = "facebook"`: a counterpart already known from a group chat resolves
to the *same* `users` row, and any people link they already have carries over.

Two things Meta's own export does that are worth knowing. It ships a file for every conversation
you have ever opened, empty ones included — those are skipped rather than turned into a thread and
an identity with nothing behind them. And where it could not retrieve an attachment it writes the
literal string `"Failed to download media"` in place of the URI; that is kept, and shows up as an
unavailable attachment, because the message really did carry one.

### The Teams export

Microsoft hands back one uncompressed `.tar` with everything at its root: `messages.json` — every
conversation and every message in a single JSON document — beside a flat `media/` folder of stored
objects, plus `endpoints.json` and `invites.json`, which are not conversation and are not imported.

**It is read where it lies.** These exports get large, mostly video, and unpacking one costs that
much disk again before a single message is imported — so `ingest/teams_export.py` opens either the
folder *or* the tar and answers the same three questions of both: what does `messages.json` say,
which objects exist, and give me the bytes of this one. `tarfile` indexes the
members once and then seeks, so an object is fetched without re-reading the archive, and the bytes
stream from the tar into the vault through `Vault.put_stream`, never landing anywhere in between and
never being held in memory. Pointing at an unpacked folder works identically; `smoke_test.py` imports
the same export both ways and asserts the two produce byte-identical rows.

**Media is keyed by document id.** A message lists `amsreferences: ["0-weu-d6-…"]`, and the objects
are named `media/<doc id>.<rendition>.<ext>`. Rendition `1` is the object itself; `2` is a video's
poster frame and `3` a voice message's transcript, and only the first is stored — the rest are
derived and the viewer never asks for them.

**Teams leaves a good deal out.** A sizeable share of the objects its own messages reference are
simply not in the download: voice messages, non-media files, and some of the older pictures. Those
are kept as attachment rows with a name, a type and no bytes — the same unavailable-file card
Discord's expired CDN links produce — because the message really did carry one. Everything that is
present is stored.

#### Working out who wrote a message

Teams attributes a message four different ways depending on which client sent it and whether Skype's
migration carried it over, and one of those ways is not at all:

| what the message carries | who that is |
|---|---|
| `from` | you, and only ever you — Teams fills it in for the exporting account alone |
| `displayName` | the sender's name, with no id |
| `properties.importedBy.RawValue` | the sender's Skype account id, sometimes with no name |
| none of them | nobody — which Teams does on some recent group messages |

An account id is the identity, not a name, so two people who share a display name stay two
identities — the caveat that applies to every Meta import does not apply here. That leaves the
accounts with an id and no name, and the export turns out to be full of a directory nobody advertises:
an `<at>` mention carries id and name together, so does a quoted reply's header, and Teams staples a
whole `<roster>` of the conversation onto any message that mentions somebody. `teams_content.identity_hints`
harvests all of it in a pass over every message before any of them is imported, and where an account
is named more than one way the commonest spelling wins. That is what turns a long run of messages
from a bare account id into messages from a named person.

Ids arrive in two spellings — `<quote author="live:someone">` drops the `8:` prefix that
`importedBy` keeps — so they are normalised before anything counts them, or one person would be
filed as two. Where two accounts genuinely share a name the handle is appended to both, so the
People page stays readable.

Messages Teams attributes to nobody are imported under a single **Unknown (Teams)** identity rather
than dropped. The alternative was discarding real messages; this way the text is searchable and the
People page has something to link if it is ever worked out who wrote them.

The account that requested the export *is* known — `messages.json` names it — so it is linked to
whoever is already marked as you on the People page. That is a fact rather than a guess, which is why
it is done at ingest and not left to be tidied up by hand; Meta exports cannot do it because nothing
in them says which participant you are.

#### Getting the sentence out of the markup

Teams inherited every message format Skype ever had and kept them all, so one conversation mixes
plain text, Skype's tag soup and modern Teams HTML. `ingest/teams_content.py` reduces all of it to
what somebody actually typed:

- An emoticon is written twice — `<ss type="laugh" alt="😆">(laugh)</ss>` — and exactly one of them
  is the message. The `alt` wins; the shortcode between the tags is dropped.
- A reply carries a **copy** of the message it answers, either as Skype's `<quote>` or as Teams'
  `<blockquote itemtype=".../Reply">`. The copy is dropped and the link recorded in
  `message_replied_to`, so the words are indexed once, under the message that actually said them.
- A media message's text is Skype's *"Pokud chcete zobrazit tuto sdílenou fotku, přejděte na: …"* —
  the recipient's-language fallback, never a caption. Media messages are given no text at all rather
  than having it stripped: indexing it would put every photo in the archive into the results for
  *fotku*.
- `<img alt="shared image">` is a picture, `<img alt="😉">` is an emoji. An `alt` with a Latin letter
  in it is prose about the element, not the element, and is dropped.
- Skype base64'd shared GIFs into a `<Swift b64="…">` attribute inside a card whose visible text is
  "To view this card, go to: …". Decoding them recovers every such message, which would otherwise
  have imported empty and been dropped.

Reactions are Skype emoticon shortcodes (`cwl`, `yes-tone1`, `2714_heavycheckmark`) and are mapped to
emoji — the codepoint-prefixed ones directly, the named ones through a table, and anything unknown
keeps its own name rather than being guessed at or discarded. Teams files its read-marker,
`reactionsConsumptionHorizon`, in among the reactions; it is not one.

**Calls, polls, and "X was added" notices are not imported**, nor is Skype's machine translation of a
message the archive already holds in the original. Neither are messages that end up with no text, no
attachment and no embed — an album header, whose photos arrive as messages of their own, and stickers
Teams no longer serves.

#### Why re-importing is exactly a no-op

Teams numbers every message, and the number is its arrival time in milliseconds, so it is the same in
every export you will ever request:

```
source_key = teams | <conversation id> | <message id>
```

That is the first importer here with a real id to lean on. Meta exports carry none, so
`ingest/meta.py` has to hash the message's own content and lives with the caveat that a freshly
requested encrypted-chat download re-imports media-only messages as new rows. Teams has no such
caveat: re-importing an overlapping export adds nothing at all, and the tar and the unpacked folder
are the same export down to the primary key.

### Why minted ids are negative

Discord snowflakes are always positive. Facebook and Instagram exports contain no message ids at
all, so the ingest mints its own: a 62-bit hash of the message's content, **negated**. A Meta id
therefore *cannot* collide with a Discord id — it is structurally impossible, not just unlikely.

Teams ids are hashed the same way even though Teams does supply one, for two reasons: its ids are
positive and only 13 digits, so they sit squarely in the range a Discord snowflake could occupy, and
hashing the whole `source_key` rather than the bare id is what keeps two conversations' messages
apart. What is hashed differs — Teams hashes an id, Meta hashes content — but every minted id lands
in the same negative half of the space.

Ids cross the HTTP boundary as **strings**. They are 62–63 bit integers and JavaScript numbers
lose precision above 2^53, so `JSON.parse` would silently corrupt them.

### How duplicate detection works

Meta exports have no message ids and each new export repeats everything from the last few months.
Identity is therefore derived from the message itself:

```
source_key = platform | thread_path | sender | timestamp_ms | sha1(text + media uris + link)
```

Media URIs are part of that key, and the encrypted-chat download names its files with a fresh UUID
every time you request one. Re-importing the *same* folder is a no-op, but a newly requested
download re-imports media-only messages as new rows. The vault still stores each file's bytes
once, addressed by their sha256.

Verified unique across every message in the exports it was built against, with zero collisions. The
message id is a hash of that key, so the primary key does the deduplication and re-importing an
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

Czech inflects heavily — a noun has about ten forms, a verb about fifty — so matching only the
exact spelling that was typed finds a fraction of what was meant; searching a word's whole paradigm
routinely finds several times as much. So each word is looked up in a Czech dictionary and widened
to every form sharing its lemma: `hospody`, `hospodě` and `hospodu` are all one search, and it does
not matter which one you type.

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

### Counting a word over time

The **Stats** tab asks the other question about a word: not where it was said, but when. Pick
someone from the list — grouped exactly as the conversation list groups them, so one person's
Discord and Instagram chats are one history, busiest first — and the chart is every message
exchanged with them, by month, over the whole timeline. Type a word into the panel beside it and
the chart becomes the months that word was used in, widened to every inflected form through the
same query language as above.

One endpoint answers both: `GET /api/stats/months?threads=<ids>&q=<word>`. Without `q` it groups
those chats' messages by month; with it, the FTS expression the search page would have run is
joined onto the index and the matches are grouped the same way. Counting in SQL rather than in
the browser is what keeps it instant on a conversation of any size — the page never pages
through hits to plot them, and the line can never disagree with the search results behind it,
which `smoke_test.py` asserts outright.

A month's number is how many *messages* used the word, not how many times it occurs in them —
the same number the search page reports as its result count. Months nobody spoke in come back
missing, so the browser fills the range before plotting: a silent month is a zero on the line
rather than a straight segment drawn across it. The y-axis rescales to whatever is on show, so a
couple of dozen messages fill the height as readily as a few thousand, and **Split by author**
breaks the line into the same blue and pink halves the conversation timeline uses.

Picking more people adds a line each, up to ten — as many as the palette can keep apart. There
the me-and-them split gives way to a single question asked of everyone at once: **All**,
**Outgoing** or **Incoming**, chosen in the left panel. Each line is named in the legend and
carries a colour taken from a fixed order, held for as long as that person is on the chart, so
dropping one of five never repaints the other four. The ten colours are stepped separately for
the light and the dark surface and were checked — not eyeballed — for lightness, chroma and
colour-blind separation against each neighbouring pair.

### Reactions are not messages

Instagram writes a reaction into the thread twice: once on the message it belongs to, and once as
a standalone message reading *"Reacted 😂 to your message"* or *"Liked a message"*. The viewer
already shows reactions under their message, so the standalone copies are dropped at ingest and
`py -m archive clean` (also part of `migrate`) removes any an earlier run let through. A notice is
only dropped when it carries nothing else: no media, no share, no reaction of its own.

## Discord attachments: what survives

Most do not, and this is not fixable after the fact. Discord CDN links are signed and expire about
24 hours after they are issued:

- In practice almost every image and file an older archive points at returns HTTP **404**. Those
  bytes are gone.
- The few that were downloaded while their links were still fresh are in the vault.
- Attachments and avatars embedded inside the `.dht` file itself are recovered offline — during
  the import, or afterwards with `py -m archive discord-media`.

Dead attachments still render as a card showing the filename, type, size and dimensions — the
message text is completely intact. Meta media is unaffected — every referenced file is stored.

**To stop losing future ones:** turn attachment downloading on inside the Discord History Tracker
app (its `downloads_auto_start` setting, which is off by default). DHT then embeds the bytes as it
scrapes, while the links still work. That is a setting in DHT, not something this code can fix.

## Identity mapping

Discord has stable numeric user ids and Teams has Skype account ids; Meta exports have only display
names. The *People* page ties them together: tick the identities that belong to one person, give them a name, and that name is
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
its message count. It does not apply to Discord or Teams, whose identities are keyed by account —
there, two people called the same thing stay two rows, and the *People* page is where you say they
are one person if they are.

## Schema

The database keeps Discord History Tracker's original table shapes, so a `.dht` file copies
straight in and any existing SQL keeps working. Migrations only ever *add* columns, each with a default.

Added to existing tables: `platform` (all), `source_key` + `is_unsent` (messages),
`local_path` + `sha256` (attachments), `person_id` + `avatar_sha256` (users),
`avatar_sha256` (channels).

New tables: `people`, `channel_participants`, `message_reaction_actors` (Meta records reactions
per actor; DHT only aggregates), `ingest_log`, and the `messages_fts` index.

## Development

```
cd app/web
npm install
npm run dev      # Vite on :5173, proxying /api to the Python server on :8765
npm run build    # writes app/web/build, which `py -m archive serve` then serves
```

Stack: FastAPI + SQLite (stdlib `sqlite3`, no ORM) and SvelteKit 5 with `adapter-static`, plus
Chart.js for the Stats lines. No CDN dependencies — everything is bundled into the build, and it
works with the machine offline.

### One archive, handed around

A database, the media vault whose files its `attachments` rows point at, and the Czech dictionary
always travel together, so one object owns all three:

```python
archive = Archive.connected()      # what this machine is set up to use
archive = Archive.open(path)       # that file, plainly
archive = Archive.create(path)     # a new, empty, complete archive

archive.read()                     # cached read-only connection
with archive.write() as con: ...   # read-write for one job
```

Nothing below `archive.py` asks where the archive is — the vault is built with its directory, the
importers are given it, `config.py` only answers *which archive should this machine use?* and
answers it as a value. Reads and writes are deliberately asymmetric: `read()` hands back one
long-lived read-only connection, so browsing can never modify the archive, while `write()` opens
one for the duration of a job and drops the cached reader on the way out — so the next read sees
the write and nobody has to remember to invalidate anything.

Which archive is open is session state, so it lives in `api.py`, the one place where "connect a
different database" is a thing that happens.

### The checks build their own archive

`py smoke_test.py` does not read your archive. `fixture.py` writes a Facebook export, an Instagram
export, a Messenger encrypted-chat download, a Microsoft Teams export — as a folder *and* as the tar
Microsoft ships — and a `.dht` file into a temp folder, then ingests them, links the identities into
people, and hands back an `Archive`. Every check runs against that, so they pass on a fresh clone and
leave nothing behind.

What the fixture deliberately contains is the list of things that have gone wrong or could: real
mojibake, Czech inflection, Instagram's reaction pseudo-messages, a media URI Meta failed to export,
Skype emoticons and quoted replies, a base64'd GIF card, an object Teams left out of the download, a
message Teams attributes to nobody at all, and the same export offered twice in two forms — which
must produce the same primary keys or the check fails.

The Czech dictionary is the one optional part: it is derived data, so checks that need it are
skipped with a note rather than failed, exactly as search itself degrades without it.

## Backups

`py -m archive migrate` copies the database to `<name>.sqlite.bak-<timestamp>`
before touching the schema. The query endpoints open SQLite read-only, so browsing the archive
cannot modify it.

# thread-ledger

Pull iMessage threads off your Mac and file them into your
[memory-vault](https://github.com/amercat37/memory-vault) as clean, searchable
markdown — so your group chats (and any one-on-one chats you choose) become part
of the same semantic memory as the rest of your notes.

It reads the local Messages database, replaces phone numbers with the real names
from your Contacts, and appends any new messages to a **per-year** markdown file
(e.g. `Text Messages/Wacky Wednesday/Wacky Wednesday - 2026.md`). Run on a
schedule, it keeps the current year's file quietly up to date; your existing
nightly vault pipeline embeds it like any other note.

> Like its sibling **echo-ledger**, this is a companion to memory-vault, and it
> honors the same rule: **LiveSync stays the vault's only writer.** thread-ledger
> writes into your *local* Obsidian folder; it never touches the server vault
> directly.

## What it is

- **Your choice of chats** — the chats you list in config, plus (if you turn on
  `auto_groups`) new group chats that pass simple rules. One-on-one chats are only
  ever saved when you list them.
- **Tracked by chat ID** — a group can be renamed, or have no name at all, and its
  history never splits.
- **Names, not numbers** — resolves people via manual overrides → a local cache →
  macOS Contacts (phone numbers and email addresses) → the raw handle.
- **Append-only, split by year** — new messages are appended under dated headings
  into a per-year file, so each file is a durable archive even if Messages later
  prunes old messages. Only the current year's file ever changes; earlier years
  are frozen, so the vault re-scans and re-embeds them only once.
- **Quiet** — runs that find nothing new touch no files, so there's no needless
  re-sync or re-embed.
- **Safe to interrupt** — a run stopped at any point never duplicates or drops a
  message, and LiveSync never sees a half-written note.
- **Zero runtime dependencies** — the collector uses only the Python standard
  library, so it runs unattended under launchd with no venv to maintain.

## Quick start

```bash
git clone <this repo> thread-ledger && cd thread-ledger
cp config.example.toml config.toml        # then edit output_dir and me
python3 thread_ledger.py --list-chats     # see your chats and their IDs; changes nothing
# add the chats you want to config.toml (and/or turn on auto_groups), then:
python3 thread_ledger.py --list-chats     # check the folder names before the first run
python3 thread_ledger.py                  # first run pulls full history
```

The first run needs **Full Disk Access** (see below) to read the Messages
database, and pulls the entire history of each chat it saves. Subsequent runs
append only what's new.

**Check the names before the first run.** Folder names are fixed when a chat is
first saved (so a folder you know never changes under you). If a person isn't in
your Contacts yet, an unnamed group gets a name like `Unknown +2` — add them to
Contacts first, or give the chat a `name` in config.

## Choosing chats

`--list-chats` prints every group chat (add `--all` for one-on-one chats too),
newest first, with what the next run would do with it:

```
STATUS  FOLDER                      KIND   TYPE  KNOWN  MSGS  LAST        NOTE                        CHAT ID
saved   Book Club                   group  SMS   8/8    412   2026-11-23                              "chat123456789012345678"
new     Alice & Bob & Carol +9      group  RCS   12/12  10    2026-11-06  will be saved (new group)   "0f3c9a1b2d4e5f60718293a4b5c6d7e8"
skip    WIN A PRIZE                 group  RCS   0/2    1     2026-06-09  under 3 messages; nobody…   "a1b2c3d4e5f60718293a4b5c6d7e8f90"
```

`KNOWN` is how many of the people in the chat are in your Contacts. The note also
flags chats with exactly the same people as a saved one (`same people as: …`) —
often a separate chat (a party-planning thread, say), sometimes a copy Messages
split off. thread-ledger never joins chats on its own; you decide.

### Listing chats in config

```toml
[[threads]]
name = "Book Club"                  # optional folder name
chats = ["chat123456789012345678"]  # chat IDs from --list-chats
```

A listed chat is always saved, whatever the auto rules say. A one-on-one chat's ID
is the person's number or email (`chats = ["+15551234567"]`); if Messages keeps
separate SMS and iMessage chats with that person, they share the ID and go into
one folder. List several IDs to keep several chats in one folder — their messages
are interleaved by time, and the chats stay together from then on.

Older configs used `match = "Wacky Wednesday"` (a group whose name contains the
text, or a chat whose ID is exactly it). That still works, but chat IDs are
better: a future group called "Wacky Wednesday Christmas" would match too.

### Saving new groups automatically

```toml
auto_groups = true
min_messages = 3              # defaults shown
active_within_days = 365
require_known_contact = true
exclude = ["a1b2c3d4e5f60718293a4b5c6d7e8f90"]
```

With `auto_groups` on, a group chat is saved — full history — the first time it
has at least `min_messages` messages, a message within `active_within_days`, and
(with `require_known_contact`) at least one person in your Contacts, which keeps
out scam and mass texts from strangers. A saved group stays saved even if it later
stops passing the rules. `exclude` lists chat IDs never to save (it also stops a
saved chat; its files are kept).

### Folder names

| Chat | Folder |
| --- | --- |
| `name` set in config | that name |
| named group | the group's name, and it **follows renames** (`follow_renames = false` to pin it) |
| unnamed group | its people: up to three first names from Contacts, alphabetical, then `+N` for everyone else — `Alice & Bob & Carol +9`, `Dana +1`, `Unknown +2` |
| one-on-one | the person's full name from Contacts |

Anyone who ever wrote in the chat counts, even if they've since left. You're never
in the name. Names drop the characters Obsidian can't use or that break links
(`* " \ / < > : | ? # ^ [ ]`) and commas (memory-vault splits `topics` on commas).
Names made from people never change, even when people come and go or you add them
to Contacts later — set `name` in config to change one.

If a name is taken (including by a folder you made yourself), the newer chat gets
the end of its chat ID: `Alice & Bob`, then `Alice & Bob 5678`. Existing folders
never shift.

**Renaming** a folder — because a group was renamed in Messages, or you changed
its `name` in config — moves the folder and its notes and updates their topics.
The vault sees new paths, so it **re-embeds every year** of that chat. For a group
renamed every year ("2026 Wacky Wednesday" → "2027 Wacky Wednesday"), set a `name`
in config.

## Configuration (`config.toml`)

| Key | Meaning |
| --- | --- |
| `output_dir` | Where each chat's folder (`<folder>/<folder> - <year>.md`) is written. Point at the `Text Messages` folder in your **local** Obsidian vault for production. |
| `timezone` | IANA timezone for rendering timestamps (the database stores UTC). |
| `me` | Name used for your own messages (and left out of folder names). |
| `chat_db` | Path to the Messages database (rarely changed). |
| `auto_groups` | Save new group chats automatically (default `false`). |
| `min_messages`, `active_within_days`, `require_known_contact` | The rules for new groups (defaults `3`, `365`, `true`). |
| `exclude` | Chat IDs never to save. |
| `start_date` | `"YYYY-MM-DD"`: ignore older messages (default blank = everything). Lowering it later needs `--rebuild`. |
| `skip_pii` | Default for every chat: write `skip_pii: true` into its notes (default `false`). |
| `[[threads]]` | Chats to always save: `chats` (IDs) or legacy `match`; optional `name`, `follow_renames`, `topics`, `skip_pii`. |
| `[contacts]` | Optional `"+1…" = "Name"` overrides that win over Contacts. |

A mistake in `config.toml` stops the run with a message saying what's wrong
(exit code 3); nothing is written.

### Name resolution

For each handle, in order: **manual override** → **`contacts_cache.json`** →
**macOS Contacts** (result is then cached) → **raw number or email**. New people
are resolved automatically the first time they appear. Use `--refresh-contacts` to
re-pull everyone from Contacts, ignoring the cache.

## Upgrading from 1.x

Version 2 tracks chats by ID, so `state.json` changes format. The first run of 2.x
converts it automatically (keeping your progress — nothing is re-written) and
saves the old file as `state.v1.backup.json`. Your existing `[[threads]]` entries
keep working unchanged, and `auto_groups` is off until you turn it on.

Do the conversion with your config unchanged, then switch entries from `match` to
`chats` if you like (`--list-chats` shows the IDs). A 1.x entry that is no longer
in config is kept aside in `state.json` (not updated) rather than lost.

## Full Disk Access (required)

Reading `~/Library/Messages/chat.db` (and Contacts) requires Full Disk Access for
the process doing the reading. For the scheduled job, that's the Python
interpreter in the plist:

1. **System Settings → Privacy & Security → Full Disk Access**
2. Click **+**, press **⌘⇧G**, and go to `/usr/local/bin/` (or wherever your
   `python3` lives — `readlink -f /usr/local/bin/python3` shows the real binary).
3. Add `python3` and make sure its toggle is **on**.

This is a one-time manual step; no command-line tool can grant it.

## Scheduling (launchd)

```bash
cp com.example.threadledger.plist ~/Library/LaunchAgents/com.<you>.threadledger.plist
# edit the plist: set the absolute path to thread_ledger.py
launchctl load ~/Library/LaunchAgents/com.<you>.threadledger.plist
launchctl kickstart -k gui/$(id -u)/com.<you>.threadledger   # run once now
```

`StartInterval` is set to **5400s (90 min)**. launchd only fires while the Mac is
awake and coalesces missed fires into one catch-up run on wake — so there's no
need to detect whether the Mac is online. Logs go to `/tmp/thread-ledger.log` and
`/tmp/thread-ledger.err`.

To stop it: `launchctl unload ~/Library/LaunchAgents/com.<you>.threadledger.plist`.

## How it fits the vault

```
Mac: launchd (every 90 min)
  └─ thread_ledger.py
       ├─ read ~/Library/Messages/chat.db  (read-only)
       ├─ resolve numbers → names (Contacts)
       └─ append new messages → <local vault>/Text Messages/<folder>/<folder> - <year>.md
                                          │
                              Obsidian LiveSync ──▶ server vault ──▶ nightly pipeline ──▶ embeddings
```

thread-ledger only ever writes to the local Obsidian folder. LiveSync propagates
the change to the server, and the existing memory-vault pipeline sanitizes,
chunks, and embeds it on its usual nightly schedule. (If LiveSync is set to sync
only on start-up or when a note is opened, new messages reach the server the next
time that happens.)

## Rebuilding

```bash
python3 thread_ledger.py --rebuild
```

Regenerates each chat's year files from the database. Use it after changing a
name mapping or the output format, or after upgrading (for example, to drop
reactions an older version let through).

Rebuild only replaces years the database still has messages for: if Messages has
nothing for a chat, or has pruned an earlier year, those files are left alone.
A year that Messages has only *partly* pruned would be rebuilt from what's left,
so only rebuild when your Messages retention is "Keep Messages: Forever".

Every rewritten file is re-embedded by the vault pipeline, so rebuild costs a
full re-embed of the chats it touches.

## Privacy note

Everything runs locally. The generated files, `config.toml`, `contacts_cache.json`
and `state.json` contain personal data (numbers, names, message content) and are
gitignored — only the `.example` files are committed. PII handling for the vault
itself is done by the memory-vault pipeline, not here.

Folder and file names come from group names and people's first names, and the
vault pipeline does not redact names or paths — which is one reason unknown
people appear as `Unknown +N` rather than as phone numbers.

Only archive conversations that you are authorized to store and process, and follow applicable privacy requirements and workplace policies.

## Architecture

A single script, `thread_ledger.py`:

- **Read** — opens `chat.db` in plain read-only mode (a reader never blocks
  Messages, and it sees messages still in the write-ahead log), loads every chat
  with its message count, dates and people, and reads messages per chat, excluding
  reactions/tapbacks (`associated_message_type = 0`), system events
  (`item_type = 0`) and the plain-text tapbacks SMS/RCS members send
  (`Loved an image`, `Reacted 😂 to "…"`, `❤️ to a photo`). Messages whose `text`
  is NULL are recovered from the archived `attributedBody` blob.
- **Choose** — config entries first, then chats already saved, then (with
  `auto_groups`) new groups that pass the rules; works out each folder name,
  including duplicates and renames.
- **Resolve** — maps handles (phone numbers or emails) to names via overrides →
  cache → Contacts → raw.
- **Render** — groups by date, emits `**Name** (time): text` lines, renders
  attachment-only messages as `[attachment]`. Each file's frontmatter carries
  `topics` (`Text Messages, iMessage, <folder>`) and `attendees` (the resolved
  participant names) — the two keys memory-vault embeds into every chunk, so the
  chat is findable by group and by participant. Only people who actually said
  something count (a reaction alone doesn't). The roster accumulates in
  `state.json` so it stays complete across incremental appends.
- **Append** — a watermark per chat (last message ROWID) in `state.json` means
  each run adds only new messages, keyed by ROWID so there are no duplicates. Each
  message is routed to a per-year file (`<folder> - <year>.md`); only years that
  received new messages are rewritten. A message that arrives late (dated before
  the last day already written) — or the history of a chat just added to a
  folder — is inserted under its own day, not appended.
- **Write safely** — each file is written to a temporary dotfile and swapped in,
  so LiveSync never sees a half-written note. Before writing, a run records in
  `state.json` what each file will contain; if the run is stopped partway, the
  next run sees which files landed and neither duplicates nor drops messages.
  Renames are recorded first and finished by the next run if interrupted.
  Progress is saved after every chat.

State lives in `contacts_cache.json` (resolved names) and `state.json`
(version 2: per folder, its chat IDs with their watermarks, and the last date and
attendees written per year).

### PII scanning

By default these files are left for the memory-vault pipeline to scan for PII like
any other note — nothing is written into the frontmatter, since group chats are
exactly where an address or phone number can slip in. The per-year split keeps that
nightly scan bounded (only the current year's file changes, and past years are
scanned once then frozen).

Set `skip_pii = true` at the top of `config.toml` to opt every chat out, or in a
`[[threads]]` block for one chat (either way round), which writes `skip_pii: true`
into the frontmatter for the pipeline to read. It has to be set in config rather
than edited into the note by hand: the frontmatter is regenerated every time new
messages are appended, so a manual edit is silently discarded on the next run.

## Tests

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

Tests run against fixture databases and never touch your real messages. They
cover the rules, naming, renames, crash recovery at every write, the 1.x upgrade,
and a 200,000-message performance check (a few seconds). The venv is for tests
only — the runtime needs no dependencies.

## About this project

Thread Ledger is a companion to my memory-vault project and a sibling to Echo Ledger, which performs the same role for audio transcripts. I defined the project’s purpose, data flow, privacy boundaries, incremental archival behavior, and integration requirements, then developed the implementation with assistance from Claude Code.

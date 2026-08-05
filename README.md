# thread-ledger

Pull iMessage **group threads** off your Mac and file them into your
[memory-vault](https://github.com/amercat37/memory-vault) as clean, searchable
markdown — so your group chats become part of the same semantic memory as the
rest of your notes.

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

- **Group-thread focused** — collects the specific group chats you name in config.
- **Names, not numbers** — resolves handles via manual overrides → a local cache →
  macOS Contacts → the raw number as a last resort.
- **Append-only, split by year** — new messages are appended under dated headings
  into a per-year file, so each file is a durable archive even if Messages later
  prunes old messages. Only the current year's file ever changes; earlier years
  are frozen, so the vault re-scans and re-embeds them only once.
- **Quiet** — runs that find nothing new touch no files, so there's no needless
  re-sync or re-embed.
- **Zero runtime dependencies** — the collector uses only the Python standard
  library, so it runs unattended under launchd with no venv to maintain.

## Quick start

```bash
git clone <this repo> thread-ledger && cd thread-ledger
cp config.example.toml config.toml      # then edit output_dir, me, threads
python3 thread_ledger.py                 # first run pulls full history
```

The first run needs **Full Disk Access** (see below) to read the Messages
database, and pulls the entire history of each configured thread. Subsequent runs
append only what's new.

## Configuration (`config.toml`)

| Key | Meaning |
| --- | --- |
| `output_dir` | Where `<thread>.md` is written. Point at the `Text Messages` folder in your **local** Obsidian vault for production. |
| `timezone` | IANA timezone for rendering timestamps (the database stores UTC). |
| `me` | Name used for your own messages. |
| `chat_db` | Path to the Messages database (rarely changed). |
| `[[threads]]` | One block per group chat: `name` (output filename) and `match` (substring of the chat's display name — survives yearly renames). |
| `[contacts]` | Optional `"+1…" = "Name"` overrides that win over Contacts. |

### Name resolution

For each handle, in order: **manual override** → **`contacts_cache.json`** →
**macOS Contacts** (result is then cached) → **raw number**. New people are
resolved automatically the first time they appear. Use `--refresh-contacts` to
re-pull everyone from Contacts, ignoring the cache.

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
       └─ append new messages → <local vault>/Text Messages/<thread>/<thread> - <year>.md
                                          │
                              Obsidian LiveSync ──▶ server vault ──▶ nightly pipeline ──▶ embeddings
```

thread-ledger only ever writes to the local Obsidian folder. LiveSync propagates
the change to the server, and the existing memory-vault pipeline sanitizes,
chunks, and embeds it on its usual nightly schedule.

## Rebuilding

```bash
python3 thread_ledger.py --rebuild
```

Wipes each thread file and its watermark and regenerates from the database. Use
it after changing a name mapping or the output format. Safe whenever your
Messages retention is set to "Keep Messages: Forever".

## Privacy note

Everything runs locally. The generated files, `config.toml`, `contacts_cache.json`
and `state.json` contain personal data (numbers, names, message content) and are
gitignored — only the `.example` files are committed. PII handling for the vault
itself is done by the memory-vault pipeline, not here.

Only archive conversations that you are authorized to store and process, and follow applicable privacy requirements and workplace policies.

## Architecture

A single script, `thread_ledger.py`:

- **Read** — opens `chat.db` read-only/immutable (so it never blocks Messages),
  filters to the configured threads by display-name substring, and excludes
  reactions/tapbacks (`associated_message_type = 0`) and system events
  (`item_type = 0`). Messages whose `text` is NULL are recovered from the archived
  `attributedBody` blob.
- **Resolve** — maps handles to names via overrides → cache → Contacts → raw.
- **Render** — groups by date, emits `**Name** (time): text` lines, renders
  attachment-only messages as `[attachment]`. Each file's frontmatter carries
  `topics` (`Text Messages, iMessage, <thread>`) and `attendees` (the resolved
  participant names) — the two keys memory-vault embeds into every chunk, so the
  thread is findable by group and by participant. The attendee roster accumulates
  in `state.json` so it stays complete across incremental appends.
- **Append** — a per-thread watermark (last message ROWID) in `state.json` means
  each run adds only new messages, keyed by ROWID so there are no duplicates. Each
  message is routed to a per-year file (`<thread> - <year>.md`); only years that
  received new messages are rewritten.

State lives in `contacts_cache.json` (resolved names) and `state.json`
(watermarks + the last date written per year).

### PII scanning

These files are left for the memory-vault pipeline to scan for PII like any other
note — `skip_pii: true` is intentionally **not** set in the frontmatter, since
group chats are exactly where an address or phone number can slip in. The per-year
split keeps that nightly scan bounded (only the current year's file changes, and
past years are scanned once then frozen).

## Tests

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

Tests run against an in-memory fixture database and never touch your real
messages. The venv is for tests only — the runtime needs no dependencies.

## About this project

Thread Ledger is a companion to my memory-vault project and a sibling to Echo Ledger, which performs the same role for audio transcripts. I defined the project’s purpose, data flow, privacy boundaries, incremental archival behavior, and integration requirements, then developed the implementation with assistance from Claude Code.

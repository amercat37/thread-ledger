#!/usr/bin/env python3
"""thread-ledger — pull iMessage group threads into markdown for the memory vault.

Reads the local Messages database (~/Library/Messages/chat.db), filters to the
group threads named in config.toml, resolves phone numbers to names (manual
overrides -> cache -> macOS Contacts -> raw number), and appends any new
messages to per-year markdown files (<name>/<name> - <year>.md). Runtime uses the standard library only
so it can run unattended under launchd with no venv.

Usage:
    thread_ledger.py [--config PATH] [--rebuild] [--refresh-contacts] [--quiet]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tomllib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Seconds between the Unix epoch (1970-01-01) and the Apple epoch (2001-01-01).
APPLE_EPOCH_OFFSET = 978_307_200

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "config.toml"
CACHE_PATH = SCRIPT_DIR / "contacts_cache.json"
STATE_PATH = SCRIPT_DIR / "state.json"

# Unicode Object Replacement Character — iMessage uses it as an inline
# placeholder where an attachment sits in the text.
OBJ_REPLACEMENT = "￼"

# SMS/RCS group members' tapbacks arrive as plain-text messages (with
# associated_message_type = 0, so the type-code filter can't catch them). They
# always take the auto-generated form "<verb> <object>", e.g. "Loved an image"
# or 'Laughed at "…"'. Drop them to honor the no-reactions rule.
REACTION_RE = re.compile(
    r"^(Loved|Liked|Disliked|Laughed at|Emphasized|Questioned) "
    r"(an image|a movie|a video|an audio message|a link|a message|the message"
    r"|[\"“].*[\"”])$",
    re.DOTALL,
)


# --------------------------------------------------------------------------- #
# Config / state / cache
# --------------------------------------------------------------------------- #
def load_config(path: Path) -> dict:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def load_json(path: Path) -> dict:
    if path.exists():
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` so readers only ever see the old or the new file,
    never a half-written one (LiveSync may pick a file up at any moment). The
    temp file is a dotfile in the same folder: Obsidian ignores dotfiles, and
    os.replace is only atomic within one filesystem."""
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def save_json(path: Path, data: dict) -> None:
    atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #
def apple_to_datetime(raw: int, tz: ZoneInfo) -> datetime:
    """Convert a Messages timestamp to a tz-aware local datetime.

    Modern macOS stores nanoseconds since 2001-01-01; older versions stored
    seconds. Anything larger than ~10^11 is nanoseconds.
    """
    seconds = raw / 1_000_000_000 if raw > 100_000_000_000 else raw
    return datetime.fromtimestamp(seconds + APPLE_EPOCH_OFFSET, tz)


# --------------------------------------------------------------------------- #
# Phone / name resolution
# --------------------------------------------------------------------------- #
def norm_key(raw: str | None) -> str:
    """Normalize a handle to a comparison key: last 10 digits for phone
    numbers, or the lowercased raw value for emails / short codes."""
    if not raw:
        return ""
    if "@" in raw:
        # Emails can contain 10+ digits ("jo1234567890@…"); never treat them
        # as phone numbers.
        return raw.strip().lower()
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 10:
        return digits[-10:]
    return raw.strip().lower()


def addressbook_dbs() -> list[Path]:
    base = Path.home() / "Library" / "Application Support" / "AddressBook"
    dbs = [base / "AddressBook-v22.abcddb"]
    dbs += sorted(base.glob("Sources/*/AddressBook-v22.abcddb"))
    return [p for p in dbs if p.exists()]


# Phone numbers and email addresses on Contacts cards. iMessage handles are
# one or the other, so both are looked up.
_AB_QUERIES = (
    "SELECT p.ZFULLNUMBER, {name} FROM ZABCDPHONENUMBER p "
    "JOIN ZABCDRECORD r ON p.ZOWNER = r.Z_PK WHERE p.ZFULLNUMBER IS NOT NULL",
    "SELECT e.ZADDRESS, {name} FROM ZABCDEMAILADDRESS e "
    "JOIN ZABCDRECORD r ON e.ZOWNER = r.Z_PK WHERE e.ZADDRESS IS NOT NULL",
)
_AB_NAME_COLS = ("COALESCE(r.ZFIRSTNAME, ''), COALESCE(r.ZLASTNAME, ''), "
                 "COALESCE(r.ZORGANIZATION, '')")


def build_addressbook_map(dbs: list[Path] | None = None) -> dict[str, str]:
    """Map norm_key -> full name from every Contacts database."""
    result: dict[str, str] = {}
    for db in addressbook_dbs() if dbs is None else dbs:
        try:
            conn = open_readonly(db)
        except sqlite3.Error:
            continue
        rows = []
        try:
            for query in _AB_QUERIES:
                try:
                    rows += conn.execute(query.format(name=_AB_NAME_COLS)).fetchall()
                except sqlite3.Error:
                    continue  # table missing in this source; use what we have
        finally:
            conn.close()
        for handle, first, last, org in rows:
            name = " ".join(part for part in (first.strip(), last.strip()) if part)
            name = name or org.strip()
            key = norm_key(handle)
            if key and name and key not in result:
                result[key] = name
    return result


class NameResolver:
    """Resolve a handle to a display name using, in order:
    manual overrides -> cache -> Contacts (then cached) -> raw fallback.
    """

    def __init__(self, overrides: dict, cache: dict, refresh: bool = False):
        self.overrides = {norm_key(k): v for k, v in overrides.items()}
        self.cache = cache  # norm_key -> name (mutated + persisted by caller)
        self.refresh = refresh
        self._ab: dict[str, str] | None = None  # lazily loaded

    def _addressbook(self) -> dict[str, str]:
        if self._ab is None:
            self._ab = build_addressbook_map()
        return self._ab

    def resolve(self, handle: str | None) -> str:
        key = norm_key(handle)
        if key in self.overrides:
            return self.overrides[key]
        if not self.refresh and key in self.cache:
            return self.cache[key]
        name = self._addressbook().get(key)
        if name:
            self.cache[key] = name
            return name
        # Fallback: keep the raw handle so an unknown sender is still traceable.
        return handle or "Unknown"


# --------------------------------------------------------------------------- #
# attributedBody decode
# --------------------------------------------------------------------------- #
def decode_attributed_body(blob: bytes | None) -> str | None:
    """Best-effort extraction of message text from the archived
    NSAttributedString blob used when message.text is NULL."""
    if not blob:
        return None
    try:
        marker = b"NSString"
        idx = blob.find(marker)
        if idx == -1:
            return None
        body = blob[idx + len(marker):]
        body = body[5:]  # skip class/version marker bytes
        if body and body[0] == 0x81:  # 0x81 => 2-byte little-endian length
            length = int.from_bytes(body[1:3], "little")
            body = body[3:]
        else:
            length = body[0]
            body = body[1:]
        text = body[:length].decode("utf-8", errors="replace")
        return text or None
    except (IndexError, UnicodeDecodeError):
        return None


# --------------------------------------------------------------------------- #
# Messages DB
# --------------------------------------------------------------------------- #
def open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open a live database (Messages, Contacts) read-only.

    Plain read-only mode, not `immutable=1`: these files change constantly, and
    immutable tells SQLite they never do — it then skips the write-ahead log
    (so recent messages show up a run late) and can read pages mid-checkpoint
    (garbled rows or "malformed" errors). In WAL mode a reader never blocks the
    writer, so Messages is not disturbed either way."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)


def fetch_messages(conn: sqlite3.Connection, match: str, since_rowid: int) -> list[dict]:
    """Return real messages (no reactions/tapbacks, no system events) for chats
    whose display_name OR chat_identifier contains `match`, newer than
    `since_rowid`, oldest first. Group chats carry a display_name ("Wacky
    Wednesday"); 1:1 chats have no name, so they match on chat_identifier (the
    handle, e.g. a phone number)."""
    rows = conn.execute(
        """
        SELECT m.ROWID              AS rowid,
               m.date               AS date,
               m.text               AS text,
               m.attributedBody     AS attributed_body,
               m.is_from_me         AS is_from_me,
               m.cache_has_attachments AS has_attachment,
               h.id                 AS sender
        FROM chat c
        JOIN chat_message_join cmj ON c.ROWID = cmj.chat_id
        JOIN message m             ON m.ROWID = cmj.message_id
        LEFT JOIN handle h         ON m.handle_id = h.ROWID
        WHERE (c.display_name    LIKE '%' || ? || '%'
            OR c.chat_identifier LIKE '%' || ? || '%')
          AND m.ROWID > ?
          AND m.associated_message_type = 0   -- exclude reactions/tapbacks
          AND m.item_type = 0                 -- exclude system events
        ORDER BY m.date ASC, m.ROWID ASC
        """,
        (match, match, since_rowid),
    ).fetchall()
    cols = ("rowid", "date", "text", "attributed_body", "is_from_me",
            "has_attachment", "sender")
    return [dict(zip(cols, row)) for row in rows]


def message_text(row: dict) -> str | None:
    """Resolve display text for a message row, applying attachment/blank rules."""
    text = row["text"]
    if not text:
        text = decode_attributed_body(row["attributed_body"])
    if text:
        # Some senders' line breaks arrive as \r\n or a bare \r. Normalize so a
        # stray \r never lands in the markdown.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = text.replace(OBJ_REPLACEMENT, "").strip()
    if text:
        if REACTION_RE.match(text):
            return None  # SMS-delivered tapback — drop it
        return text
    if row["has_attachment"]:
        return "[attachment]"
    return None  # nothing to render


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def frontmatter(topics: list[str], attendees: list[str],
                skip_pii: bool = False) -> str:
    # `topics` and `attendees` are the only frontmatter keys memory-vault embeds
    # into every chunk, so those are all we emit — they make the thread findable
    # by group and by participant even when a chunk doesn't name them.
    # `skip_pii` is the exception: memory-vault reads it to skip the nightly PII
    # scan for this note. Written only when true, so the default (scan on) leaves
    # every existing file's frontmatter unchanged.
    lines = []
    if topics:
        lines.append("topics: " + ", ".join(topics))
    if attendees:
        lines.append("attendees: " + ", ".join(attendees))
    if skip_pii:
        lines.append("skip_pii: true")
    if not lines:
        return ""
    return "---\n" + "\n".join(lines) + "\n---\n"


def render_lines(messages: list[dict], resolver: NameResolver, me: str,
                 tz: ZoneInfo, start_date: str | None) -> tuple[list[str], str | None]:
    """Render messages to markdown lines, emitting a `### YYYY-MM-DD (Ddd)` heading
    whenever the date changes. `start_date` is the last date already written to the
    file so we don't repeat a heading across runs. Returns (lines, last_date)."""
    lines: list[str] = []
    current_date = start_date
    for row in messages:
        text = message_text(row)
        if text is None:
            continue
        dt = apple_to_datetime(row["date"], tz)
        day = dt.strftime("%Y-%m-%d")
        if day != current_date:
            if lines:
                lines.append("")
            lines.append(f"### {dt.strftime('%Y-%m-%d (%a)')}")
            current_date = day
        name = me if row["is_from_me"] else resolver.resolve(row["sender"])
        stamp = dt.strftime("%-I:%M %p")
        first = True
        for chunk in text.split("\n"):
            if first:
                lines.append(f"**{name}** ({stamp}): {chunk}")
                first = False
            else:
                lines.append(chunk)
    return lines, current_date


# --------------------------------------------------------------------------- #
# Per-thread processing
# --------------------------------------------------------------------------- #
# A day heading exactly as render_lines writes it. Used to find where a late
# message belongs in an existing file.
HEADING_RE = re.compile(r"^### (\d{4}-\d{2}-\d{2}) \([A-Za-z]{3}\)$")


def split_frontmatter(content: str) -> str:
    """Return the body of an existing markdown file, dropping any frontmatter."""
    if content.startswith("---\n"):
        end = content.find("\n---\n", 4)
        if end != -1:
            return content[end + len("\n---\n"):].lstrip("\n")
    return content


def year_file(out_root: Path, name: str, year: int) -> Path:
    """Per-year file: <output_dir>/<name>/<name> - <year>.md."""
    return out_root / name / f"{name} - {year}.md"


def read_raw(path: Path) -> str:
    """Read a file exactly as stored (no newline translation)."""
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def insert_late(body: list[str], day: str, heading: str,
                msg_lines: list[str]) -> list[str]:
    """Put messages from an earlier `day` into the right place in `body`.

    They go at the end of that day's section if it exists; otherwise a new
    section is created just before the first later day."""
    sections = [(i, m.group(1)) for i, line in enumerate(body)
                if (m := HEADING_RE.match(line))]
    for k, (start, d) in enumerate(sections):
        if d == day:
            end = sections[k + 1][0] if k + 1 < len(sections) else len(body)
            while end > start + 1 and body[end - 1] == "":
                end -= 1  # stay above the blank line before the next heading
            return body[:end] + msg_lines + body[end:]
    for start, d in sections:
        if d > day:
            block = [heading] + msg_lines + [""]
            if start > 0 and body[start - 1] != "":
                block = [""] + block
            return body[:start] + block + body[start:]
    # Not before any existing day (not expected for a late message): append.
    return body + ([""] if body else []) + [heading] + msg_lines


def written_years(pending: dict | None, out_root: Path, name: str) -> dict[str, dict]:
    """Years that an interrupted earlier run already wrote.

    Before writing any file, a run records in state.json a hash of what each
    file will contain. If a file on disk matches its hash, that write landed
    and its messages must not be added again."""
    done: dict[str, dict] = {}
    for ykey, info in ((pending or {}).get("years") or {}).items():
        path = year_file(out_root, name, int(ykey))
        if path.exists() and sha256(read_raw(path)) == info.get("sha"):
            done[ykey] = info
    return done


def process_thread(thread: dict, conn: sqlite3.Connection, resolver: NameResolver,
                   cfg: dict, state: dict, tz: ZoneInfo, rebuild: bool,
                   log, save=None) -> bool:
    """Append a thread's new messages to its per-year files.

    `save()` persists `state`; it is called before files are written (to record
    what is about to be written) and again after, so a run stopped at any point
    never adds the same messages twice."""
    name = thread["name"]
    match = thread.get("match", name)
    me = cfg["me"]
    out_root = Path(cfg["output_dir"]).expanduser()
    save = save or (lambda: None)

    committed = dict(state.get(name, {}))
    pending = committed.pop("pending", None)
    # Normalize per-year state to {"last_date", "attendees"} (older state stored
    # just the date string).
    years_state: dict[str, dict] = {}
    for y, v in (committed.get("years", {}) or {}).items():
        years_state[y] = v if isinstance(v, dict) else {"last_date": v, "attendees": []}

    done = written_years(pending, out_root, name)
    done_upto = int(pending.get("last_rowid", 0)) if done else 0
    for ykey, info in done.items():
        years_state[ykey] = info["state"]

    topics = thread.get("topics", ["Text Messages", "iMessage", name])
    skip_pii = bool(thread.get("skip_pii", False))

    since = 0 if rebuild else int(committed.get("last_rowid", 0))
    fetched = fetch_messages(conn, match, since)
    if not fetched:
        log(f"  {name}: no new messages (watermark ROWID {since})")
        return False

    # Watermark = highest ROWID seen. ROWID (insertion order) and date (send
    # time) can diverge, so never use the last message in date order for this.
    max_rowid = max(m["rowid"] for m in fetched)

    def day_of(m: dict) -> datetime:
        return apple_to_datetime(m["date"], tz)

    # Route each message to its calendar year (local time). Only the year(s)
    # with new messages get rewritten; earlier years stay frozen. Skip messages
    # an interrupted run already wrote (unless rebuilding from scratch).
    by_year: dict[int, list[dict]] = {}
    for m in fetched:
        year = day_of(m).year
        if not rebuild and str(year) in done and m["rowid"] <= done_upto:
            continue
        by_year.setdefault(year, []).append(m)

    # Build every file's new content in memory first.
    writes: dict[str, tuple[Path, str, dict, int]] = {}
    for year in sorted(by_year):
        ykey = str(year)
        batch = by_year[year]
        out_file = year_file(out_root, name, year)
        body: list[str] = []
        if rebuild:
            yinfo = {"last_date": None, "attendees": []}
        else:
            yinfo = years_state.get(ykey, {"last_date": None, "attendees": []})
            if out_file.exists():
                existing = split_frontmatter(
                    out_file.read_text(encoding="utf-8")).rstrip("\n")
                body = existing.split("\n") if existing else []
        last_date = yinfo.get("last_date")
        changed = False

        # Messages from a day already written (they arrived late) go into that
        # day's section instead of after everything else.
        late: dict[str, list[dict]] = {}
        current: list[dict] = []
        for m in batch:
            day = day_of(m).strftime("%Y-%m-%d")
            if body and last_date and day < last_date:
                late.setdefault(day, []).append(m)
            else:
                current.append(m)
        for day in sorted(late):
            lines, _ = render_lines(late[day], resolver, me, tz, day)
            if lines:
                heading = f"### {day_of(late[day][0]).strftime('%Y-%m-%d (%a)')}"
                body = insert_late(body, day, heading, lines)
                changed = True

        new_lines, new_last = render_lines(current, resolver, me, tz, last_date)
        if new_lines:
            if body and new_lines[0].startswith("### "):
                body.append("")  # blank line before a new day, as on a fresh run
            body += new_lines
            last_date = new_last
            changed = True
        if not changed:
            continue

        # Attendees: everyone already recorded for the year + whoever actually
        # said something in this batch (so a frozen file's roster survives
        # incremental appends, and a reaction alone doesn't count).
        names = set(yinfo.get("attendees", []))
        for m in batch:
            if message_text(m) is not None:
                names.add(me if m["is_from_me"] else resolver.resolve(m["sender"]))
        attendees = sorted(names)
        text = frontmatter(topics, attendees, skip_pii) + "\n" + "\n".join(body) + "\n"
        writes[ykey] = (out_file, text, {"last_date": last_date, "attendees": attendees},
                        len(batch))

    if writes:
        # 1) Record what is about to be written. Years an earlier interrupted
        #    run wrote stay listed until this run commits.
        intent = {k: v for k, v in done.items() if k not in writes}
        for ykey, (_, text, ystate, _) in writes.items():
            intent[ykey] = {"sha": sha256(text), "state": ystate}
        state[name] = {**committed, "pending": {"last_rowid": max_rowid, "years": intent}}
        save()
        # 2) Write the files (each one atomically).
        (out_root / name).mkdir(parents=True, exist_ok=True)
        for ykey, (out_file, text, ystate, count) in sorted(writes.items()):
            atomic_write(out_file, text)
            years_state[ykey] = ystate
            log(f"  {name} {ykey}: {'rebuilt' if rebuild else 'updated'} "
                f"(+{count} msg) -> {out_file}")
    else:
        log(f"  {name}: {len(fetched)} row(s) had no renderable text")

    # 3) Commit: new watermark, no pending writes.
    state[name] = {"last_rowid": max_rowid, "years": years_state}
    save()
    return bool(writes)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pull iMessage group threads into the memory vault.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--rebuild", action="store_true",
                        help="Regenerate each thread file from scratch (safe when retention is Forever).")
    parser.add_argument("--refresh-contacts", action="store_true",
                        help="Re-resolve all names from Contacts, ignoring the cache.")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    def log(msg: str) -> None:
        if not args.quiet:
            print(msg, flush=True)

    cfg = load_config(args.config)
    tz = ZoneInfo(cfg.get("timezone", "UTC"))
    db_path = Path(cfg.get("chat_db", "~/Library/Messages/chat.db")).expanduser()
    if not db_path.exists():
        log(f"ERROR: chat.db not found at {db_path}")
        return 1

    cache = load_json(CACHE_PATH)
    state = load_json(STATE_PATH)
    resolver = NameResolver(cfg.get("contacts", {}), cache, refresh=args.refresh_contacts)

    log(f"thread-ledger: {datetime.now(tz).isoformat(timespec='seconds')}")
    try:
        conn = open_readonly(db_path)
        conn.execute("SELECT 1 FROM chat LIMIT 1")  # force a real read now
    except sqlite3.OperationalError:
        log(
            "ERROR: cannot read the Messages database. Grant Full Disk Access to "
            f"the interpreter running this script ({sys.executable}) in "
            "System Settings > Privacy & Security > Full Disk Access, then retry."
        )
        return 2
    except sqlite3.DatabaseError as exc:
        log(f"ERROR: {db_path} is not a readable Messages database ({exc}).")
        return 2
    def save() -> None:
        # Saved after every thread, so a run stopped partway keeps the progress
        # of the threads it finished.
        save_json(CACHE_PATH, cache)
        save_json(STATE_PATH, state)

    changed = False
    try:
        for thread in cfg.get("threads", []):
            if process_thread(thread, conn, resolver, cfg, state, tz, args.rebuild, log,
                              save=save):
                changed = True
    finally:
        conn.close()

    save()
    log("done" + ("" if changed else " (nothing to write)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

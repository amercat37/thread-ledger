#!/usr/bin/env python3
"""thread-ledger — pull iMessage group threads into markdown for the memory vault.

Reads the local Messages database (~/Library/Messages/chat.db), filters to the
group threads named in config.toml, resolves phone numbers to names (manual
overrides -> cache -> macOS Contacts -> raw number), and appends any new
messages to a per-thread markdown file. Runtime uses the standard library only
so it can run unattended under launchd with no venv.

Usage:
    thread_ledger.py [--config PATH] [--rebuild] [--refresh-contacts] [--quiet]
"""
from __future__ import annotations

import argparse
import json
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


def save_json(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")


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
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 10:
        return digits[-10:]
    return raw.strip().lower()


def addressbook_dbs() -> list[Path]:
    base = Path.home() / "Library" / "Application Support" / "AddressBook"
    dbs = [base / "AddressBook-v22.abcddb"]
    dbs += sorted(base.glob("Sources/*/AddressBook-v22.abcddb"))
    return [p for p in dbs if p.exists()]


def build_addressbook_map() -> dict[str, str]:
    """Map norm_key -> full name from every Contacts database."""
    result: dict[str, str] = {}
    for db in addressbook_dbs():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
        except sqlite3.Error:
            continue
        try:
            rows = conn.execute(
                """
                SELECT p.ZFULLNUMBER,
                       COALESCE(r.ZFIRSTNAME, ''),
                       COALESCE(r.ZLASTNAME, ''),
                       COALESCE(r.ZORGANIZATION, '')
                FROM ZABCDPHONENUMBER p
                JOIN ZABCDRECORD r ON p.ZOWNER = r.Z_PK
                WHERE p.ZFULLNUMBER IS NOT NULL
                """
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
        for number, first, last, org in rows:
            name = " ".join(part for part in (first.strip(), last.strip()) if part)
            name = name or org.strip()
            key = norm_key(number)
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
    """Open the Messages DB read-only + immutable so we never block or disturb
    the live database (which Messages keeps open in WAL mode)."""
    return sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)


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
def frontmatter(topics: list[str], attendees: list[str]) -> str:
    # `topics` and `attendees` are the only frontmatter keys memory-vault embeds
    # into every chunk, so those are all we emit — they make the thread findable
    # by group and by participant even when a chunk doesn't name them.
    lines = []
    if topics:
        lines.append("topics: " + ", ".join(topics))
    if attendees:
        lines.append("attendees: " + ", ".join(attendees))
    if not lines:
        return ""
    return "---\n" + "\n".join(lines) + "\n---\n"


def render_lines(messages: list[dict], resolver: NameResolver, me: str,
                 tz: ZoneInfo, start_date: str | None) -> tuple[list[str], str | None]:
    """Render messages to markdown lines, emitting a `## YYYY-MM-DD (Ddd)` heading
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


def process_thread(thread: dict, conn: sqlite3.Connection, resolver: NameResolver,
                   cfg: dict, state: dict, tz: ZoneInfo, rebuild: bool,
                   log) -> bool:
    name = thread["name"]
    match = thread.get("match", name)
    out_root = Path(cfg["output_dir"]).expanduser()
    thread_dir = out_root / name

    tstate = {} if rebuild else state.get(name, {})
    since = int(tstate.get("last_rowid", 0))
    # Normalize per-year state to {"last_date", "attendees"} (older state stored
    # just the date string).
    years_state: dict[str, dict] = {}
    for y, v in (tstate.get("years", {}) or {}).items():
        years_state[y] = v if isinstance(v, dict) else {"last_date": v, "attendees": []}

    topics = thread.get("topics", ["Text Messages", "iMessage", name])

    if rebuild and thread_dir.exists():
        for stale in thread_dir.glob(f"{name} - *.md"):
            stale.unlink()

    messages = fetch_messages(conn, match, since)
    if not messages:
        log(f"  {name}: no new messages (watermark ROWID {since})")
        return False

    # Watermark = highest ROWID seen. ROWID (insertion order) and date (send
    # time) can diverge, so never use the last message in date order for this.
    max_rowid = max(m["rowid"] for m in messages)

    # Route each message to its calendar year (local time). Only the year(s)
    # with new messages get rewritten; earlier years stay frozen.
    by_year: dict[int, list[dict]] = {}
    for m in messages:
        by_year.setdefault(apple_to_datetime(m["date"], tz).year, []).append(m)

    thread_dir.mkdir(parents=True, exist_ok=True)
    wrote_any = False
    for year in sorted(by_year):
        ykey = str(year)
        yinfo = years_state.get(ykey, {"last_date": None, "attendees": []})
        out_file = year_file(out_root, name, year)
        existing_body = ""
        if out_file.exists():
            existing_body = split_frontmatter(out_file.read_text(encoding="utf-8")).rstrip("\n")
        new_lines, last_date = render_lines(
            by_year[year], resolver, cfg["me"], tz, yinfo.get("last_date"))
        if not new_lines:
            continue
        # Merge attendees: everyone already recorded for the year + this batch's
        # senders (so a frozen file's roster survives incremental appends).
        names = set(yinfo.get("attendees", []))
        for m in by_year[year]:
            names.add(cfg["me"] if m["is_from_me"] else resolver.resolve(m["sender"]))
        attendees = sorted(names)
        body = "\n".join(new_lines) if not existing_body \
            else existing_body + "\n" + "\n".join(new_lines)
        out_file.write_text(
            frontmatter(topics, attendees) + "\n" + body + "\n",
            encoding="utf-8")
        years_state[ykey] = {"last_date": last_date, "attendees": attendees}
        wrote_any = True
        log(f"  {name} {year}: {'rebuilt' if rebuild else 'updated'} "
            f"(+{len(by_year[year])} msg) -> {out_file}")

    state[name] = {"last_rowid": max_rowid, "years": years_state}
    if not wrote_any:
        log(f"  {name}: {len(messages)} row(s) had no renderable text")
    return wrote_any


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
    changed = False
    try:
        for thread in cfg.get("threads", []):
            if process_thread(thread, conn, resolver, cfg, state, tz, args.rebuild, log):
                changed = True
    finally:
        conn.close()

    save_json(CACHE_PATH, cache)
    save_json(STATE_PATH, state)
    log("done" + ("" if changed else " (nothing to write)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

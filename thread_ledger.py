#!/usr/bin/env python3
"""thread-ledger — pull iMessage threads into markdown for the memory vault.

Reads the local Messages database (~/Library/Messages/chat.db), picks the chats
to save (the ones listed in config.toml, plus — when `auto_groups` is on — new
group chats that pass the configured rules), resolves phone numbers to names
(manual overrides -> cache -> macOS Contacts -> raw number), and appends any new
messages to per-year markdown files (<folder>/<folder> - <year>.md). Chats are
tracked by their Messages chat ID, so renames never split a history. Runtime
uses the standard library only so it can run unattended under launchd.

Usage:
    thread_ledger.py [--config PATH] [--rebuild] [--refresh-contacts] [--quiet]
    thread_ledger.py --list-chats [--all]     # preview; changes nothing
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tomllib
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Seconds between the Unix epoch (1970-01-01) and the Apple epoch (2001-01-01).
APPLE_EPOCH_OFFSET = 978_307_200

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "config.toml"
CACHE_PATH = SCRIPT_DIR / "contacts_cache.json"
STATE_PATH = SCRIPT_DIR / "state.json"
STATE_VERSION = 2

# chat.style values in the Messages database.
STYLE_GROUP = 43
STYLE_ONE_ON_ONE = 45

# Top-level settings and their defaults. Conservative on purpose: someone who
# upgrades without touching config.toml gets exactly the old behaviour.
DEFAULTS = {
    "auto_groups": False,           # save new group chats automatically
    "min_messages": 3,              # ...with at least this many messages
    "active_within_days": 365,      # ...and a message within this many days
    "require_known_contact": True,  # ...and someone in it is in your Contacts
    "exclude": [],                  # chat IDs never to save
    "start_date": "",               # "YYYY-MM-DD": ignore anything earlier
    "skip_pii": False,              # default skip_pii for every saved chat
}

# Unicode Object Replacement Character — iMessage uses it as an inline
# placeholder where an attachment sits in the text.
OBJ_REPLACEMENT = "￼"

# Tapbacks from SMS/RCS members arrive as plain-text messages (with
# associated_message_type = 0, so the type-code filter can't catch them). They
# always take an auto-generated form; drop them to honor the no-reactions rule.
#   iPhone SMS:          Loved an image / Laughed at "…" / Removed a heart from "…"
#   Android/RCS:         Reacted 😂 to "…"
#   iOS 18 emoji (SMS):  ❤️ to "…"   (often wrapped in zero-width spaces)
#   ...and for an attachment, the object is e.g. "an image" / "a photo".
_QUOTED = r"[\"“].*[\"”]"
_OBJECT = (r"(an image|a photo|an? (animated )?GIF|a movie|a video|a sticker|an audio message"
           r"|a voice message|an attachment|a file|a link|a location|a contact|a message"
           r"|the message|" + _QUOTED + r")")
REACTION_RES = (
    re.compile(r"^(Loved|Liked|Disliked|Laughed at|Emphasized|Questioned) " + _OBJECT + r"$",
               re.DOTALL),
    re.compile(r"^Removed an? \w+ from " + _OBJECT + r"$", re.DOTALL),
    re.compile(r"^Reacted [^\w\s]{1,16} to " + _OBJECT + r"$", re.DOTALL),
    re.compile(r"^​?[^\w\s​]{1,16}​? to " + _OBJECT + r"$", re.DOTALL),
)

# Characters that Obsidian forbids in file names or that break [[links]].
# Commas are removed too: memory-vault splits `topics:` on commas, and the
# folder name is one of the topics.
_UNSAFE_NAME_RE = re.compile(r'[*"\\/<>:|?#^\[\],]')


class ConfigError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Config / state / cache
# --------------------------------------------------------------------------- #
def load_config(path: Path) -> dict:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def settings(cfg: dict) -> dict:
    """Top-level settings with defaults filled in, validated."""
    s = {k: cfg.get(k, v) for k, v in DEFAULTS.items()}
    for key in ("auto_groups", "require_known_contact", "skip_pii"):
        if not isinstance(s[key], bool):
            raise ConfigError(f"{key} must be true or false")
    for key in ("min_messages", "active_within_days"):
        if not isinstance(s[key], int) or isinstance(s[key], bool) or s[key] < 0:
            raise ConfigError(f"{key} must be a whole number, 0 or more")
    if not isinstance(s["exclude"], list) or not all(isinstance(x, str) for x in s["exclude"]):
        raise ConfigError('exclude must be a list of chat IDs, e.g. exclude = ["chat123"]')
    if s["start_date"]:
        try:
            datetime.strptime(s["start_date"], "%Y-%m-%d")
        except (TypeError, ValueError):
            raise ConfigError('start_date must look like "2020-01-31" (or be blank)')
    threads = cfg.get("threads", [])
    if not isinstance(threads, list):
        raise ConfigError("[[threads]] entries must be written as [[threads]] blocks")
    for t in threads:
        if not isinstance(t, dict):
            raise ConfigError("each [[threads]] entry must be a table")
        if "chats" in t and (not isinstance(t["chats"], list)
                             or not all(isinstance(x, str) for x in t["chats"])):
            raise ConfigError('chats must be a list of chat IDs, e.g. chats = ["chat123"]')
        if not t.get("chats") and not t.get("match") and not t.get("name"):
            raise ConfigError("each [[threads]] entry needs chats, match or name")
        for key in ("skip_pii", "follow_renames"):
            if key in t and not isinstance(t[key], bool):
                raise ConfigError(f"{key} must be true or false")
        if "topics" in t and (not isinstance(t["topics"], list)
                              or not all(isinstance(x, str) for x in t["topics"])):
            raise ConfigError("topics must be a list of strings")
    return s


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


def datetime_to_apple_ns(dt: datetime) -> int:
    return int((dt.timestamp() - APPLE_EPOCH_OFFSET) * 1_000_000_000)


def apple_ns(raw: int | None) -> int:
    """A Messages timestamp in nanoseconds, whichever unit it was stored in."""
    if not raw:
        return 0
    return raw if raw > 100_000_000_000 else raw * 1_000_000_000


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

    def lookup(self, handle: str | None) -> str | None:
        """The person's name, or None if they aren't known."""
        key = norm_key(handle)
        if not key:
            return None
        if key in self.overrides:
            return self.overrides[key]
        if not self.refresh and key in self.cache:
            return self.cache[key]
        name = self._addressbook().get(key)
        if name:
            self.cache[key] = name
        return name

    def resolve(self, handle: str | None) -> str:
        # Fallback: keep the raw handle so an unknown sender is still traceable.
        return self.lookup(handle) or handle or "Unknown"


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


# A "real" message: not a reaction/tapback, not a system event.
_REAL = "m.associated_message_type = 0 AND m.item_type = 0"


@dataclass
class Chat:
    """One row of the Messages `chat` table, plus what the rules need."""
    rowid: int
    guid: str
    ident: str          # chat_identifier: the chat ID shown to the user
    name: str           # display name ("" for unnamed chats)
    is_group: bool
    service: str
    count: int = 0      # real messages
    first: int = 0      # Apple ns of first / last real message (0 = none)
    last: int = 0
    members: list[str] = field(default_factory=list)   # current members
    senders: list[str] = field(default_factory=list)   # anyone who wrote

    @property
    def participants(self) -> list[str]:
        """Everyone who is or was in the chat (members + past senders)."""
        seen: set[str] = set()
        out = []
        for handle in self.members + self.senders:
            key = norm_key(handle)
            if key and key not in seen:
                seen.add(key)
                out.append(handle)
        return out


def load_chats(conn: sqlite3.Connection) -> list[Chat]:
    """Every chat in the database with its message count, dates and people."""
    chats: dict[int, Chat] = {}
    for rowid, guid, ident, name, style, service in conn.execute(
            "SELECT ROWID, COALESCE(guid, ''), COALESCE(chat_identifier, ''), "
            "COALESCE(display_name, ''), COALESCE(style, 0), COALESCE(service_name, '') "
            "FROM chat"):
        chats[rowid] = Chat(rowid, guid or f"rowid:{rowid}", ident,
                            " ".join(name.split()), style == STYLE_GROUP, service)
    for cid, count, first, last in conn.execute(
            f"SELECT j.chat_id, COUNT(*), MIN(m.date), MAX(m.date) "
            f"FROM chat_message_join j JOIN message m ON m.ROWID = j.message_id "
            f"WHERE {_REAL} GROUP BY j.chat_id"):
        if cid in chats:
            chats[cid].count = count
            chats[cid].first = apple_ns(first)
            chats[cid].last = apple_ns(last)
    for cid, handle in conn.execute(
            "SELECT j.chat_id, h.id FROM chat_handle_join j "
            "JOIN handle h ON h.ROWID = j.handle_id ORDER BY j.chat_id, h.ROWID"):
        if cid in chats and handle:
            chats[cid].members.append(handle)
    for cid, handle in conn.execute(
            f"SELECT DISTINCT j.chat_id, h.id FROM chat_message_join j "
            f"JOIN message m ON m.ROWID = j.message_id "
            f"JOIN handle h ON h.ROWID = m.handle_id "
            f"WHERE m.is_from_me = 0 AND {_REAL}"):
        if cid in chats and handle:
            chats[cid].senders.append(handle)
    return sorted(chats.values(), key=lambda c: c.rowid)


def fetch_chat_messages(conn: sqlite3.Connection, chat_rowid: int, since_rowid: int,
                        start_ns: int = 0) -> list[dict]:
    """Real messages (no reactions/tapbacks, no system events) in one chat,
    newer than `since_rowid` and not before `start_ns`, oldest first.
    (sync_thread passes no start_ns: it filters by date itself, so the
    watermark still moves past skipped old messages.)"""
    rows = conn.execute(
        f"""
        SELECT m.ROWID, m.date, m.text, m.attributedBody, m.is_from_me,
               m.cache_has_attachments, h.id
        FROM chat_message_join cmj
        JOIN message m      ON m.ROWID = cmj.message_id
        LEFT JOIN handle h  ON m.handle_id = h.ROWID
        WHERE cmj.chat_id = ?
          AND m.ROWID > ?
          AND {_REAL}
        ORDER BY m.date ASC, m.ROWID ASC
        """,
        (chat_rowid, since_rowid),
    ).fetchall()
    cols = ("rowid", "date", "text", "attributed_body", "is_from_me",
            "has_attachment", "sender")
    out = [dict(zip(cols, row)) for row in rows]
    if start_ns:
        out = [m for m in out if apple_ns(m["date"]) >= start_ns]
    kinds = attachment_kinds(conn, [m["rowid"] for m in out if m["has_attachment"]])
    for m in out:
        m["attachments"] = kinds.get(m["rowid"], [])
    return out


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def attachment_kind(mime: str | None, uti: str | None, name: str | None,
                    sticker: bool, voice: bool) -> str | None:
    """What an attachment is, for its label; None for link previews and other
    hidden app data (they aren't something the person sent)."""
    mime, uti, name = (mime or "").lower(), (uti or "").lower(), (name or "").lower()
    if name.endswith(".pluginpayloadattachment"):
        return None
    if sticker:
        return "sticker"
    if voice or uti == "com.apple.coreaudio-format":
        return "voice message"
    if mime == "image/gif" or uti == "com.compuserve.gif":
        return "GIF"
    if mime.startswith("image/") or uti in ("public.jpeg", "public.heic", "public.png"):
        return "photo"
    if mime.startswith("video/") or uti in ("com.apple.quicktime-movie", "public.mpeg-4"):
        return "video"
    if mime.startswith("audio/"):
        return "audio file"
    if mime in ("text/vcard", "text/x-vcard") or uti == "public.vcard":
        return "contact card"
    return "file"


def attachment_kinds(conn: sqlite3.Connection, rowids: list[int]) -> dict[int, list[str]]:
    """message ROWID -> kinds of its attachments, in order. Empty if the
    database has no attachment tables (older versions, or test fixtures)."""
    out: dict[int, list[str]] = {}
    if not rowids:
        return out
    acols = _columns(conn, "attachment")
    if not acols or not _columns(conn, "message_attachment_join"):
        return out
    pick = lambda col: f"a.{col}" if col in acols else "NULL"  # noqa: E731
    voice = "m.is_audio_message" if "is_audio_message" in _columns(conn, "message") else "0"
    for i in range(0, len(rowids), 500):
        chunk = rowids[i:i + 500]
        for mid, mime, uti, name, sticker, is_voice in conn.execute(
                f"SELECT j.message_id, {pick('mime_type')}, {pick('uti')}, "
                f"{pick('transfer_name')}, {pick('is_sticker')}, {voice} "
                f"FROM message_attachment_join j "
                f"JOIN attachment a ON a.ROWID = j.attachment_id "
                f"JOIN message m ON m.ROWID = j.message_id "
                f"WHERE j.message_id IN ({','.join('?' * len(chunk))}) "
                f"ORDER BY j.message_id, a.ROWID", chunk):
            kind = attachment_kind(mime, uti, name, bool(sticker), bool(is_voice))
            if kind:
                out.setdefault(mid, []).append(kind)
    return out


_PLURAL = {"photo": "photos", "video": "videos", "GIF": "GIFs", "sticker": "stickers",
           "voice message": "voice messages", "audio file": "audio files",
           "contact card": "contact cards", "file": "files"}


def attachment_label(kinds: list[str]) -> str:
    """["photo", "photo", "video"] -> "[2 photos, video]"."""
    counts: dict[str, int] = {}
    for k in kinds:
        counts[k] = counts.get(k, 0) + 1
    return "[" + ", ".join(k if n == 1 else f"{n} {_PLURAL.get(k, k + 's')}"
                           for k, n in counts.items()) + "]"


def message_text(row: dict) -> str | None:
    """Resolve display text for a message row, applying attachment/blank rules.

    Attachments are labelled by kind — "[photo]", "[2 photos, video]",
    "[voice message]" — after any caption; "[attachment]" when the kind isn't
    known. The files themselves are never copied."""
    text = row["text"]
    if not text:
        text = decode_attributed_body(row["attributed_body"])
    if text:
        # Some senders' line breaks arrive as \r\n or a bare \r. Normalize so a
        # stray \r never lands in the markdown.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = text.replace(OBJ_REPLACEMENT, "").strip()
    if text and any(r.match(text) for r in REACTION_RES):
        return None  # SMS/RCS-delivered tapback — drop it
    kinds = row.get("attachments") or []
    if text:
        return f"{text} {attachment_label(kinds)}" if kinds else text
    if kinds:
        return attachment_label(kinds)
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
    # by group and by participant even when a chunk doesn't name them. Both are
    # comma-separated lists, so a comma inside one value is dropped.
    # `skip_pii` is the exception: memory-vault reads it to skip the nightly PII
    # scan for this note. Written only when true.
    clean = lambda vals: [v.replace(",", "") for v in vals]  # noqa: E731
    lines = []
    if topics:
        lines.append("topics: " + ", ".join(clean(topics)))
    if attendees:
        lines.append("attendees: " + ", ".join(clean(attendees)))
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
# Folder names
# --------------------------------------------------------------------------- #
def clean_name(raw: str) -> str:
    """Make `raw` safe as an Obsidian folder/file name (and as a topic)."""
    s = unicodedata.normalize("NFC", raw or "")
    s = re.sub(r"\s", " ", s)          # tabs/newlines become spaces, not nothing
    # Drop control and invisible format characters, but keep the joiners that
    # hold emoji sequences together.
    s = "".join(ch for ch in s if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co")
                or ch in "‍️")
    s = _UNSAFE_NAME_RE.sub("", s)
    s = " ".join(s.split()).strip(". ")
    return s[:80].rstrip(". ")


def first_name(full: str) -> str:
    token = full.split()[0] if full.split() else full
    # A card like "🍕 Pizza Palace" has no real first name; keep it whole.
    return token if any(ch.isalpha() for ch in token) else full


def people_label(handles: list[str], resolver: NameResolver, me: str) -> str:
    """Name an unnamed group after its people: up to three known first names
    (alphabetical) joined by " & ", then "+N" for everyone else."""
    known: dict[str, str] = {}   # full name -> first name (one per person)
    unknown = 0
    for handle in handles:
        name = resolver.lookup(handle)
        if name is None:
            unknown += 1
        elif name != me:
            known.setdefault(name, first_name(name))
    firsts = sorted(known.values(), key=lambda n: (n.casefold(), n))
    extra = max(0, len(firsts) - 3) + unknown
    if not firsts:
        return f"Unknown +{unknown}" if unknown else "Unknown"
    return " & ".join(firsts[:3]) + (f" +{extra}" if extra else "")


def unique_folder(base: str, ident: str, taken: set[str]) -> str:
    """`base`, or `base` plus the end of the chat ID if that name is taken.
    Compared without case: macOS and memory-vault both ignore case."""
    if base.casefold() not in taken:
        return base
    tail = re.sub(r"[^0-9A-Za-z]", "", ident) or "chat"
    if re.fullmatch(r"chat\d+", tail):
        tail = tail[4:]               # "chat123456789012345678" -> its digits
    for n in (4, 6, 8, len(tail)):
        cand = f"{base} {tail[-n:]}"
        if cand.casefold() not in taken:
            return cand
    i = 2
    while f"{base} {tail} {i}".casefold() in taken:
        i += 1
    return f"{base} {tail} {i}"


# --------------------------------------------------------------------------- #
# Choosing which chats to save
# --------------------------------------------------------------------------- #
@dataclass
class Thread:
    """One folder in the vault: one or more chats saved together."""
    tid: str
    folder: str
    base: str            # folder name before any duplicate suffix
    name_source: str     # "config" | "display" | "people"
    chats: list[Chat]
    topics: list[str] | None   # None = default (Text Messages, iMessage, folder)
    skip_pii: bool
    origin: str          # "config" | "saved" | "auto"
    rename_to: str | None = None
    rename_base: str | None = None


def entry_label(entry: dict) -> str:
    return entry.get("name") or ", ".join(entry.get("chats", [])) or entry.get("match", "?")


def resolve_entry(entry: dict, chats: list[Chat]) -> list[Chat]:
    """The chats a [[threads]] entry refers to.

    `chats = [...]` lists chat IDs (chat_identifier, or a full guid). The older
    `match = "..."` still works: a group whose name contains it, or a chat whose
    ID is exactly it (a one-on-one's ID is the person's number or email). With
    neither, `name` is used as `match`, as in earlier versions."""
    ids = set(entry.get("chats") or [])
    match = entry.get("match")
    if not ids and not match:
        match = entry.get("name")
    out = []
    for c in chats:
        if c.ident in ids or c.guid in ids:
            out.append(c)
        elif match and ((c.is_group and match.casefold() in c.name.casefold())
                        or (c.ident and norm_key(c.ident) == norm_key(match))):
            out.append(c)
    return out


def _known_people(chat: Chat, resolver: NameResolver, me: str) -> int:
    return sum(1 for h in chat.participants
               if (n := resolver.lookup(h)) is not None and n != me)


def auto_skip_reason(chat: Chat, s: dict, resolver: NameResolver, me: str,
                     now_ns: int) -> str | None:
    """Why auto mode would not save this group (None = it would)."""
    reasons = []
    if chat.count < s["min_messages"]:
        reasons.append(f"under {s['min_messages']} messages")
    cutoff = now_ns - s["active_within_days"] * 86_400 * 1_000_000_000
    if not chat.last or chat.last < cutoff:
        reasons.append(f"no messages in {s['active_within_days']} days")
    if s["require_known_contact"] and not _known_people(chat, resolver, me):
        reasons.append("nobody in Contacts")
    return "; ".join(reasons) or None


def desired_name(entry: dict | None, chats: list[Chat], resolver: NameResolver,
                 me: str) -> tuple[str, str]:
    """(folder base name, where it came from) for a set of chats."""
    if entry and entry.get("name") and clean_name(entry["name"]):
        return clean_name(entry["name"]), "config"
    named = [c for c in chats if clean_name(c.name)]
    if named:
        newest = max(named, key=lambda c: (c.last, c.rowid))
        return clean_name(newest.name), "display"
    if chats and not any(c.is_group for c in chats):
        names = [resolver.lookup(c.ident) for c in chats]
        name = next((n for n in names if n), None)
        return (clean_name(name) or "Unknown") if name else "Unknown", "people"
    handles: list[str] = []
    for c in chats:
        handles += c.participants
    return clean_name(people_label(handles, resolver, me)) or "Unknown", "people"


def plan_threads(cfg: dict, chats: list[Chat], state: dict, resolver: NameResolver,
                 now: datetime, log, report: dict | None = None) -> list[Thread]:
    """Decide which threads to save this run and what their folders are called.

    Mutates `state` only to join saved threads that a [[threads]] entry now
    lists together (the absorbed thread's state is removed). `report`, when
    given, receives guid -> (status, detail) for every chat (--list-chats)."""
    s = settings(cfg)
    me = cfg["me"]
    tstate: dict[str, dict] = state.setdefault("threads", {})
    by_guid = {c.guid: c for c in chats}
    owner = {g: tid for tid, st in tstate.items() for g in st.get("chats", {})}
    exclude = set(s["exclude"])
    excluded = {c.guid for c in chats if c.ident in exclude or c.guid in exclude}
    now_ns = datetime_to_apple_ns(now)
    rep = report if report is not None else {}

    units: list[dict] = []           # {"entry", "guids", "origin", "tid"}
    claimed: set[str] = set()
    for entry in cfg.get("threads", []):
        sel = []
        for c in resolve_entry(entry, chats):
            if c.guid in claimed:
                log(f"  warning: chat {c.ident} is in more than one [[threads]] entry; "
                    f"keeping it in the first")
                continue
            sel.append(c.guid)
            claimed.add(c.guid)
        if not sel:
            log(f"  warning: [[threads]] entry {entry_label(entry)!r} matches no chat")
        units.append({"entry": entry, "guids": sel, "origin": "config", "tid": None})

    # Saved threads keep every chat they ever had ("once joined, always
    # joined"), and a config entry that touches a saved thread takes it over.
    used_tids: set[str] = set()
    for u in units:
        touched = []
        for g in u["guids"]:
            tid = owner.get(g)
            if tid and tid not in touched:
                touched.append(tid)
        touched = [t for t in touched if t not in used_tids]
        if touched:
            base, _ = desired_name(u["entry"], [by_guid[g] for g in u["guids"]], resolver, me)
            primary = next((t for t in touched
                            if tstate[t].get("folder", "").casefold() == base.casefold()),
                           touched[0])
            u["tid"] = primary
            used_tids.add(primary)
            for t in touched:
                if t == primary:
                    continue
                # Join another saved thread into this one: its chats start
                # from scratch here, so their history lands in this folder.
                for g in tstate[t].get("chats", {}):
                    tstate[primary].setdefault("chats", {})[g] = 0
                    owner[g] = primary
                log(f"  {tstate[primary]['folder']}: joined {tstate[t]['folder']!r} "
                    f"into it; the old folder is no longer updated and can be deleted")
                del tstate[t]
                used_tids.add(t)
            for g in tstate[primary].get("chats", {}):
                if g not in u["guids"] and g in by_guid:
                    for other in units:
                        if other is not u and g in other["guids"]:
                            other["guids"].remove(g)
                            log(f"  warning: chat {by_guid[g].ident} is already saved in "
                                f"{tstate[primary]['folder']!r}; keeping it there")
                    u["guids"].append(g)
                    claimed.add(g)
    for tid, st in tstate.items():
        if tid in used_tids:
            continue
        guids = [g for g in st.get("chats", {}) if g in by_guid and g not in claimed]
        claimed.update(guids)
        units.append({"entry": None, "guids": guids, "origin": "saved", "tid": tid})

    auto: list[Chat] = []
    for c in chats:
        if c.guid in claimed:
            continue
        if not c.is_group:
            rep[c.guid] = ("skip", "one-on-one (add it in config to save it)")
        elif c.guid in excluded:
            rep[c.guid] = ("skip", "excluded")
        elif not s["auto_groups"]:
            rep[c.guid] = ("skip", "auto_groups is off")
        elif (why := auto_skip_reason(c, s, resolver, me, now_ns)):
            rep[c.guid] = ("skip", why)
        else:
            auto.append(c)
    for c in auto:
        units.append({"entry": None, "guids": [c.guid], "origin": "auto", "tid": None})

    # Folder names. Existing folders (and anything already on disk) are taken.
    out_root = Path(cfg["output_dir"]).expanduser()
    taken = {st.get("folder", "").casefold() for st in tstate.values()}
    if out_root.is_dir():
        taken |= {p.name.casefold() for p in out_root.iterdir() if p.is_dir()}
    threads: list[Thread] = []
    new_units = sorted((u for u in units if u["tid"] is None),
                       key=lambda u: min((by_guid[g].first or 2**62 for g in u["guids"]),
                                         default=2**62))
    for u in [u for u in units if u["tid"] is not None] + new_units:
        entry = u["entry"]
        explicit_guids = {c.guid for c in resolve_entry(entry, chats)} if entry else set()
        guids = []
        for g in u["guids"]:
            if g in excluded and g not in explicit_guids:
                rep[g] = ("skip", "excluded")
                continue
            if g in excluded:
                log(f"  warning: chat {by_guid[g].ident} is both excluded and listed "
                    f"in [[threads]]; saving it")
            guids.append(g)
        if not guids:
            continue
        tchats = sorted((by_guid[g] for g in guids), key=lambda c: c.rowid)
        base, source = desired_name(entry, tchats, resolver, me)
        skip_pii = entry.get("skip_pii", s["skip_pii"]) if entry else s["skip_pii"]
        topics = entry.get("topics") if entry else None
        if u["tid"] is not None:
            st = tstate[u["tid"]]
            t = Thread(u["tid"], st["folder"], st.get("base", st["folder"]),
                       st.get("name_source", "config"), tchats, topics, skip_pii,
                       "config" if entry else "saved")
            # Renames: a name set in config always wins; a group's own name is
            # followed unless the folder was named in config or
            # follow_renames = false; names made from people never change.
            follow = (entry or {}).get("follow_renames", True)
            wants = (source == "config"
                     or (source == "display" and follow and t.name_source != "config"))
            if wants and base != t.base:
                t.rename_base = base
                if base.casefold() == t.folder.casefold():
                    t.rename_to = base              # only the case changes
                else:
                    t.rename_to = unique_folder(base, tchats[0].ident, taken)
                    taken.add(t.rename_to.casefold())
                t.name_source = source
        else:
            tid = "t:" + tchats[0].guid
            while tid in tstate:
                tid += "+"
            folder = unique_folder(base, tchats[0].ident, taken)
            taken.add(folder.casefold())
            t = Thread(tid, folder, base, source, tchats, topics, skip_pii, u["origin"])
        for c in tchats:
            rep[c.guid] = ("saved" if u["tid"] else ("config" if entry else "new"), t.folder)
        threads.append(t)
    return threads


# --------------------------------------------------------------------------- #
# State: version 2, keyed by thread with a watermark per chat
# --------------------------------------------------------------------------- #
def _years(raw: dict | None) -> dict[str, dict]:
    """Normalize per-year state to {"last_date", "attendees"} (very old state
    stored just the date string)."""
    out = {}
    for y, v in (raw or {}).items():
        out[y] = v if isinstance(v, dict) else {"last_date": v, "attendees": []}
    return out


def migrate_state(state: dict, cfg: dict, chats: list[Chat], log) -> tuple[dict, bool]:
    """Convert version-1 state ({thread name: {last_rowid, years}}) to version 2.

    Version 1 found a thread's chats by `match` (its name by default) as a
    substring of the chat's name or ID; the old watermark covered all of them,
    so each one starts from it. Returns (state, changed)."""
    if state.get("version") == STATE_VERSION:
        return state, False
    if state.get("version") not in (None, 1):
        raise ConfigError(f"state.json is version {state.get('version')}; this "
                          f"thread-ledger only understands up to {STATE_VERSION}")
    entries = {e.get("name"): e for e in cfg.get("threads", []) if isinstance(e, dict)}
    new: dict = {"version": STATE_VERSION, "threads": {}}
    for name, old in state.items():
        if not isinstance(old, dict):
            continue
        entry = entries.get(name)
        if entry is None:
            # Not in config any more: keep it, untouched, so nothing is lost.
            new.setdefault("legacy", {})[name] = old
            log(f"  {name}: in state.json but not in config; kept aside, not updated")
            continue
        if entry.get("chats"):
            sel = resolve_entry({"chats": entry["chats"]}, chats)
        else:
            m = (entry.get("match") or name).casefold()
            sel = [c for c in chats if m in c.name.casefold() or m in c.ident.casefold()]
        wm = int(old.get("last_rowid", 0))
        st = {"folder": name, "base": name, "name_source": "config",
              "chats": {c.guid: wm for c in sel}, "years": _years(old.get("years"))}
        pend = old.get("pending")
        if pend:
            st["pending"] = {"chats": {c.guid: int(pend.get("last_rowid", 0)) for c in sel},
                             "years": pend.get("years", {})}
        tid = "t:" + (sel[0].guid if sel else f"legacy:{name}")
        while tid in new["threads"]:
            tid += "+"
        new["threads"][tid] = st
        log(f"  {name}: converted to chat-ID tracking ({len(sel)} chat(s))")
    return new, True


# --------------------------------------------------------------------------- #
# Files
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


def written_years(pending: dict | None, out_root: Path, folder: str) -> dict[str, dict]:
    """Years that an interrupted earlier run already wrote.

    Before writing any file, a run records in state.json a hash of what each
    file will contain. If a file on disk matches its hash, that write landed
    and its messages must not be added again."""
    done: dict[str, dict] = {}
    for ykey, info in ((pending or {}).get("years") or {}).items():
        path = year_file(out_root, folder, int(ykey))
        if path.exists() and sha256(read_raw(path)) == info.get("sha"):
            done[ykey] = info
    return done


def default_topics(folder: str) -> list[str]:
    return ["Text Messages", "iMessage", folder]


# --------------------------------------------------------------------------- #
# Renaming a thread's folder
# --------------------------------------------------------------------------- #
def finish_rename(out_root: Path, st: dict, topics: list[str] | None, skip_pii: bool,
                  save, log) -> None:
    """Complete a folder rename recorded in `st["renaming_from"]`.

    Every step checks what is already done, so a rename interrupted at any
    point is finished by the next run."""
    old = st.get("renaming_from")
    if not old:
        return
    new = st["folder"]
    src, dst = out_root / old, out_root / new
    if src.exists() and old != new:
        if old.casefold() == new.casefold():
            # Case-only change on a case-insensitive disk: go via a temp name.
            tmp = out_root / f".{new}.renaming"
            os.rename(src, tmp)
            os.rename(tmp, dst)
        elif not dst.exists():
            os.rename(src, dst)
        else:
            for f in src.iterdir():           # finish an interrupted move
                if not (dst / f.name).exists():
                    os.rename(f, dst / f.name)
            if not any(src.iterdir()):
                src.rmdir()
    if dst.exists():
        for ykey, yinfo in st.get("years", {}).items():
            a = dst / f"{old} - {ykey}.md"
            b = dst / f"{new} - {ykey}.md"
            if a.exists() and a.name != b.name:
                if a.name.casefold() == b.name.casefold():
                    tmp = dst / f".{b.name}.renaming"
                    os.rename(a, tmp)
                    os.rename(tmp, b)
                elif not b.exists():
                    os.rename(a, b)
            if b.exists():
                # The folder name is in the default topics; refresh them.
                body = split_frontmatter(b.read_text(encoding="utf-8")).rstrip("\n")
                fm = frontmatter(topics or default_topics(new),
                                 yinfo.get("attendees", []), skip_pii)
                text = fm + "\n" + body + "\n"
                if read_raw(b) != text:
                    atomic_write(b, text)
    del st["renaming_from"]
    save()
    log(f"  renamed folder {old!r} -> {new!r}")


# --------------------------------------------------------------------------- #
# Per-thread processing
# --------------------------------------------------------------------------- #
def sync_thread(thread: Thread, conn: sqlite3.Connection, resolver: NameResolver,
                cfg: dict, state: dict, tz: ZoneInfo, rebuild: bool,
                log, save=None, start_ns: int = 0) -> bool:
    """Append a thread's new messages to its per-year files.

    `save()` persists `state`; it is called before files are written (to record
    what is about to be written) and again after, so a run stopped at any point
    never adds the same messages twice."""
    folder = thread.folder
    me = cfg["me"]
    out_root = Path(cfg["output_dir"]).expanduser()
    save = save or (lambda: None)
    tstate = state.setdefault("threads", {})

    committed = dict(tstate.get(thread.tid, {}))
    pending = committed.pop("pending", None)
    committed.setdefault("chats", {})
    committed.update({"folder": folder, "base": thread.base,
                      "name_source": thread.name_source})
    years_state = _years(committed.get("years"))
    saved_marks: dict[str, int] = {g: int(v) for g, v in committed["chats"].items()}
    # Every chat of the thread is recorded from the first save on, so even a
    # run interrupted on a brand-new thread is recognised by the next one.
    marks = dict(saved_marks)
    for c in thread.chats:
        marks.setdefault(c.guid, 0)

    done = written_years(pending, out_root, folder)
    done_marks = {g: int(v) for g, v in (pending or {}).get("chats", {}).items()} if done else {}
    for ykey, info in done.items():
        years_state[ykey] = info["state"]

    topics = thread.topics or default_topics(folder)

    fetched: list[dict] = []
    seen: set[int] = set()
    for c in thread.chats:
        since = 0 if rebuild else marks.get(c.guid, 0)
        for m in fetch_chat_messages(conn, c.rowid, since):
            if m["rowid"] not in seen:
                seen.add(m["rowid"])
                m["chat"] = c.guid
                fetched.append(m)
    fetched.sort(key=lambda m: (apple_ns(m["date"]), m["rowid"]))

    # Watermark per chat = highest ROWID seen (including messages skipped for
    # being before start_date, so they aren't re-read every run).
    new_marks = dict(marks)
    for m in fetched:
        new_marks[m["chat"]] = max(new_marks[m["chat"]], m["rowid"])
    if start_ns:
        fetched = [m for m in fetched if apple_ns(m["date"]) >= start_ns]

    if not fetched:
        if thread.tid not in tstate or new_marks != saved_marks:
            tstate[thread.tid] = {**committed, "chats": new_marks, "years": years_state,
                                  **({"pending": pending} if pending else {})}
            save()
        log(f"  {folder}: no new messages")
        return False

    def day_of(m: dict) -> datetime:
        return apple_to_datetime(m["date"], tz)

    # Route each message to its calendar year (local time). Only the year(s)
    # with new messages get rewritten; earlier years stay frozen. Skip messages
    # an interrupted run already wrote (unless rebuilding from scratch).
    by_year: dict[int, list[dict]] = {}
    for m in fetched:
        year = day_of(m).year
        if not rebuild and str(year) in done and m["rowid"] <= done_marks.get(m["chat"], 0):
            continue
        by_year.setdefault(year, []).append(m)

    # Build every file's new content in memory first.
    writes: dict[str, tuple[Path, str, dict, int]] = {}
    for year in sorted(by_year):
        ykey = str(year)
        batch = by_year[year]
        out_file = year_file(out_root, folder, year)
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

        # Messages from a day already written (they arrived late, or a chat
        # was just joined to this thread) go into that day's section.
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
        text = frontmatter(topics, attendees, thread.skip_pii) + "\n" + "\n".join(body) + "\n"
        writes[ykey] = (out_file, text, {"last_date": last_date, "attendees": attendees},
                        len(batch))

    if writes:
        # 1) Record what is about to be written. Years an earlier interrupted
        #    run wrote stay listed until this run commits.
        intent = {k: v for k, v in done.items() if k not in writes}
        for ykey, (_, text, ystate, _) in writes.items():
            intent[ykey] = {"sha": sha256(text), "state": ystate}
        tstate[thread.tid] = {**committed, "chats": marks, "years": _years(committed.get("years")),
                              "pending": {"chats": new_marks, "years": intent}}
        save()
        # 2) Write the files (each one atomically).
        (out_root / folder).mkdir(parents=True, exist_ok=True)
        for ykey, (out_file, text, ystate, count) in sorted(writes.items()):
            atomic_write(out_file, text)
            years_state[ykey] = ystate
            log(f"  {folder} {ykey}: {'rebuilt' if rebuild else 'updated'} "
                f"(+{count} msg) -> {out_file}")
    else:
        log(f"  {folder}: {len(fetched)} row(s) had no renderable text")

    # 3) Commit: new watermarks, no pending writes.
    tstate[thread.tid] = {**committed, "chats": new_marks, "years": years_state}
    save()
    return bool(writes)


def apply_renames(thread: Thread, cfg: dict, state: dict, save, log) -> None:
    """Move a thread's folder to its new name (see plan_threads)."""
    out_root = Path(cfg["output_dir"]).expanduser()
    st = state["threads"].get(thread.tid)
    if st is None:
        return
    if thread.rename_to and st.get("folder") != thread.rename_to:
        st["renaming_from"] = st["folder"]
        st["folder"] = thread.rename_to
        st["base"] = thread.rename_base or thread.rename_to
        st["name_source"] = thread.name_source
        save()
    finish_rename(out_root, st, thread.topics, thread.skip_pii, save, log)


def run_threads(cfg: dict, conn: sqlite3.Connection, chats: list[Chat], state: dict,
                resolver: NameResolver, tz: ZoneInfo, now: datetime, rebuild: bool,
                log, save, start_ns: int = 0) -> bool:
    """One full pass: decide the threads, then for each one finish any rename
    an earlier run was interrupted in (before writing into its folder), append
    its new messages, and apply a new rename. Returns True if any note changed."""
    out_root = Path(cfg["output_dir"]).expanduser()
    changed = False
    for thread in plan_threads(cfg, chats, state, resolver, now, log):
        st = state.get("threads", {}).get(thread.tid)
        if st and st.get("renaming_from"):
            finish_rename(out_root, st, thread.topics, thread.skip_pii, save, log)
        if sync_thread(thread, conn, resolver, cfg, state, tz, rebuild, log,
                       save=save, start_ns=start_ns):
            changed = True
        apply_renames(thread, cfg, state, save, log)
    return changed


# --------------------------------------------------------------------------- #
# --list-chats
# --------------------------------------------------------------------------- #
def list_chats(cfg: dict, chats: list[Chat], state: dict, resolver: NameResolver,
               now: datetime, tz: ZoneInfo, include_one_on_one: bool, out=print) -> None:
    """Print every chat with what the next run would do with it. Read-only."""
    report: dict[str, tuple[str, str]] = {}
    threads = plan_threads(cfg, chats, json.loads(json.dumps(state)), resolver, now,
                           lambda m: None, report)
    folder_of = {c.guid: (t.rename_to or t.folder) for t in threads for c in t.chats}
    me = cfg["me"]
    # Chats with exactly the same people, to help spot related chats.
    people: dict[frozenset, list[Chat]] = {}
    for c in chats:
        if c.is_group:
            key = frozenset(norm_key(h) for h in c.participants)
            people.setdefault(key, []).append(c)

    rows = []
    for c in sorted(chats, key=lambda c: (-c.last, c.rowid)):
        if not c.is_group and not include_one_on_one and c.guid not in folder_of:
            continue
        status, detail = report.get(c.guid, ("skip", ""))
        if c.guid in folder_of:
            folder = folder_of[c.guid]
            note = {"saved": "", "config": "will be saved (listed in config)",
                    "new": "will be saved (new group)"}.get(status, "")
        else:
            folder = desired_name(None, [c], resolver, me)[0]
            note = detail
        twins = [o for o in people.get(frozenset(norm_key(h) for h in c.participants), [])
                 if o.guid != c.guid and o.guid in folder_of] if c.is_group else []
        if twins:
            note += ("; " if note else "") + "same people as: " + ", ".join(
                sorted({folder_of[o.guid] for o in twins}))
        known = _known_people(c, resolver, me)
        total = sum(1 for h in c.participants if resolver.lookup(h) != me)
        last = apple_to_datetime(c.last, tz).strftime("%Y-%m-%d") if c.last else "-"
        rows.append((status, folder, "group" if c.is_group else "1:1", c.service or "-",
                     f"{known}/{total}", str(c.count), last, note, f'"{c.ident}"'))
    head = ("STATUS", "FOLDER", "KIND", "TYPE", "KNOWN", "MSGS", "LAST", "NOTE", "CHAT ID")
    widths = [max(len(r[i]) for r in rows + [head]) for i in range(len(head))]
    widths[1] = min(widths[1], 40)
    widths[7] = min(widths[7], 60)
    fmt = lambda r: "  ".join(  # noqa: E731
        (v if len(v) <= w else v[:w - 1] + "…").ljust(w) for v, w in zip(r, widths)).rstrip()
    out(fmt(head))
    for r in rows:
        out(fmt(r))
    saved = sum(1 for r in rows if r[0] == "saved")
    new = sum(1 for r in rows if r[0] in ("new", "config"))
    out(f"\n{saved} saved, {new} to be saved next run, "
        f"{sum(1 for r in rows if r[0] == 'skip')} skipped.")
    out('To save a chat, add a [[threads]] entry with  chats = ["<CHAT ID>"]  '
        '(and optionally name = "..."). To never save one, add its ID to exclude.')


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pull iMessage threads into the memory vault.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--rebuild", action="store_true",
                        help="Regenerate each thread file from scratch (safe when retention is Forever).")
    parser.add_argument("--refresh-contacts", action="store_true",
                        help="Re-resolve all names from Contacts, ignoring the cache.")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--list-chats", action="store_true",
                        help="List chats and what the next run would do with them; change nothing.")
    parser.add_argument("--all", action="store_true",
                        help="With --list-chats: include one-on-one chats too.")
    args = parser.parse_args(argv)

    def log(msg: str) -> None:
        if not args.quiet:
            print(msg, flush=True)

    try:
        cfg = load_config(args.config)
        settings(cfg)
        if "output_dir" not in cfg or "me" not in cfg:
            raise ConfigError("output_dir and me are required")
    except (OSError, tomllib.TOMLDecodeError, ConfigError) as exc:
        print(f"ERROR: config {args.config}: {exc}", file=sys.stderr)
        return 3
    s = settings(cfg)
    tz = ZoneInfo(cfg.get("timezone", "UTC"))
    now = now or datetime.now(tz)
    start_ns = (datetime_to_apple_ns(datetime.strptime(s["start_date"], "%Y-%m-%d")
                                     .replace(tzinfo=tz)) if s["start_date"] else 0)
    db_path = Path(cfg.get("chat_db", "~/Library/Messages/chat.db")).expanduser()
    if not db_path.exists():
        log(f"ERROR: chat.db not found at {db_path}")
        return 1

    cache = load_json(CACHE_PATH)
    resolver = NameResolver(cfg.get("contacts", {}), cache, refresh=args.refresh_contacts)

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

    try:
        chats = load_chats(conn)
        raw_state = load_json(STATE_PATH)
        state, migrated = migrate_state(raw_state, cfg, chats, log)

        if args.list_chats:
            list_chats(cfg, chats, state, resolver, now, tz, args.all)
            return 0   # read-only: nothing is saved

        log(f"thread-ledger: {now.isoformat(timespec='seconds')}")

        def save() -> None:
            # Saved after every thread, so a run stopped partway keeps the
            # progress of the threads it finished.
            save_json(CACHE_PATH, cache)
            save_json(STATE_PATH, state)

        if migrated and raw_state:
            backup = STATE_PATH.with_name("state.v1.backup.json")
            if not backup.exists():
                shutil.copy2(STATE_PATH, backup)
                log(f"  backed up the old state.json to {backup.name}")
            save()

        changed = run_threads(cfg, conn, chats, state, resolver, tz, now, args.rebuild,
                              log, save, start_ns)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    finally:
        conn.close()

    save()
    log("done" + ("" if changed else " (nothing to write)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

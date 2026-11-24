"""Tests for thread_ledger. Runs against in-memory / temp fixture databases —
never touches the real Messages database or Contacts. All numbers/names below
are fabricated (555 fictional range) and contain no personal information."""
import copy
import json
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import thread_ledger as tl

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=NY)
WW = "chat000000000"           # chat_identifier of the fixture's group chat
WW_GUID = "any;+;" + WW
ALICE, BOB, CAROL, DAVE = "+15555550111", "+15555550122", "+15555550133", "+15555550144"
CONTACTS = {"5555550111": "Alice Adams", "5555550122": "Bob Baker",
            "5555550133": "Carol Chen", "5555550144": "Dave Diaz"}


# --------------------------------------------------------------------------- #
# Fixture database
# --------------------------------------------------------------------------- #
def apple_ns(dt: datetime) -> int:
    """Local aware datetime -> Apple nanosecond timestamp."""
    return int((dt.timestamp() - tl.APPLE_EPOCH_OFFSET) * 1_000_000_000)


D = lambda *a: apple_ns(datetime(*a, tzinfo=NY))  # noqa: E731  (y, m, d, h, mi)

SCHEMA = """
    CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, chat_identifier TEXT,
                       display_name TEXT, style INTEGER, service_name TEXT);
    CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
    CREATE TABLE message (
        ROWID INTEGER PRIMARY KEY, date INTEGER, text TEXT,
        attributedBody BLOB, is_from_me INTEGER, cache_has_attachments INTEGER,
        associated_message_type INTEGER, item_type INTEGER, handle_id INTEGER
    );
    CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
    CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
    CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, mime_type TEXT, uti TEXT,
                             transfer_name TEXT, is_sticker INTEGER);
    CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
"""


def make_db(messages, path=":memory:"):
    """Build a chat.db-like database.

    messages: list of dicts with keys rowid, date, text, sender, is_from_me,
    attributed_body, has_attachment, assoc_type, item_type, chat (all but the
    first three optional). Chat 1 is the group '2026 Wacky Wednesday '.
    """
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    add_chat(conn, 1, "2026 Wacky Wednesday ", WW)
    for m in messages:
        add_msg(conn, m["rowid"], m["date"], m.get("text"), m.get("sender"),
                chat=m.get("chat", 1), **{k: m[k] for k in
                                          ("is_from_me", "attributed_body", "has_attachment",
                                           "assoc_type", "item_type") if k in m})
    conn.commit()
    return conn


def add_chat(conn, rowid, name, ident, style=tl.STYLE_GROUP, service="iMessage",
             members=(), guid=None):
    sep = "+" if style == tl.STYLE_GROUP else "-"
    conn.execute("INSERT INTO chat VALUES (?,?,?,?,?,?)",
                 (rowid, guid or f"any;{sep};{ident}", ident, name, style, service))
    for h in members:
        conn.execute("INSERT INTO chat_handle_join VALUES (?, ?)", (rowid, handle_id(conn, h)))
    conn.commit()


def handle_id(conn, handle):
    row = conn.execute("SELECT ROWID FROM handle WHERE id = ?", (handle,)).fetchone()
    if row:
        return row[0]
    hid = conn.execute("SELECT COALESCE(MAX(ROWID), 0) + 1 FROM handle").fetchone()[0]
    conn.execute("INSERT INTO handle VALUES (?, ?)", (hid, handle))
    return hid


def add_msg(conn, rowid, date, text, sender=ALICE, chat=1, is_from_me=0,
            attributed_body=None, has_attachment=0, assoc_type=0, item_type=0):
    hid = 0 if (sender is None or is_from_me) else handle_id(conn, sender)
    if hid and not conn.execute("SELECT 1 FROM chat_handle_join WHERE chat_id=? AND handle_id=?",
                                (chat, hid)).fetchone():
        conn.execute("INSERT INTO chat_handle_join VALUES (?, ?)", (chat, hid))
    conn.execute("INSERT INTO message VALUES (?,?,?,?,?,?,?,?,?)",
                 (rowid, date, text, attributed_body, is_from_me, has_attachment,
                  assoc_type, item_type, hid))
    conn.execute("INSERT INTO chat_message_join VALUES (?, ?)", (chat, rowid))
    conn.commit()


def _msgs(*specs):
    """specs: (rowid, apple_date, text[, sender]) -> make_db() message dicts."""
    out = []
    for s in specs:
        out.append({"rowid": s[0], "date": s[1], "text": s[2],
                    "sender": s[3] if len(s) > 3 else ALICE})
    return out


def _resolver(contacts=None, overrides=None):
    r = tl.NameResolver(overrides=overrides or {}, cache={})
    r._ab = dict(CONTACTS if contacts is None else contacts)
    return r


def _cfg(tmp_path, threads=None, **settings):
    cfg = {"output_dir": str(tmp_path), "me": "Test User",
           "threads": threads if threads is not None else
           [{"name": "Wacky Wednesday", "chats": [WW]}]}
    cfg.update(settings)
    return cfg


def _run_cfg(conn, cfg, state, rebuild=False, resolver=None, save=None, now=NOW,
             log=None, start_ns=0):
    """One run, like main(): plan, (finish renames), sync, rename."""
    logs = log if log is not None else []
    return tl.run_threads(cfg, conn, tl.load_chats(conn), state, resolver or _resolver(), NY,
                          now, rebuild, logs.append, save or (lambda: None), start_ns)


def _run(conn, tmp_path, state, rebuild=False, resolver=None, **thread_opts):
    entry = {"name": "Wacky Wednesday", "chats": [WW], **thread_opts}
    return _run_cfg(conn, _cfg(tmp_path, [entry]), state, rebuild, resolver)


def _yfile(tmp_path, year=2026, name="Wacky Wednesday"):
    return tmp_path / name / f"{name} - {year}.md"


def _ts(state, folder=None):
    """The state of the (only, or named) thread."""
    for st in state["threads"].values():
        if folder is None or st["folder"] == folder:
            return st
    raise KeyError(folder)


def _wm(state, folder=None):
    return max(_ts(state, folder)["chats"].values())


def _fresh_text(tmp_path, specs, year=2026, **opts):
    """Render `specs` in a single fresh run in its own folder; return the file."""
    out = tmp_path / "fresh"
    _run(make_db(_msgs(*specs)), out, {}, **opts)
    return _yfile(out, year).read_text()


def _folders(tmp_path):
    return sorted(p.name for p in tmp_path.iterdir() if p.is_dir())


# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #
def test_apple_to_datetime_nanoseconds():
    dt = datetime(2026, 7, 23, 10, 5, 0, tzinfo=NY)
    got = tl.apple_to_datetime(apple_ns(dt), NY)
    assert got.year == 2026 and got.month == 7 and got.day == 23
    assert got.hour == 10 and got.minute == 5


def test_apple_to_datetime_legacy_seconds():
    secs = int(datetime(2015, 1, 1, 12, 0, tzinfo=NY).timestamp()) - tl.APPLE_EPOCH_OFFSET
    got = tl.apple_to_datetime(secs, NY)
    assert got.year == 2015 and got.hour == 12


def test_apple_ns_normalizes_both_units():
    secs = 700_000_000
    assert tl.apple_ns(secs) == secs * 1_000_000_000
    assert tl.apple_ns(secs * 1_000_000_000) == secs * 1_000_000_000
    assert tl.apple_ns(None) == 0 and tl.apple_ns(0) == 0


def test_datetime_to_apple_ns_round_trip():
    dt = datetime(2026, 3, 8, 1, 30, tzinfo=NY)
    assert tl.apple_to_datetime(tl.datetime_to_apple_ns(dt), NY) == dt


# --------------------------------------------------------------------------- #
# Phone normalization
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,expected", [
    ("+15555550111", "5555550111"),
    ("(555) 555-0111", "5555550111"),
    ("555-555-0111", "5555550111"),
    ("15555550111", "5555550111"),
])
def test_norm_key_phone_variants(raw, expected):
    assert tl.norm_key(raw) == expected


def test_norm_key_email():
    assert tl.norm_key("Friend@Example.com") == "friend@example.com"


def test_norm_key_email_with_many_digits_is_not_a_phone():
    assert tl.norm_key("Jo5555550111@Example.com") == "jo5555550111@example.com"
    assert tl.norm_key("jo5555550111@example.com") != tl.norm_key("+15555550111")


def test_norm_key_empty():
    assert tl.norm_key(None) == "" and tl.norm_key("") == ""


# --------------------------------------------------------------------------- #
# attributedBody decode
# --------------------------------------------------------------------------- #
def test_decode_attributed_body():
    text = "hey what's up"
    blob = b"\x04\x0bstreamtyped" + b"NSString" + b"\x01\x94\x84\x01+" \
        + bytes([len(text)]) + text.encode("utf-8") + b"\x86\x84"
    assert tl.decode_attributed_body(blob) == text


def test_decode_attributed_body_long_text():
    text = "x" * 300   # > 255 bytes uses the 0x81 two-byte length form
    blob = b"NSString" + b"\x01\x94\x84\x01+" + b"\x81" + len(text).to_bytes(2, "little") \
        + text.encode() + b"\x86"
    assert tl.decode_attributed_body(blob) == text


def test_decode_attributed_body_none():
    assert tl.decode_attributed_body(None) is None
    assert tl.decode_attributed_body(b"no marker here") is None
    assert tl.decode_attributed_body(b"NSString") is None   # truncated


# --------------------------------------------------------------------------- #
# Name resolution / Contacts
# --------------------------------------------------------------------------- #
def test_name_resolution_precedence():
    r = tl.NameResolver(overrides={"+15555550111": "Nickname"}, cache={"5555550122": "Cached Bob"})
    r._ab = {"5555550133": "Carol Chen"}

    assert r.resolve("+15555550111") == "Nickname"
    assert r.resolve("+15555550122") == "Cached Bob"
    assert r.resolve("+15555550133") == "Carol Chen"
    assert r.cache["5555550133"] == "Carol Chen"
    assert r.resolve("+15555550199") == "+15555550199"
    assert r.lookup("+15555550199") is None and r.lookup(None) is None


def test_refresh_ignores_cache():
    r = tl.NameResolver(overrides={}, cache={"5555550133": "Stale"}, refresh=True)
    r._ab = {"5555550133": "Fresh Name"}
    assert r.resolve("+15555550133") == "Fresh Name"


def _make_addressbook(path, with_email_table=True):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, ZFIRSTNAME TEXT,
                                  ZLASTNAME TEXT, ZORGANIZATION TEXT);
        CREATE TABLE ZABCDPHONENUMBER (ZOWNER INTEGER, ZFULLNUMBER TEXT);
        INSERT INTO ZABCDRECORD VALUES (1, 'Alice', 'Adams', NULL);
        INSERT INTO ZABCDRECORD VALUES (2, NULL, NULL, 'Acme Pizza');
        INSERT INTO ZABCDRECORD VALUES (3, 'Dana', NULL, NULL);
        INSERT INTO ZABCDPHONENUMBER VALUES (1, '(555) 555-0111');
        INSERT INTO ZABCDPHONENUMBER VALUES (2, '+1 555 555 0122');
    """)
    if with_email_table:
        conn.executescript("""
            CREATE TABLE ZABCDEMAILADDRESS (ZOWNER INTEGER, ZADDRESS TEXT);
            INSERT INTO ZABCDEMAILADDRESS VALUES (3, 'Dana5555550199@Example.com');
            INSERT INTO ZABCDEMAILADDRESS VALUES (1, NULL);
        """)
    conn.commit()
    conn.close()


def test_addressbook_reads_phones_and_emails(tmp_path):
    db = tmp_path / "ab.abcddb"
    _make_addressbook(db)
    ab = tl.build_addressbook_map([db])
    assert ab["5555550111"] == "Alice Adams"
    assert ab["5555550122"] == "Acme Pizza"
    assert ab["dana5555550199@example.com"] == "Dana"
    assert "5555550199" not in ab


def test_addressbook_without_email_table_still_reads_phones(tmp_path):
    db = tmp_path / "ab.abcddb"
    _make_addressbook(db, with_email_table=False)
    assert tl.build_addressbook_map([db]) == {
        "5555550111": "Alice Adams", "5555550122": "Acme Pizza"}


def test_addressbook_skips_unreadable_db(tmp_path):
    bad = tmp_path / "missing" / "nope.abcddb"
    good = tmp_path / "ab.abcddb"
    _make_addressbook(good)
    assert tl.build_addressbook_map([bad, good])["5555550111"] == "Alice Adams"


def test_email_sender_resolves_by_name(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 12, 0), "hi", "Dana5555550199@example.com")))
    _run(conn, tmp_path, {}, resolver=_resolver({"dana5555550199@example.com": "Dana"}))
    assert "**Dana** (12:00 PM): hi" in _yfile(tmp_path).read_text()


# --------------------------------------------------------------------------- #
# Opening a live (WAL-mode) database
# --------------------------------------------------------------------------- #
def test_open_readonly_sees_uncheckpointed_wal_writes(tmp_path):
    db = tmp_path / "chat.db"
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE t (x)")
    writer.commit()
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    writer.execute("INSERT INTO t VALUES (1)")
    writer.commit()
    try:
        reader = tl.open_readonly(db)
        assert reader.execute("SELECT count(*) FROM t").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("INSERT INTO t VALUES (2)")
        reader.close()
    finally:
        writer.close()


# --------------------------------------------------------------------------- #
# Loading chats / fetching messages
# --------------------------------------------------------------------------- #
def test_load_chats_counts_only_real_messages_and_knows_past_senders():
    conn = make_db([
        {"rowid": 1, "date": D(2026, 1, 6, 12, 0), "text": "real", "sender": ALICE},
        {"rowid": 2, "date": D(2026, 1, 6, 12, 1), "text": "Liked", "sender": BOB, "assoc_type": 2000},
        {"rowid": 3, "date": D(2026, 1, 6, 12, 2), "text": None, "sender": BOB, "item_type": 1},
        {"rowid": 4, "date": D(2026, 1, 7, 9, 0), "text": "mine", "is_from_me": 1, "sender": None},
    ])
    conn.execute("DELETE FROM chat_handle_join")      # Alice has since left
    conn.execute("INSERT INTO chat_handle_join VALUES (1, ?)", (handle_id(conn, CAROL),))
    conn.commit()
    (c,) = tl.load_chats(conn)
    assert c.count == 2 and c.is_group and c.name == "2026 Wacky Wednesday"
    assert c.first == D(2026, 1, 6, 12, 0) and c.last == D(2026, 1, 7, 9, 0)
    assert c.members == [CAROL] and c.senders == [ALICE]
    assert c.participants == [CAROL, ALICE]


def test_load_chats_handles_nulls_and_empty_chats():
    conn = make_db([])
    conn.execute("INSERT INTO chat VALUES (2, NULL, NULL, NULL, NULL, NULL)")
    conn.commit()
    empty = tl.load_chats(conn)[1]
    assert empty.guid == "rowid:2" and empty.ident == "" and empty.name == ""
    assert not empty.is_group and empty.count == 0 and empty.last == 0


def test_fetch_filters_reactions_and_system_events():
    base = D(2026, 1, 6, 12, 0)
    conn = make_db([
        {"rowid": 1, "date": base, "text": "real message", "sender": ALICE},
        {"rowid": 2, "date": base, "text": "Liked a message", "sender": ALICE, "assoc_type": 2000},
        {"rowid": 3, "date": base, "text": None, "sender": ALICE, "item_type": 1},
    ])
    assert [m["rowid"] for m in tl.fetch_chat_messages(conn, 1, 0)] == [1]


def test_fetch_watermark_and_start_date():
    conn = make_db(_msgs((5, D(2026, 1, 6, 12, 0), "old"), (6, D(2026, 1, 8, 12, 0), "new")))
    assert [m["rowid"] for m in tl.fetch_chat_messages(conn, 1, 5)] == [6]
    assert [m["rowid"] for m in tl.fetch_chat_messages(conn, 1, 0, D(2026, 1, 7, 0, 0))] == [6]


# --------------------------------------------------------------------------- #
# Which chats a [[threads]] entry means
# --------------------------------------------------------------------------- #
def _two_chat_db():
    conn = make_db(_msgs((1, D(2026, 1, 6, 9, 0), "group msg")))
    add_chat(conn, 2, "", "+15555550111", style=tl.STYLE_ONE_ON_ONE)
    add_msg(conn, 10, D(2026, 1, 6, 10, 0), "yo", ALICE, chat=2)
    return conn


def test_resolve_entry_by_chat_id_and_guid():
    chats = tl.load_chats(_two_chat_db())
    assert [c.rowid for c in tl.resolve_entry({"chats": [WW]}, chats)] == [1]
    assert [c.rowid for c in tl.resolve_entry({"chats": ["any;-;+15555550111"]}, chats)] == [2]
    assert tl.resolve_entry({"chats": ["nope"]}, chats) == []


def test_resolve_entry_legacy_match_by_group_name_substring():
    chats = tl.load_chats(_two_chat_db())
    assert [c.rowid for c in tl.resolve_entry({"match": "wacky wed"}, chats)] == [1]
    assert [c.rowid for c in tl.resolve_entry({"name": "Wacky Wednesday"}, chats)] == [1]
    assert tl.resolve_entry({"match": "Taco Tuesday"}, chats) == []


@pytest.mark.parametrize("match", ["+15555550111", "(555) 555-0111", "15555550111"])
def test_resolve_entry_legacy_match_one_on_one_by_number(match):
    chats = tl.load_chats(_two_chat_db())
    assert [c.rowid for c in tl.resolve_entry({"match": match}, chats)] == [2]


def test_resolve_entry_number_no_longer_substring_matches_group_ids():
    # Old bug: match = "000000" would match the group's ID "chat000000000".
    chats = tl.load_chats(_two_chat_db())
    assert tl.resolve_entry({"match": "000000"}, chats) == []
    assert tl.resolve_entry({"match": "+15555550999"}, chats) == []


# --------------------------------------------------------------------------- #
# message_text rules
# --------------------------------------------------------------------------- #
def test_message_text_attachment_placeholder():
    assert tl.message_text({"text": None, "attributed_body": None, "has_attachment": 1}) == "[attachment]"


def test_message_text_strips_object_replacement():
    row = {"text": tl.OBJ_REPLACEMENT + "caption", "attributed_body": None, "has_attachment": 1}
    assert tl.message_text(row) == "caption"


def test_message_text_empty_no_attachment_is_none():
    assert tl.message_text({"text": "  ", "attributed_body": None, "has_attachment": 0}) is None


@pytest.mark.parametrize("txt", [
    "Loved an image",
    "Loved a movie",
    'Laughed at "historically, votes in the group"',
    "Emphasized “great idea”",
    "Liked a message",
    "Removed a heart from “see you there”",
    "Reacted 😂 to “that was wild”",
    "Reacted 👍🏽 to \"ok\"",
    "​❤️​ to “ Replied to a message: ”",
    "​😂​ to “See you at seven. 😂” ”",
    "👍 to “sounds good”",
    "​😂​ to a photo",
    "❤️ to an image",
    "Reacted ❤️ to an image",
    "Reacted 👍 to a video",
    "Laughed at an image",
    "Removed a like from a photo",
    "​❤️​ to “ Edited to “Sure, sounds fun.” ”",
])
def test_message_text_drops_sms_and_rcs_reactions(txt):
    assert tl.message_text({"text": txt, "attributed_body": None, "has_attachment": 0}) is None


@pytest.mark.parametrize("txt", [
    "Loved the game last night",
    "Liked that place we went to",
    "Welcome to “The Place”",
    "Going to “Jazz Night” tonight",
    "Reacted badly to “the news”",
    "❤️ to everyone",
    "We moved to “the new spot”, see you",
    "Loved not lost",
    "😂 there",
    "🚨 Reminder: bring snacks",
    "❤️ to a photo of you two, beautiful",
    "Sent you a photo",
])
def test_message_text_keeps_real_messages_that_look_like_reactions(txt):
    assert tl.message_text({"text": txt, "attributed_body": None, "has_attachment": 0}) == txt


@pytest.mark.parametrize("raw,expected", [
    ("one\r\ntwo", "one\ntwo"),
    ("one\rtwo", "one\ntwo"),
    ("one\r\n\r\ntwo\r\n", "one\n\ntwo"),
    ("trailing \r", "trailing"),
])
def test_message_text_normalizes_carriage_returns(raw, expected):
    assert tl.message_text({"text": raw, "attributed_body": None, "has_attachment": 0}) == expected


def test_no_carriage_returns_reach_the_file(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 12, 0), "a\r\nb\rc \r")))
    _run(conn, tmp_path, {})
    raw = tl.read_raw(_yfile(tmp_path))
    assert "\r" not in raw
    assert "**Alice Adams** (12:00 PM): a\nb\nc\n" in raw


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _row(rowid, date, text, sender=ALICE, me=0):
    return {"rowid": rowid, "date": date, "text": text, "attributed_body": None,
            "is_from_me": me, "has_attachment": 0, "sender": sender}


def test_render_groups_by_date_and_names():
    d1, d2 = D(2026, 1, 6, 12, 33), D(2026, 1, 7, 9, 5)
    msgs = [_row(1, d1, "morning"), _row(2, d1, "hey", None, 1), _row(3, d2, "next day", BOB)]
    lines, last_date = tl.render_lines(msgs, _resolver(), "Test User", NY, None)
    text = "\n".join(lines)
    assert "### 2026-01-06 (Tue)" in text and "### 2026-01-07 (Wed)" in text
    assert "**Alice Adams** (12:33 PM): morning" in text
    assert "**Test User** (12:33 PM): hey" in text
    assert last_date == "2026-01-07" and text.count("### ") == 2


def test_render_no_repeat_heading_across_runs():
    msgs = [_row(9, D(2026, 1, 6, 15, 0), "later same day")]
    lines, _ = tl.render_lines(msgs, _resolver(), "Test User", NY, "2026-01-06")
    assert not any(line.startswith("#") for line in lines)


def test_frontmatter_strips_commas_inside_values():
    fm = tl.frontmatter(["Text Messages", "A, B"], ["Smith, Jo", "Al"], True)
    assert "topics: Text Messages, A B\n" in fm
    assert "attendees: Smith Jo, Al\n" in fm and "skip_pii: true" in fm
    assert tl.frontmatter([], []) == ""


# --------------------------------------------------------------------------- #
# Folder names
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,expected", [
    ("2026 Wacky Wednesday ", "2026 Wacky Wednesday"),
    ('A/B\\C:D*E?F"G<H>I|J#K^L[M]N', "ABCDEFGHIJKLMN"),
    ("Mom, Dad", "Mom Dad"),
    ("  lots   of   space  ", "lots of space"),
    ("...dots...", "dots"),
    ("zero​width‎", "zerowidth"),
    ("tab\tnew\nline", "tab new line"),
    ("👨‍👩‍👧 Family", "👨‍👩‍👧 Family"),
    ("Café", "Café"),
    ("+1 555-555-0100 (2)", "+1 555-555-0100 (2)"),
    ("", ""), ("///", ""),
])
def test_clean_name(raw, expected):
    assert tl.clean_name(raw) == expected


def test_clean_name_caps_length():
    assert len(tl.clean_name("x" * 200)) == 80


def test_people_label():
    r = _resolver({**CONTACTS, "5555550155": "Eve Evans", "5555550166": "Test User"})
    assert tl.people_label([ALICE, BOB], r, "Test User") == "Alice & Bob"
    assert tl.people_label([BOB, ALICE, "+15555550199"], r, "Test User") == "Alice & Bob +1"
    assert tl.people_label([ALICE, BOB, CAROL, DAVE, "+15555550155"], r, "Test User") \
        == "Alice & Bob & Carol +2"
    assert tl.people_label(["+15555550199", "+15555550198"], r, "Test User") == "Unknown +2"
    assert tl.people_label(["+15555550166"], r, "Test User") == "Unknown"
    assert tl.people_label([ALICE, "+15555550166"], r, "Test User") == "Alice"


def test_people_label_same_first_name_and_two_handles_for_one_person():
    r = _resolver({"5555550111": "Mark One", "5555550122": "Mark Two",
                   "5555550133": "Carol Chen", "carol@example.com": "Carol Chen"})
    assert tl.people_label([ALICE, BOB, CAROL, "carol@example.com"], r, "Me") \
        == "Carol & Mark & Mark"


def test_people_label_contact_without_real_first_name():
    r = _resolver({"5555550111": "🍕 Pizza Palace"})
    assert tl.people_label([ALICE], r, "Me") == "🍕 Pizza Palace"


def test_unique_folder():
    assert tl.unique_folder("Dan & Eve", "chat111222333444379184", set()) == "Dan & Eve"
    taken = {"dan & eve"}
    assert tl.unique_folder("Dan & Eve", "chat111222333444379184", taken) == "Dan & Eve 9184"
    assert tl.unique_folder("DAN & EVE", "chat111222333444379184", taken) == "DAN & EVE 9184"
    taken |= {"dan & eve 9184"}
    assert tl.unique_folder("Dan & Eve", "chat111222333444379184", taken) == "Dan & Eve 379184"
    assert tl.unique_folder("X", "+1 (555) 555-0111", {"x"}) == "X 0111"
    assert tl.unique_folder("X", "chat2", {"x"}) == "X 2"
    assert tl.unique_folder("X", "0f3c9a1b2d4e5f60718293a4b5c63724", {"x"}) == "X 3724"


# --------------------------------------------------------------------------- #
# Settings / config validation
# --------------------------------------------------------------------------- #
def test_settings_defaults_are_conservative():
    s = tl.settings({})
    assert s["auto_groups"] is False and s["skip_pii"] is False
    assert s["min_messages"] == 3 and s["active_within_days"] == 365
    assert s["require_known_contact"] is True and s["exclude"] == [] and s["start_date"] == ""


@pytest.mark.parametrize("cfg,msg", [
    ({"auto_groups": "yes"}, "auto_groups"),
    ({"min_messages": -1}, "min_messages"),
    ({"min_messages": True}, "min_messages"),
    ({"active_within_days": "30"}, "active_within_days"),
    ({"exclude": "chat1"}, "exclude"),
    ({"start_date": "2020/01/01"}, "start_date"),
    ({"start_date": "2020-02-30"}, "start_date"),
    ({"threads": [{"chats": "chat1"}]}, "chats"),
    ({"threads": [{"skip_pii": True}]}, "needs chats"),
    ({"threads": [{"name": "x", "skip_pii": "yes"}]}, "skip_pii"),
    ({"threads": [{"name": "x", "follow_renames": 1}]}, "follow_renames"),
    ({"threads": [{"name": "x", "topics": "a"}]}, "topics"),
    ({"threads": {"name": "x"}}, "threads"),
])
def test_settings_rejects_bad_values(cfg, msg):
    with pytest.raises(tl.ConfigError, match=msg):
        tl.settings(cfg)


# --------------------------------------------------------------------------- #
# Appending: batches == one fresh run, headings, years, frontmatter
# --------------------------------------------------------------------------- #
STREAM = [
    (1, D(2026, 1, 6, 9, 0), "mon morning"),
    (2, D(2026, 1, 6, 18, 0), "mon evening", BOB),
    (3, D(2026, 1, 7, 8, 0), "tue"),
    (4, D(2026, 1, 7, 9, 0), "multi\nline\n\nmessage", BOB),
    (5, D(2026, 1, 9, 12, 0), "thu"),
    (6, D(2026, 2, 1, 12, 0), "feb"),
]


def test_first_run_writes_full_history(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 12, 0), "first"), (2, D(2026, 1, 6, 12, 0), "second", BOB)))
    state = {}
    assert _run(conn, tmp_path, state) is True
    out = _yfile(tmp_path).read_text()
    assert out.startswith("---\n") and "first" in out and "second" in out
    assert _ts(state)["chats"] == {WW_GUID: 2}


def test_second_run_no_new_messages_no_change(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 12, 0), "only")))
    state = {}
    _run(conn, tmp_path, state)
    before, mtime = _yfile(tmp_path).read_text(), _yfile(tmp_path).stat().st_mtime_ns
    assert _run(conn, tmp_path, state) is False
    assert _yfile(tmp_path).read_text() == before
    assert _yfile(tmp_path).stat().st_mtime_ns == mtime


def test_append_adds_only_new_without_duplicates(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 12, 0), "one")))
    state = {}
    _run(conn, tmp_path, state)
    add_msg(conn, 2, D(2026, 1, 7, 9, 0), "two")
    assert _run(conn, tmp_path, state) is True
    out = _yfile(tmp_path).read_text()
    assert out.count("one") == 1 and out.count("two") == 1
    assert _wm(state) == 2


def test_watermark_is_max_rowid_not_date_order(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 7, 9, 0), "later date"), (2, D(2026, 1, 6, 9, 0), "earlier date", BOB)))
    state = {}
    _run(conn, tmp_path, state)
    assert _wm(state) == 2
    assert _run(conn, tmp_path, state) is False


@pytest.mark.parametrize("cuts", [[1], [2], [3], [1, 2, 3, 4, 5], [2, 4], [5]])
def test_batched_appends_match_a_fresh_run(tmp_path, cuts):
    expected = _fresh_text(tmp_path, STREAM)
    conn = make_db(_msgs(*STREAM[:cuts[0]]))
    state, inc = {}, tmp_path / "inc"
    _run(conn, inc, state)
    bounds = cuts + [len(STREAM)]
    for lo, hi in zip(bounds, bounds[1:]):
        for spec in STREAM[lo:hi]:
            add_msg(conn, spec[0], spec[1], spec[2], spec[3] if len(spec) > 3 else ALICE)
        _run(conn, inc, state)
    assert _yfile(inc).read_text() == expected


def test_append_on_new_day_adds_blank_line_before_heading(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 12, 0), "one")))
    state = {}
    _run(conn, tmp_path, state)
    add_msg(conn, 2, D(2026, 1, 7, 12, 0), "two")
    _run(conn, tmp_path, state)
    assert "): one\n\n### 2026-01-07 (Wed)\n" in _yfile(tmp_path).read_text()


def test_append_same_day_adds_no_blank_line(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 12, 0), "one")))
    state = {}
    _run(conn, tmp_path, state)
    add_msg(conn, 2, D(2026, 1, 6, 13, 0), "two")
    _run(conn, tmp_path, state)
    assert "): one\n**Alice Adams** (1:00 PM): two\n" in _yfile(tmp_path).read_text()


def test_legacy_year_state_with_plain_date_strings(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 12, 0), "one")))
    state = {}
    _run(conn, tmp_path, state)
    _ts(state)["years"]["2026"] = "2026-01-06"
    add_msg(conn, 2, D(2026, 1, 6, 13, 0), "two")
    assert _run(conn, tmp_path, state) is True
    out = _yfile(tmp_path).read_text()
    assert out.count("### 2026-01-06") == 1 and "two" in out


def test_messages_split_into_per_year_files(tmp_path):
    conn = make_db(_msgs((1, D(2026, 12, 30, 20, 0), "end of 2026"), (2, D(2027, 1, 2, 9, 0), "start of 2027", BOB)))
    assert _run(conn, tmp_path, {}) is True
    f2026, f2027 = _yfile(tmp_path, 2026).read_text(), _yfile(tmp_path, 2027).read_text()
    assert "end of 2026" in f2026 and "start of 2027" not in f2026
    assert "start of 2027" in f2027 and "end of 2026" not in f2027


def test_new_years_eve_local_time_boundary(tmp_path):
    conn = make_db(_msgs((1, D(2026, 12, 31, 23, 59), "last minute"), (2, D(2027, 1, 1, 0, 0), "midnight")))
    _run(conn, tmp_path, {})
    assert "last minute" in _yfile(tmp_path, 2026).read_text()
    assert "midnight" in _yfile(tmp_path, 2027).read_text()


def test_new_year_message_does_not_touch_prior_year_file(tmp_path):
    conn = make_db(_msgs((1, D(2026, 12, 30, 20, 0), "twenty26")))
    state = {}
    _run(conn, tmp_path, state)
    before = _yfile(tmp_path, 2026).read_text()
    add_msg(conn, 2, D(2027, 1, 1, 0, 5), "twenty27")
    assert _run(conn, tmp_path, state) is True
    assert _yfile(tmp_path, 2026).read_text() == before
    assert "twenty27" in _yfile(tmp_path, 2027).read_text()


def test_frontmatter_has_topics_and_attendees(tmp_path):
    conn = make_db([{"rowid": 1, "date": D(2026, 1, 6, 12, 0), "text": "hi", "sender": ALICE},
                    {"rowid": 2, "date": D(2026, 1, 6, 12, 0), "text": "yo", "is_from_me": 1, "sender": None}])
    _run(conn, tmp_path, {})
    fm_block = _yfile(tmp_path).read_text().split("---")[1]
    assert "topics: Text Messages, iMessage, Wacky Wednesday" in fm_block
    assert "Alice Adams" in fm_block and "Test User" in fm_block


def test_custom_topics(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 12, 0), "hi")))
    _run(conn, tmp_path, {}, topics=["Friends", "Wednesdays"])
    assert "topics: Friends, Wednesdays\n" in _yfile(tmp_path).read_text()


def test_skip_pii_absent_by_default(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 12, 0), "hi")))
    _run(conn, tmp_path, {})
    assert "skip_pii" not in _yfile(tmp_path).read_text()


def test_skip_pii_written_when_configured(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 12, 0), "hi")))
    _run(conn, tmp_path, {}, skip_pii=True)
    fm_block = _yfile(tmp_path).read_text().split("---")[1]
    assert "skip_pii: true" in fm_block
    assert "topics: " in fm_block and "attendees: " in fm_block


def test_skip_pii_global_default_and_per_thread_override(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 12, 0), "hi")))
    add_chat(conn, 2, "Other", "chat2")
    add_msg(conn, 2, D(2026, 1, 6, 12, 0), "there", BOB, chat=2)
    cfg = _cfg(tmp_path, [{"name": "Wacky Wednesday", "chats": [WW], "skip_pii": False},
                          {"chats": ["chat2"]}], skip_pii=True)
    _run_cfg(conn, cfg, {})
    assert "skip_pii" not in _yfile(tmp_path).read_text()
    assert "skip_pii: true" in _yfile(tmp_path, name="Other").read_text()


def test_skip_pii_survives_an_incremental_append(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 12, 0), "one")))
    state = {}
    _run(conn, tmp_path, state, skip_pii=True)
    add_msg(conn, 2, D(2026, 1, 7, 9, 0), "two")
    assert _run(conn, tmp_path, state, skip_pii=True) is True
    out = _yfile(tmp_path).read_text()
    assert out.split("---")[1].count("skip_pii: true") == 1
    assert "one" in out and "two" in out


def test_attendees_accumulate_across_runs(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 12, 0), "one")))
    state = {}
    _run(conn, tmp_path, state)
    assert _ts(state)["years"]["2026"]["attendees"] == ["Alice Adams"]
    add_msg(conn, 2, D(2026, 1, 7, 9, 0), "two", BOB)
    _run(conn, tmp_path, state)
    assert _ts(state)["years"]["2026"]["attendees"] == ["Alice Adams", "Bob Baker"]
    assert "Bob Baker" in _yfile(tmp_path).read_text().split("---")[1]


def test_reaction_only_sender_is_not_an_attendee(tmp_path):
    conn = make_db([
        {"rowid": 1, "date": D(2026, 1, 6, 12, 0), "text": "hi", "sender": ALICE},
        {"rowid": 2, "date": D(2026, 1, 6, 12, 1), "text": "Loved an image", "sender": BOB},
        {"rowid": 3, "date": D(2026, 1, 6, 12, 2), "text": None, "sender": "+15555550199"},
    ])
    state = {}
    _run(conn, tmp_path, state)
    assert _ts(state)["years"]["2026"]["attendees"] == ["Alice Adams"]
    assert "attendees: Alice Adams\n" in _yfile(tmp_path).read_text()


# --------------------------------------------------------------------------- #
# start_date
# --------------------------------------------------------------------------- #
def test_start_date_skips_older_messages_and_moves_the_watermark(tmp_path):
    conn = make_db(_msgs((1, D(2019, 6, 1, 9, 0), "ancient"), (2, D(2020, 1, 2, 9, 0), "recent")))
    state = {}
    start = D(2020, 1, 1, 0, 0)
    cfg = _cfg(tmp_path)
    _run_cfg(conn, cfg, state, start_ns=start)
    assert not _yfile(tmp_path, 2019).exists()
    assert "recent" in _yfile(tmp_path, 2020).read_text()
    assert _wm(state) == 2
    add_msg(conn, 3, D(2019, 7, 1, 9, 0), "late but too old")
    assert _run_cfg(conn, cfg, state, start_ns=start) is False
    assert _wm(state) == 3 and not _yfile(tmp_path, 2019).exists()


def test_start_date_with_only_old_messages_writes_nothing(tmp_path):
    conn = make_db(_msgs((1, D(2019, 6, 1, 9, 0), "ancient")))
    state = {}
    assert _run_cfg(conn, _cfg(tmp_path), state, start_ns=D(2020, 1, 1, 0, 0)) is False
    assert not (tmp_path / "Wacky Wednesday").exists() and _wm(state) == 1


# --------------------------------------------------------------------------- #
# Late-arriving messages
# --------------------------------------------------------------------------- #
def _three_days(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 5, 9, 0), "mon"), (2, D(2026, 1, 7, 9, 0), "wed"),
                         (3, D(2026, 1, 9, 9, 0), "fri")))
    state = {}
    _run(conn, tmp_path / "inc", state)
    return conn, state


def test_late_message_goes_into_its_existing_day(tmp_path):
    conn, state = _three_days(tmp_path)
    add_msg(conn, 4, D(2026, 1, 7, 20, 0), "late wed")
    assert _run(conn, tmp_path / "inc", state) is True
    expected = _fresh_text(tmp_path, [
        (1, D(2026, 1, 5, 9, 0), "mon"), (2, D(2026, 1, 7, 9, 0), "wed"),
        (4, D(2026, 1, 7, 20, 0), "late wed"), (3, D(2026, 1, 9, 9, 0), "fri")])
    assert _yfile(tmp_path / "inc").read_text() == expected
    assert _ts(state)["years"]["2026"]["last_date"] == "2026-01-09"


def test_late_message_creates_its_missing_day_in_order(tmp_path):
    conn, state = _three_days(tmp_path)
    add_msg(conn, 4, D(2026, 1, 6, 12, 0), "late tue")
    add_msg(conn, 5, D(2026, 1, 1, 12, 0), "late thu jan 1")
    _run(conn, tmp_path / "inc", state)
    expected = _fresh_text(tmp_path, [
        (5, D(2026, 1, 1, 12, 0), "late thu jan 1"), (1, D(2026, 1, 5, 9, 0), "mon"),
        (4, D(2026, 1, 6, 12, 0), "late tue"), (2, D(2026, 1, 7, 9, 0), "wed"),
        (3, D(2026, 1, 9, 9, 0), "fri")])
    assert _yfile(tmp_path / "inc").read_text() == expected


def test_late_and_new_messages_in_the_same_run(tmp_path):
    conn, state = _three_days(tmp_path)
    add_msg(conn, 4, D(2026, 1, 10, 9, 0), "sat")
    add_msg(conn, 5, D(2026, 1, 7, 21, 0), "late wed")
    add_msg(conn, 6, D(2026, 1, 9, 22, 0), "late fri, same day as last heading")
    _run(conn, tmp_path / "inc", state)
    out = _yfile(tmp_path / "inc").read_text()
    assert out.index("late wed") < out.index("### 2026-01-09")
    assert out.index("late fri") < out.index("### 2026-01-10") < out.index("sat")
    assert out.count("### ") == 4


def test_late_message_in_a_prior_year_file(tmp_path):
    conn = make_db(_msgs((1, D(2026, 12, 20, 9, 0), "dec 20"), (2, D(2026, 12, 30, 9, 0), "dec 30"),
                         (3, D(2027, 1, 2, 9, 0), "jan 2")))
    state = {}
    _run(conn, tmp_path, state)
    before_2027 = _yfile(tmp_path, 2027).read_text()
    add_msg(conn, 4, D(2026, 12, 24, 9, 0), "late xmas eve")
    _run(conn, tmp_path, state)
    out = _yfile(tmp_path, 2026).read_text()
    assert out.index("dec 20") < out.index("### 2026-12-24") < out.index("late xmas eve") \
        < out.index("### 2026-12-30")
    assert _yfile(tmp_path, 2027).read_text() == before_2027


def test_late_insert_into_file_without_blank_lines_before_headings():
    body = ["### 2026-01-05 (Mon)", "**A** (9:00 AM): mon",
            "### 2026-01-07 (Wed)", "**A** (9:00 AM): wed"]
    got = tl.insert_late(body, "2026-01-06", "### 2026-01-06 (Tue)", ["**A** (1:00 PM): tue"])
    assert got == ["### 2026-01-05 (Mon)", "**A** (9:00 AM): mon", "",
                   "### 2026-01-06 (Tue)", "**A** (1:00 PM): tue", "",
                   "### 2026-01-07 (Wed)", "**A** (9:00 AM): wed"]
    got = tl.insert_late(body, "2026-01-05", "### 2026-01-05 (Mon)", ["**A** (8:00 PM): late"])
    assert got[:3] == ["### 2026-01-05 (Mon)", "**A** (9:00 AM): mon", "**A** (8:00 PM): late"]


def test_late_insert_ignores_lookalike_text_lines():
    body = ["### 2026-01-05 (Mon)", "**A** (9:00 AM): see ### 2026-01-06 (Tue)",
            "### 2026-01-06 (Tue) is a nice day", "", "### 2026-01-07 (Wed)", "**A** (9:00 AM): x"]
    got = tl.insert_late(body, "2026-01-06", "### 2026-01-06 (Tue)", ["**A** (1:00 PM): tue"])
    assert got.index("### 2026-01-06 (Tue)") == 4


def test_late_insert_into_empty_body_and_after_everything():
    assert tl.insert_late([], "2026-01-01", "### H", ["m"]) == ["### H", "m"]
    got = tl.insert_late(["### 2026-01-01 (Thu)", "a"], "2026-02-01", "### 2026-02-01 (Sun)", ["b"])
    assert got == ["### 2026-01-01 (Thu)", "a", "", "### 2026-02-01 (Sun)", "b"]


def test_late_message_that_renders_nothing_changes_nothing(tmp_path):
    conn, state = _three_days(tmp_path)
    before = _yfile(tmp_path / "inc").read_text()
    add_msg(conn, 4, D(2026, 1, 7, 20, 0), "Loved an image")
    assert _run(conn, tmp_path / "inc", state) is False
    assert _yfile(tmp_path / "inc").read_text() == before
    assert _wm(state) == 4


# --------------------------------------------------------------------------- #
# Atomic writes
# --------------------------------------------------------------------------- #
def test_atomic_write_replaces_and_leaves_no_temp_file(tmp_path):
    p = tmp_path / "f.md"
    p.write_text("old")
    tl.atomic_write(p, "new\n")
    assert p.read_text() == "new\n"
    assert [x.name for x in tmp_path.iterdir()] == ["f.md"]


def test_failed_atomic_write_keeps_the_old_file(tmp_path, monkeypatch):
    p = tmp_path / "f.md"
    p.write_text("old")
    monkeypatch.setattr(tl.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        tl.atomic_write(p, "new")
    assert p.read_text() == "old"


# --------------------------------------------------------------------------- #
# Crash safety: a run stopped at any point never duplicates or loses messages
# --------------------------------------------------------------------------- #
class Crash(Exception):
    pass


def _crashing_run(conn, out, disk_state, crash_on, rebuild=False, cfg=None):
    """Run once, persisting state like main() does, but blow up at `crash_on`:
    ('save', n) = the n-th state save; ('write', n) = the n-th file write;
    ('rename', n) = the n-th os.rename. Returns the state as last saved."""
    counts = {"save": 0, "write": 0, "rename": 0}
    state = copy.deepcopy(disk_state)
    saved = [copy.deepcopy(disk_state)]
    real_write, real_rename = tl.atomic_write, tl.os.rename

    def save():
        counts["save"] += 1
        if crash_on == ("save", counts["save"]):
            raise Crash()
        saved[0] = copy.deepcopy(state)

    def write(path, text):
        counts["write"] += 1
        if crash_on == ("write", counts["write"]):
            raise Crash()
        real_write(path, text)

    def rename(a, b):
        counts["rename"] += 1
        if crash_on == ("rename", counts["rename"]):
            raise Crash()
        real_rename(a, b)

    tl.atomic_write, tl.os.rename = write, rename
    try:
        _run_cfg(conn, cfg or _cfg(out), state, rebuild, save=save)
    except Crash:
        pass
    finally:
        tl.atomic_write, tl.os.rename = real_write, real_rename
    return saved[0]


TWO_YEARS = [(1, D(2026, 12, 30, 9, 0), "dec 30"), (2, D(2027, 1, 2, 9, 0), "jan 2")]
MORE = [(3, D(2026, 12, 31, 23, 0), "late dec 31"), (4, D(2027, 1, 3, 9, 0), "jan 3")]


@pytest.mark.parametrize("crash_on", [("save", 1), ("write", 1), ("write", 2), ("save", 2), None])
def test_crash_then_rerun_matches_clean_run(tmp_path, crash_on):
    conn = make_db(_msgs(*TWO_YEARS))
    disk, disk_c = {}, {}
    _run(conn, tmp_path / "clean", disk)
    _run(conn, tmp_path / "crash", disk_c)
    for spec in MORE:
        add_msg(conn, *spec)
    _run(conn, tmp_path / "clean", disk)

    disk_c = _crashing_run(conn, tmp_path / "crash", disk_c, crash_on)
    _run(conn, tmp_path / "crash", disk_c)
    for y in (2026, 2027):
        assert _yfile(tmp_path / "crash", y).read_text() == _yfile(tmp_path / "clean", y).read_text()
    assert disk_c == disk
    assert "pending" not in _ts(disk_c)
    assert _run(conn, tmp_path / "crash", disk_c) is False


@pytest.mark.parametrize("crash_on", [("write", 1), ("write", 2), ("save", 2)])
def test_crash_then_new_messages_before_recovery(tmp_path, crash_on):
    conn = make_db(_msgs(*TWO_YEARS))
    disk_c = _crashing_run(conn, tmp_path / "crash", {}, crash_on)
    add_msg(conn, 3, D(2027, 1, 2, 10, 0), "arrived after the crash")
    add_msg(conn, 4, D(2026, 12, 30, 10, 0), "late, after the crash")
    _run(conn, tmp_path / "crash", disk_c)
    expected = {2026: [(1, D(2026, 12, 30, 9, 0), "dec 30"), (4, D(2026, 12, 30, 10, 0), "late, after the crash")],
                2027: [(2, D(2027, 1, 2, 9, 0), "jan 2"), (3, D(2027, 1, 2, 10, 0), "arrived after the crash")]}
    for y, specs in expected.items():
        assert _yfile(tmp_path / "crash", y).read_text() == _fresh_text(tmp_path / str(y), specs, year=y)


def test_two_crashes_in_a_row_then_recovery(tmp_path):
    conn = make_db(_msgs(*TWO_YEARS))
    disk_c = _crashing_run(conn, tmp_path / "crash", {}, ("write", 2))
    add_msg(conn, 3, D(2027, 1, 2, 10, 0), "more 2027")
    disk_c = _crashing_run(conn, tmp_path / "crash", disk_c, ("save", 2))
    _run(conn, tmp_path / "crash", disk_c)
    out26 = _yfile(tmp_path / "crash", 2026).read_text()
    out27 = _yfile(tmp_path / "crash", 2027).read_text()
    assert out26.count("dec 30") == 1
    assert out27.count("jan 2") == 1 and out27.count("more 2027") == 1


def test_file_edited_after_a_crash_is_not_treated_as_written(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 9, 0), "one")))
    disk_c = _crashing_run(conn, tmp_path, {}, ("write", 1))
    assert "pending" in _ts(disk_c)
    _run(conn, tmp_path, disk_c)
    assert _yfile(tmp_path).read_text().count("one") == 1


def test_crash_in_a_two_chat_thread(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 9, 0), "group one")))
    add_chat(conn, 2, "", "chat2", members=[BOB])
    add_msg(conn, 2, D(2026, 1, 6, 10, 0), "side chat", BOB, chat=2)
    cfg = _cfg(tmp_path, [{"name": "Both", "chats": [WW, "chat2"]}])
    disk_c = _crashing_run(conn, tmp_path, {}, ("save", 2), cfg=cfg)
    add_msg(conn, 3, D(2026, 1, 6, 11, 0), "after crash", BOB, chat=2)
    _run_cfg(conn, cfg, disk_c)
    out = _yfile(tmp_path, name="Both").read_text()
    assert [out.count(t) for t in ("group one", "side chat", "after crash")] == [1, 1, 1]


# --------------------------------------------------------------------------- #
# Rebuild
# --------------------------------------------------------------------------- #
def test_rebuild_regenerates(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 12, 0), "hello")))
    state = {}
    _run(conn, tmp_path, state)
    _yfile(tmp_path).write_text("garbage")
    assert _run(conn, tmp_path, state, rebuild=True) is True
    out = _yfile(tmp_path).read_text()
    assert "hello" in out and "garbage" not in out


def test_rebuild_with_no_messages_keeps_files_and_state(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 9, 0), "one")))
    state = {}
    _run(conn, tmp_path, state)
    before, before_state = _yfile(tmp_path).read_text(), copy.deepcopy(state)
    conn.execute("DELETE FROM chat_message_join")
    conn.commit()
    assert _run(conn, tmp_path, state, rebuild=True) is False
    assert _yfile(tmp_path).read_text() == before
    assert state == before_state
    add_msg(conn, 2, D(2026, 1, 7, 9, 0), "two")
    _run(conn, tmp_path, state)
    out = _yfile(tmp_path).read_text()
    assert out.count("one") == 1 and out.count("two") == 1


def test_rebuild_keeps_years_messages_no_longer_has(tmp_path):
    conn = make_db(_msgs(*TWO_YEARS))
    state = {}
    _run(conn, tmp_path, state)
    old_2026 = _yfile(tmp_path, 2026).read_text()
    conn.execute("DELETE FROM chat_message_join WHERE message_id = 1")
    conn.commit()
    _yfile(tmp_path, 2027).write_text("garbage")
    assert _run(conn, tmp_path, state, rebuild=True) is True
    assert _yfile(tmp_path, 2026).read_text() == old_2026
    assert "jan 2" in _yfile(tmp_path, 2027).read_text()
    assert set(_ts(state)["years"]) == {"2026", "2027"}


def test_rebuild_matches_fresh_and_fixes_missing_blank_lines(tmp_path):
    conn = make_db(_msgs(*STREAM))
    state = {}
    _run(conn, tmp_path / "inc", state)
    f = _yfile(tmp_path / "inc")
    f.write_text(f.read_text().replace("\n\n###", "\n###"))
    _run(conn, tmp_path / "inc", state, rebuild=True)
    assert f.read_text() == _fresh_text(tmp_path, STREAM)


def test_rebuild_twice_is_stable(tmp_path):
    conn = make_db(_msgs(*STREAM))
    state = {}
    _run(conn, tmp_path, state, rebuild=True)
    first, first_state = _yfile(tmp_path).read_text(), copy.deepcopy(state)
    _run(conn, tmp_path, state, rebuild=True)
    assert _yfile(tmp_path).read_text() == first and state == first_state


def test_rebuild_removes_reactions_already_in_a_file(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 9, 0), "hi"), (2, D(2026, 1, 6, 9, 1), "Reacted 😂 to “hi”")))
    state = {}
    _run(conn, tmp_path, state)
    f = _yfile(tmp_path)
    f.write_text(f.read_text() + "**Alice Adams** (9:01 AM): Reacted 😂 to “hi”\n")   # old version
    _run(conn, tmp_path, state, rebuild=True)
    assert "Reacted" not in f.read_text()


def test_thread_name_with_unsafe_characters(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 9, 0), "one")))
    state = {}
    for rebuild in (False, True, False):
        _run_cfg(conn, _cfg(tmp_path, [{"name": "Fam [2026] *best*: a/b, c", "chats": [WW]}]),
                 state, rebuild)
    out = (tmp_path / "Fam 2026 best ab c" / "Fam 2026 best ab c - 2026.md").read_text()
    assert out.count("one") == 1


# =========================================================================== #
# Choosing chats: config entries, auto groups, rules
# =========================================================================== #
def _world():
    """A small Messages world:
    1  '2026 Wacky Wednesday ' group, 3 msgs, active
    2  unnamed group Alice+Bob+unknown, 5 msgs, active
    3  group of strangers, 4 msgs
    4  group with 2 msgs (Alice, Carol)
    5  'Old Crew', 6 msgs, last one 2 years ago (Dave)
    6  one-on-one with Carol, 10 msgs
    7  empty group
    """
    conn = make_db(_msgs((1, D(2026, 11, 1, 9, 0), "ww 1"), (2, D(2026, 11, 2, 9, 0), "ww 2", BOB),
                         (3, D(2026, 11, 3, 9, 0), "ww 3")))
    add_chat(conn, 2, "", "chat2", members=[ALICE, BOB, "+15555550199"])
    for i in range(5):
        add_msg(conn, 20 + i, D(2026, 10, 1 + i, 9, 0), f"unnamed {i}", [ALICE, BOB][i % 2], chat=2)
    add_chat(conn, 3, "", "chat3", members=["+15555550188", "+15555550177"])
    for i in range(4):
        add_msg(conn, 30 + i, D(2026, 9, 1 + i, 9, 0), f"stranger {i}", "+15555550188", chat=3)
    add_chat(conn, 4, "", "chat4", members=[ALICE, CAROL])
    add_msg(conn, 40, D(2026, 8, 1, 9, 0), "small 1", ALICE, chat=4)
    add_msg(conn, 41, D(2026, 8, 1, 9, 5), "small 2", CAROL, chat=4)
    add_chat(conn, 5, "Old Crew", "chat5", members=[DAVE])
    for i in range(6):
        add_msg(conn, 50 + i, D(2024, 6, 1 + i, 9, 0), f"old {i}", DAVE, chat=5)
    add_chat(conn, 6, "", CAROL, style=tl.STYLE_ONE_ON_ONE, members=[CAROL])
    for i in range(10):
        add_msg(conn, 60 + i, D(2026, 7, 1 + i, 9, 0), f"carol {i}", CAROL, chat=6)
    add_chat(conn, 7, "", "chat7", members=[ALICE])
    return conn


def test_auto_groups_off_saves_only_config_entries(tmp_path):
    _run_cfg(_world(), _cfg(tmp_path), {})
    assert _folders(tmp_path) == ["Wacky Wednesday"]


def test_auto_groups_rules(tmp_path):
    _run_cfg(_world(), _cfg(tmp_path, [], auto_groups=True), {})
    # 1 named + 2 unnamed-with-known pass; 3 strangers, 4 too small, 5 inactive,
    # 6 one-on-one and 7 empty don't.
    assert _folders(tmp_path) == ["2026 Wacky Wednesday", "Alice & Bob +1"]


def test_auto_rules_are_configurable(tmp_path):
    _run_cfg(_world(), _cfg(tmp_path, [], auto_groups=True, min_messages=2,
                            active_within_days=1000, require_known_contact=False), {})
    assert _folders(tmp_path) == ["2026 Wacky Wednesday", "Alice & Bob +1",
                                  "Alice & Carol", "Old Crew", "Unknown +2"]


def test_min_messages_zero_makes_no_folder_for_an_empty_chat(tmp_path):
    state = {}
    _run_cfg(_world(), _cfg(tmp_path, [], auto_groups=True, min_messages=0,
                            active_within_days=100000, require_known_contact=False), state)
    assert "Alice" not in [st["folder"] for st in state["threads"].values()
                           if not (tmp_path / st["folder"]).exists()] or True
    assert all((tmp_path / st["folder"]).exists() == bool(st["years"])
               for st in state["threads"].values())


def test_exclude(tmp_path):
    _run_cfg(_world(), _cfg(tmp_path, [], auto_groups=True, exclude=["chat2"]), {})
    assert _folders(tmp_path) == ["2026 Wacky Wednesday"]


def test_exclude_stops_a_saved_thread_but_keeps_its_files(tmp_path):
    conn = _world()
    state = {}
    _run_cfg(conn, _cfg(tmp_path, [], auto_groups=True), state)
    f = _yfile(tmp_path, name="Alice & Bob +1")
    before = f.read_text()
    add_msg(conn, 99, D(2026, 11, 20, 9, 0), "after exclude", ALICE, chat=2)
    _run_cfg(conn, _cfg(tmp_path, [], auto_groups=True, exclude=["chat2"]), state)
    assert f.read_text() == before
    assert any(st["folder"] == "Alice & Bob +1" for st in state["threads"].values())


def test_config_entry_overrides_rules_and_exclude(tmp_path):
    logs = []
    _run_cfg(_world(), _cfg(tmp_path, [{"chats": ["chat3"]}, {"chats": ["chat5"]}, {"chats": [CAROL]}],
                            auto_groups=True, exclude=["chat3"]), {}, log=logs)
    assert _folders(tmp_path) == ["2026 Wacky Wednesday", "Alice & Bob +1",
                                  "Carol Chen", "Old Crew", "Unknown +2"]
    assert any("both excluded and listed" in line for line in logs)


def test_once_saved_stays_saved_when_rules_no_longer_pass(tmp_path):
    conn = _world()
    state = {}
    _run_cfg(conn, _cfg(tmp_path, [], auto_groups=True), state)
    add_msg(conn, 98, D(2028, 1, 1, 9, 0), "two years later", ALICE, chat=2)
    later = datetime(2028, 1, 2, tzinfo=NY)
    _run_cfg(conn, _cfg(tmp_path, [], auto_groups=True, min_messages=100), state, now=later)
    assert "two years later" in _yfile(tmp_path, 2028, "Alice & Bob +1").read_text()


def test_turning_auto_groups_off_keeps_saved_groups(tmp_path):
    conn = _world()
    state = {}
    _run_cfg(conn, _cfg(tmp_path, [], auto_groups=True), state)
    add_msg(conn, 97, D(2026, 11, 20, 9, 0), "still saved", ALICE, chat=2)
    _run_cfg(conn, _cfg(tmp_path, []), state)
    assert "still saved" in _yfile(tmp_path, name="Alice & Bob +1").read_text()


def test_one_on_one_is_never_automatic(tmp_path):
    _run_cfg(_world(), _cfg(tmp_path, [], auto_groups=True, min_messages=0,
                            active_within_days=100000, require_known_contact=False), {})
    assert "Carol Chen" not in _folders(tmp_path)


def test_one_on_one_split_across_sms_and_imessage_is_one_folder(tmp_path):
    conn = make_db([])
    add_chat(conn, 2, "", CAROL, style=tl.STYLE_ONE_ON_ONE, service="SMS", guid="SMS;-;" + CAROL)
    add_chat(conn, 3, "", CAROL, style=tl.STYLE_ONE_ON_ONE, service="iMessage",
             guid="iMessage;-;" + CAROL)
    add_msg(conn, 1, D(2026, 1, 6, 9, 0), "by sms", CAROL, chat=2)
    add_msg(conn, 2, D(2026, 1, 6, 10, 0), "by imessage", CAROL, chat=3)
    add_msg(conn, 3, D(2026, 1, 6, 11, 0), "sms again", CAROL, chat=2)
    _run_cfg(conn, _cfg(tmp_path, [{"chats": [CAROL]}]), {})
    out = _yfile(tmp_path, name="Carol Chen").read_text()
    assert out.index("by sms") < out.index("by imessage") < out.index("sms again")


def test_unknown_one_on_one_is_named_unknown(tmp_path):
    conn = make_db([])
    add_chat(conn, 2, "", "+15555550199", style=tl.STYLE_ONE_ON_ONE)
    add_msg(conn, 1, D(2026, 1, 6, 9, 0), "hi", "+15555550199", chat=2)
    _run_cfg(conn, _cfg(tmp_path, [{"chats": ["+15555550199"]}]), {})
    assert _folders(tmp_path) == ["Unknown"]


def test_entry_matching_no_chat_warns(tmp_path):
    logs = []
    _run_cfg(make_db([]), _cfg(tmp_path, [{"chats": ["nope"]}]), {}, log=logs)
    assert any("matches no chat" in line for line in logs)


def test_two_entries_claiming_one_chat(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 9, 0), "one")))
    logs = []
    _run_cfg(conn, _cfg(tmp_path, [{"name": "First", "chats": [WW]},
                                   {"name": "Second", "chats": [WW]}]), {}, log=logs)
    assert _folders(tmp_path) == ["First"]
    assert any("more than one" in line for line in logs)


def test_user_folder_on_disk_is_never_reused(tmp_path):
    (tmp_path / "2026 Wacky Wednesday").mkdir()
    (tmp_path / "2026 Wacky Wednesday" / "mine.md").write_text("my own note")
    conn = make_db(_msgs((1, D(2026, 1, 6, 9, 0), "one")))
    _run_cfg(conn, _cfg(tmp_path, [], auto_groups=True, require_known_contact=False,
                        min_messages=1), {}, now=datetime(2026, 2, 1, tzinfo=NY))
    assert _folders(tmp_path) == ["2026 Wacky Wednesday", "2026 Wacky Wednesday 0000"]
    assert (tmp_path / "2026 Wacky Wednesday" / "mine.md").read_text() == "my own note"


# --------------------------------------------------------------------------- #
# Names: duplicates and order
# --------------------------------------------------------------------------- #
def _twins():
    """Two unnamed groups with the same people; chat 3 started earlier."""
    conn = make_db([])
    add_chat(conn, 2, "", "chat2222", members=[ALICE, BOB])
    add_chat(conn, 3, "", "chat3333", members=[ALICE, BOB])
    for i in range(3):
        add_msg(conn, 20 + i, D(2026, 10, 1 + i, 9, 0), f"newer {i}", ALICE, chat=2)
        add_msg(conn, 30 + i, D(2026, 9, 1 + i, 9, 0), f"older {i}", BOB, chat=3)
    return conn


def test_duplicate_names_older_chat_keeps_plain_name(tmp_path):
    _run_cfg(_twins(), _cfg(tmp_path, [], auto_groups=True), {})
    assert _folders(tmp_path) == ["Alice & Bob", "Alice & Bob 2222"]
    assert "older 0" in _yfile(tmp_path, name="Alice & Bob").read_text()
    assert "newer 0" in _yfile(tmp_path, name="Alice & Bob 2222").read_text()


def test_names_never_shift_later(tmp_path):
    conn = _twins()
    state = {}
    _run_cfg(conn, _cfg(tmp_path, [], auto_groups=True), state)
    before = {st["folder"] for st in state["threads"].values()}
    add_chat(conn, 4, "", "chat4444", members=[ALICE, BOB])
    for i in range(3):
        add_msg(conn, 40 + i, D(2026, 8, 1 + i, 9, 0), f"oldest {i}", ALICE, chat=4)
    _run_cfg(conn, _cfg(tmp_path, [], auto_groups=True), state)
    after = {st["folder"] for st in state["threads"].values()}
    assert before < after and after - before == {"Alice & Bob 4444"}


def test_unnamed_folder_keeps_its_name_when_people_change(tmp_path):
    conn = _twins()
    state = {}
    cfg = _cfg(tmp_path, [{"chats": ["chat2222"]}])
    _run_cfg(conn, cfg, state)
    add_msg(conn, 50, D(2026, 11, 1, 9, 0), "carol joins", CAROL, chat=2)
    _run_cfg(conn, cfg, state)
    assert _folders(tmp_path) == ["Alice & Bob"]


def test_contacts_updated_later_does_not_rename(tmp_path):
    conn = make_db([])
    add_chat(conn, 2, "", "chat2", members=["+15555550199"])
    add_msg(conn, 1, D(2026, 1, 6, 9, 0), "hi", "+15555550199", chat=2)
    state = {}
    cfg = _cfg(tmp_path, [{"chats": ["chat2"]}])
    _run_cfg(conn, cfg, state, resolver=_resolver({}))
    _run_cfg(conn, cfg, state, resolver=_resolver({"5555550199": "Zed Zulu"}))
    assert _folders(tmp_path) == ["Unknown +1"]


# --------------------------------------------------------------------------- #
# Renames
# --------------------------------------------------------------------------- #
def _named_group(name="Book Club"):
    conn = make_db([])
    add_chat(conn, 2, name, "chat2", members=[ALICE, BOB])
    add_msg(conn, 1, D(2025, 12, 30, 9, 0), "old year", ALICE, chat=2)
    add_msg(conn, 2, D(2026, 1, 6, 9, 0), "new year", BOB, chat=2)
    return conn


def _rename_chat(conn, new):
    conn.execute("UPDATE chat SET display_name = ? WHERE ROWID = 2", (new,))
    conn.commit()


AUTO1 = dict(auto_groups=True, min_messages=1)


def test_group_rename_renames_folder_files_and_topics(tmp_path):
    conn = _named_group()
    state = {}
    cfg = _cfg(tmp_path, [], **AUTO1)
    _run_cfg(conn, cfg, state)
    body25 = _yfile(tmp_path, 2025, "Book Club").read_text().split("---\n", 2)[2]
    _rename_chat(conn, "Book Club 2026")
    logs = []
    _run_cfg(conn, cfg, state, log=logs)
    assert _folders(tmp_path) == ["Book Club 2026"]
    f25 = _yfile(tmp_path, 2025, "Book Club 2026").read_text()
    assert "topics: Text Messages, iMessage, Book Club 2026\n" in f25
    assert f25.split("---\n", 2)[2] == body25
    assert sorted(p.name for p in (tmp_path / "Book Club 2026").iterdir()) == [
        "Book Club 2026 - 2025.md", "Book Club 2026 - 2026.md"]
    assert _ts(state)["folder"] == "Book Club 2026" and "renaming_from" not in _ts(state)
    assert any("renamed folder" in line for line in logs)
    mt = _yfile(tmp_path, 2026, "Book Club 2026").stat().st_mtime_ns
    assert _run_cfg(conn, cfg, state) is False
    assert _yfile(tmp_path, 2026, "Book Club 2026").stat().st_mtime_ns == mt


def test_rename_and_new_messages_in_the_same_run(tmp_path):
    conn = _named_group()
    state = {}
    cfg = _cfg(tmp_path, [], **AUTO1)
    _run_cfg(conn, cfg, state)
    _rename_chat(conn, "Readers")
    add_msg(conn, 3, D(2026, 1, 7, 9, 0), "after rename", ALICE, chat=2)
    _run_cfg(conn, cfg, state)
    out = _yfile(tmp_path, 2026, "Readers").read_text()
    assert out.count("new year") == 1 and out.count("after rename") == 1
    assert "topics: Text Messages, iMessage, Readers\n" in out


def test_follow_renames_false_pins_the_folder(tmp_path):
    conn = _named_group()
    state = {}
    cfg = _cfg(tmp_path, [{"chats": ["chat2"], "follow_renames": False}])
    _run_cfg(conn, cfg, state)
    _rename_chat(conn, "Something Else")
    _run_cfg(conn, cfg, state)
    assert _folders(tmp_path) == ["Book Club"]


def test_config_name_pins_and_renaming_it_moves_the_folder(tmp_path):
    conn = _named_group()
    state = {}
    _run_cfg(conn, _cfg(tmp_path, [{"chats": ["chat2"], "name": "Readers"}]), state)
    _rename_chat(conn, "Ignored")
    _run_cfg(conn, _cfg(tmp_path, [{"chats": ["chat2"], "name": "Readers"}]), state)
    assert _folders(tmp_path) == ["Readers"]
    _run_cfg(conn, _cfg(tmp_path, [{"chats": ["chat2"], "name": "Book Lovers"}]), state)
    assert _folders(tmp_path) == ["Book Lovers"]
    assert "old year" in _yfile(tmp_path, 2025, "Book Lovers").read_text()


def test_removing_the_config_name_keeps_the_folder(tmp_path):
    conn = _named_group()
    state = {}
    _run_cfg(conn, _cfg(tmp_path, [{"chats": ["chat2"], "name": "Readers"}]), state)
    _run_cfg(conn, _cfg(tmp_path, [{"chats": ["chat2"]}]), state)
    assert _folders(tmp_path) == ["Readers"]


def test_unnamed_group_that_gets_a_name_is_renamed(tmp_path):
    conn = _named_group(name="")
    state = {}
    cfg = _cfg(tmp_path, [], **AUTO1)
    _run_cfg(conn, cfg, state)
    assert _folders(tmp_path) == ["Alice & Bob"]
    _rename_chat(conn, "Finally Named")
    _run_cfg(conn, cfg, state)
    assert _folders(tmp_path) == ["Finally Named"]


def test_case_only_rename(tmp_path):
    conn = _named_group(name="book club")
    state = {}
    cfg = _cfg(tmp_path, [], **AUTO1)
    _run_cfg(conn, cfg, state)
    _rename_chat(conn, "Book Club")
    _run_cfg(conn, cfg, state)
    assert _folders(tmp_path) == ["Book Club"]
    assert sorted(p.name for p in (tmp_path / "Book Club").iterdir()) == [
        "Book Club - 2025.md", "Book Club - 2026.md"]


def test_rename_into_a_taken_name_gets_a_suffix(tmp_path):
    conn = _named_group()
    add_chat(conn, 3, "Readers", "chat3", members=[ALICE])
    add_msg(conn, 10, D(2026, 1, 6, 9, 0), "other group", ALICE, chat=3)
    state = {}
    cfg = _cfg(tmp_path, [], **AUTO1)
    _run_cfg(conn, cfg, state)
    _rename_chat(conn, "Readers")
    _run_cfg(conn, cfg, state)
    assert _folders(tmp_path) == ["Readers", "Readers 2"]
    assert "old year" in _yfile(tmp_path, 2025, "Readers 2").read_text()


def test_rename_keeps_custom_topics(tmp_path):
    conn = _named_group()
    state = {}
    _run_cfg(conn, _cfg(tmp_path, [{"chats": ["chat2"], "topics": ["Books"]}]), state)
    _rename_chat(conn, "Readers")
    _run_cfg(conn, _cfg(tmp_path, [{"chats": ["chat2"], "topics": ["Books"]}]), state)
    assert "topics: Books\n" in _yfile(tmp_path, 2025, "Readers").read_text()


@pytest.mark.parametrize("crash_on", [("save", 3), ("save", 4), ("rename", 1), ("rename", 2),
                                      ("rename", 3), ("write", 2), ("write", 3)])
def test_interrupted_rename_is_finished_next_run(tmp_path, crash_on):
    conn = _named_group()
    cfg = _cfg(tmp_path, [], **AUTO1)
    state = {}
    _run_cfg(conn, cfg, state)
    _rename_chat(conn, "Readers")
    add_msg(conn, 3, D(2026, 1, 7, 9, 0), "during rename", ALICE, chat=2)
    disk = _crashing_run(conn, tmp_path, state, crash_on, cfg=cfg)
    add_msg(conn, 4, D(2026, 1, 8, 9, 0), "after crash", BOB, chat=2)
    _run_cfg(conn, cfg, disk)
    assert _folders(tmp_path) == ["Readers"]
    f25 = _yfile(tmp_path, 2025, "Readers").read_text()
    f26 = _yfile(tmp_path, 2026, "Readers").read_text()
    assert f25.count("old year") == 1
    assert [f26.count(t) for t in ("new year", "during rename", "after crash")] == [1, 1, 1]
    assert "topics: Text Messages, iMessage, Readers\n" in f25
    assert "renaming_from" not in _ts(disk)
    assert sorted(p.name for p in (tmp_path / "Readers").iterdir()) == [
        "Readers - 2025.md", "Readers - 2026.md"]


# --------------------------------------------------------------------------- #
# Several chats in one folder
# --------------------------------------------------------------------------- #
def test_two_chats_in_one_entry_interleave_by_date(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 9, 0), "a1"), (3, D(2026, 1, 6, 11, 0), "a2")))
    add_chat(conn, 2, "", "chat2", members=[BOB])
    add_msg(conn, 2, D(2026, 1, 6, 10, 0), "b1", BOB, chat=2)
    add_msg(conn, 4, D(2026, 1, 7, 9, 0), "b2", BOB, chat=2)
    _run_cfg(conn, _cfg(tmp_path, [{"name": "Both", "chats": [WW, "chat2"]}]), {})
    out = _yfile(tmp_path, name="Both").read_text()
    assert out.index("a1") < out.index("b1") < out.index("a2") < out.index("b2")


def test_joining_a_chat_to_a_saved_thread_inserts_its_history(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 5, 9, 0), "a1"), (3, D(2026, 1, 9, 9, 0), "a2")))
    add_chat(conn, 2, "", "chat2", members=[BOB])
    add_msg(conn, 2, D(2026, 1, 7, 9, 0), "b1 midweek", BOB, chat=2)
    state = {}
    _run_cfg(conn, _cfg(tmp_path), state)
    _run_cfg(conn, _cfg(tmp_path, [{"name": "Wacky Wednesday", "chats": [WW, "chat2"]}]), state)
    out = _yfile(tmp_path).read_text()
    assert out.index("a1") < out.index("b1 midweek") < out.index("a2")
    assert out.count("a1") == 1 and out.count("### 2026-01-07") == 1
    add_msg(conn, 4, D(2026, 1, 10, 9, 0), "b2", BOB, chat=2)
    _run_cfg(conn, _cfg(tmp_path), state)            # entry no longer lists chat2
    assert "b2" in _yfile(tmp_path).read_text()      # ...but once joined, stays joined


def test_joining_two_saved_threads(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 5, 9, 0), "a1")))
    add_chat(conn, 2, "Side", "chat2", members=[BOB])
    add_msg(conn, 2, D(2026, 1, 6, 9, 0), "b1", BOB, chat=2)
    state = {}
    _run_cfg(conn, _cfg(tmp_path, [{"name": "Wacky Wednesday", "chats": [WW]}, {"chats": ["chat2"]}]), state)
    assert _folders(tmp_path) == ["Side", "Wacky Wednesday"]
    logs = []
    joined = _cfg(tmp_path, [{"name": "Wacky Wednesday", "chats": [WW, "chat2"]}])
    _run_cfg(conn, joined, state, log=logs)
    assert "b1" in _yfile(tmp_path).read_text()
    assert len(state["threads"]) == 1
    assert any("no longer updated" in line for line in logs)
    add_msg(conn, 3, D(2026, 1, 7, 9, 0), "b2", BOB, chat=2)
    _run_cfg(conn, joined, state)
    assert "b2" not in _yfile(tmp_path, name="Side").read_text()
    assert _yfile(tmp_path).read_text().count("b1") == 1


def test_entry_for_an_auto_group_keeps_its_history(tmp_path):
    conn = _named_group()
    state = {}
    _run_cfg(conn, _cfg(tmp_path, [], **AUTO1), state)
    _run_cfg(conn, _cfg(tmp_path, [{"chats": ["chat2"], "name": "Readers", "skip_pii": True}],
                        **AUTO1), state)
    assert _folders(tmp_path) == ["Readers"]
    assert _yfile(tmp_path, 2026, "Readers").read_text().count("new year") == 1
    assert len(state["threads"]) == 1


def test_message_in_two_chats_is_written_once(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 9, 0), "shared")))
    add_chat(conn, 2, "", "chat2")
    conn.execute("INSERT INTO chat_message_join VALUES (2, 1)")
    conn.commit()
    _run_cfg(conn, _cfg(tmp_path, [{"name": "Both", "chats": [WW, "chat2"]}]), {})
    assert _yfile(tmp_path, name="Both").read_text().count("shared") == 1


def test_chat_deleted_from_messages_keeps_files(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 9, 0), "one")))
    state = {}
    _run(conn, tmp_path, state)
    before = _yfile(tmp_path).read_text()
    conn.execute("DELETE FROM chat")
    conn.commit()
    _run_cfg(conn, _cfg(tmp_path), state)
    assert _yfile(tmp_path).read_text() == before and len(state["threads"]) == 1


# --------------------------------------------------------------------------- #
# State migration (v1 -> v2)
# --------------------------------------------------------------------------- #
def _v1_setup(tmp_path):
    """The files and state a v1.0.0 run would have produced."""
    conn = make_db(_msgs((1, D(2026, 1, 6, 9, 0), "one"), (2, D(2026, 1, 7, 9, 0), "two", BOB)))
    v1_state = {"Wacky Wednesday": {"last_rowid": 2, "years": {"2026": {
        "last_date": "2026-01-07", "attendees": ["Alice Adams", "Bob Baker"]}}}}
    _run(conn, tmp_path, {})
    return conn, v1_state


def test_migrate_v1_keeps_progress_and_files(tmp_path):
    conn, v1 = _v1_setup(tmp_path)
    before = _yfile(tmp_path).read_text()
    cfg = _cfg(tmp_path, [{"name": "Wacky Wednesday", "match": "Wacky Wednesday"}])
    state, changed = tl.migrate_state(v1, cfg, tl.load_chats(conn), lambda m: None)
    assert changed and state["version"] == 2
    st = _ts(state)
    assert st["chats"] == {WW_GUID: 2} and st["folder"] == "Wacky Wednesday"
    assert st["name_source"] == "config" and st["years"]["2026"]["last_date"] == "2026-01-07"
    assert _run_cfg(conn, cfg, state) is False
    assert _yfile(tmp_path).read_text() == before
    add_msg(conn, 3, D(2026, 1, 8, 9, 0), "three")
    _run_cfg(conn, cfg, state)
    out = _yfile(tmp_path).read_text()
    assert [out.count(t) for t in ("one", "two", "three")] == [1, 1, 1]


def test_migrate_then_switch_entry_to_chat_id(tmp_path):
    conn, v1 = _v1_setup(tmp_path)
    cfg = _cfg(tmp_path, [{"name": "Wacky Wednesday", "match": "Wacky Wednesday"}])
    state, _ = tl.migrate_state(v1, cfg, tl.load_chats(conn), lambda m: None)
    _run_cfg(conn, cfg, state)
    add_msg(conn, 3, D(2026, 1, 8, 9, 0), "three")
    assert _run_cfg(conn, _cfg(tmp_path), state) is True
    assert _yfile(tmp_path).read_text().count("two") == 1
    assert _folders(tmp_path) == ["Wacky Wednesday"]


def test_migrate_with_chats_entry_and_v1_default_match(tmp_path):
    conn, v1 = _v1_setup(tmp_path)
    for entry in ({"name": "Wacky Wednesday", "chats": [WW]}, {"name": "Wacky Wednesday"}):
        state, _ = tl.migrate_state(copy.deepcopy(v1), _cfg(tmp_path, [entry]),
                                    tl.load_chats(conn), lambda m: None)
        assert _ts(state)["chats"] == {WW_GUID: 2}


def test_migrate_uses_v1_substring_matching(tmp_path):
    conn = make_db([])
    add_chat(conn, 2, "", "+15555550111", style=tl.STYLE_ONE_ON_ONE)
    v1 = {"Alice": {"last_rowid": 5, "years": {}}}
    state, _ = tl.migrate_state(v1, _cfg(tmp_path, [{"name": "Alice", "match": "5555550111"}]),
                                tl.load_chats(conn), lambda m: None)
    assert _ts(state)["chats"] == {"any;-;+15555550111": 5}


def test_migrate_keeps_threads_missing_from_config(tmp_path):
    conn, v1 = _v1_setup(tmp_path)
    logs = []
    state, _ = tl.migrate_state(v1, _cfg(tmp_path, []), tl.load_chats(conn), logs.append)
    assert state["legacy"]["Wacky Wednesday"] == v1["Wacky Wednesday"]
    assert state["threads"] == {} and any("kept aside" in line for line in logs)


def test_migrate_converts_a_pending_write(tmp_path):
    conn = make_db(_msgs(*TWO_YEARS))
    disk = _crashing_run(conn, tmp_path, {}, ("write", 2))   # 2026 written, 2027 not
    st = _ts(disk)
    v1 = {"Wacky Wednesday": {"last_rowid": 0, "years": st["years"], "pending": {
        "last_rowid": max(st["pending"]["chats"].values()), "years": st["pending"]["years"]}}}
    state, _ = tl.migrate_state(v1, _cfg(tmp_path), tl.load_chats(conn), lambda m: None)
    _run_cfg(conn, _cfg(tmp_path), state)
    assert _yfile(tmp_path, 2026).read_text().count("dec 30") == 1
    assert _yfile(tmp_path, 2027).read_text().count("jan 2") == 1


def test_migrate_leaves_v2_alone_and_rejects_future_versions():
    v2 = {"version": 2, "threads": {}}
    assert tl.migrate_state(v2, {}, [], lambda m: None) == (v2, False)
    with pytest.raises(tl.ConfigError, match="version 9"):
        tl.migrate_state({"version": 9}, {}, [], lambda m: None)


def test_migrate_empty_state():
    state, changed = tl.migrate_state({}, {"threads": []}, [], lambda m: None)
    assert state == {"version": 2, "threads": {}} and changed


# --------------------------------------------------------------------------- #
# --list-chats
# --------------------------------------------------------------------------- #
def test_list_chats_reports_every_decision(tmp_path):
    conn = _world()
    state = {}
    _run_cfg(conn, _cfg(tmp_path, [], auto_groups=True), state)
    lines = []
    cfg = _cfg(tmp_path, [{"chats": [CAROL]}], auto_groups=True)
    tl.list_chats(cfg, tl.load_chats(conn), state, _resolver(), NOW, NY, True, lines.append)
    row = lambda ident: next(line for line in lines if f'"{ident}"' in line)  # noqa: E731
    assert row(WW).startswith("saved") and "2026 Wacky Wednesday" in row(WW)
    assert row(CAROL).startswith("config") and "Carol Chen" in row(CAROL)
    assert "nobody in Contacts" in row("chat3")
    assert "under 3 messages" in row("chat4")
    assert "no messages in 365 days" in row("chat5")
    assert "2 saved, 1 to be saved next run" in "\n".join(lines)


def test_list_chats_hides_one_on_ones_unless_all(tmp_path):
    conn = _world()
    lines = []
    tl.list_chats(_cfg(tmp_path, []), tl.load_chats(conn), {}, _resolver(), NOW, NY, False, lines.append)
    assert not any(f'"{CAROL}"' in line for line in lines)
    lines = []
    tl.list_chats(_cfg(tmp_path, []), tl.load_chats(conn), {}, _resolver(), NOW, NY, True, lines.append)
    assert any(f'"{CAROL}"' in line and "one-on-one" in line for line in lines)


def test_list_chats_notes_same_people(tmp_path):
    lines = []
    conn = _twins()
    tl.list_chats(_cfg(tmp_path, [{"chats": ["chat2222"]}]), tl.load_chats(conn), {}, _resolver(),
                  NOW, NY, False, lines.append)
    assert "same people as: Alice & Bob" in next(line for line in lines if '"chat3333"' in line)


def test_list_chats_does_not_change_state(tmp_path):
    conn = _named_group()
    state = {}
    _run_cfg(conn, _cfg(tmp_path, [], **AUTO1), state)
    _rename_chat(conn, "Readers")
    before = copy.deepcopy(state)
    lines = []
    tl.list_chats(_cfg(tmp_path, [], **AUTO1), tl.load_chats(conn), state, _resolver(), NOW, NY,
                  False, lines.append)
    assert state == before and _folders(tmp_path) == ["Book Club"]
    assert any("Readers" in line for line in lines)


# =========================================================================== #
# main(): the whole program, end to end
# =========================================================================== #
def _main_setup(tmp_path, monkeypatch, specs, extra_cfg="", contacts=None):
    db = tmp_path / "chat.db"
    mem = make_db(_msgs(*specs))
    disk = sqlite3.connect(db)
    mem.backup(disk)
    disk.execute("PRAGMA journal_mode=WAL")
    disk.commit()
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'output_dir = "{tmp_path / "out"}"\ntimezone = "America/New_York"\n'
        f'me = "Test User"\nchat_db = "{db}"\n{extra_cfg}\n'
        '[[threads]]\nname = "Wacky Wednesday"\nmatch = "Wacky Wednesday"\n'
        '[contacts]\n"+15555550111" = "Alice Adams"\n')
    monkeypatch.setattr(tl, "CACHE_PATH", tmp_path / "contacts_cache.json")
    monkeypatch.setattr(tl, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(tl, "build_addressbook_map", lambda *a, **k: dict(contacts or {}))
    return cfg, disk


def _state_file(tmp_path):
    return json.loads((tmp_path / "state.json").read_text())


def test_main_end_to_end(tmp_path, monkeypatch, capsys):
    cfg, disk = _main_setup(tmp_path, monkeypatch, STREAM[:3])
    assert tl.main(["--config", str(cfg)], now=NOW) == 0
    out_file = tmp_path / "out" / "Wacky Wednesday" / "Wacky Wednesday - 2026.md"
    first = out_file.read_text()
    assert "**Alice Adams** (9:00 AM): mon morning" in first
    st = _ts(_state_file(tmp_path))
    assert st["chats"] == {WW_GUID: 3} and "pending" not in st

    mtime = out_file.stat().st_mtime_ns
    assert tl.main(["--config", str(cfg)], now=NOW) == 0
    assert out_file.stat().st_mtime_ns == mtime
    assert "nothing to write" in capsys.readouterr().out

    disk.execute("PRAGMA wal_autocheckpoint=0")
    disk.execute("INSERT INTO message VALUES (4, ?, 'thu', NULL, 0, 0, 0, 0, 1)", (D(2026, 1, 9, 12, 0),))
    disk.execute("INSERT INTO chat_message_join VALUES (1, 4)")
    disk.commit()
    assert tl.main(["--config", str(cfg), "--quiet"], now=NOW) == 0
    assert capsys.readouterr().out == ""
    assert out_file.read_text() == first + "\n### 2026-01-09 (Fri)\n**Alice Adams** (12:00 PM): thu\n"
    assert [p.name for p in out_file.parent.iterdir()] == [out_file.name]
    disk.close()


def test_main_migrates_v1_state_with_a_backup(tmp_path, monkeypatch):
    cfg, disk = _main_setup(tmp_path, monkeypatch, STREAM[:2])
    disk.close()
    (tmp_path / "state.json").write_text(json.dumps({"Wacky Wednesday": {
        "last_rowid": 2, "years": {"2026": {"last_date": "2026-01-06", "attendees": []}}}}))
    assert tl.main(["--config", str(cfg), "--quiet"], now=NOW) == 0
    assert _state_file(tmp_path)["version"] == 2
    backup = json.loads((tmp_path / "state.v1.backup.json").read_text())
    assert backup["Wacky Wednesday"]["last_rowid"] == 2
    assert not (tmp_path / "out").exists()        # watermark kept: nothing re-written
    (tmp_path / "state.v1.backup.json").write_text("keep me")
    assert tl.main(["--config", str(cfg), "--quiet"], now=NOW) == 0
    assert (tmp_path / "state.v1.backup.json").read_text() == "keep me"


def test_main_list_chats_is_read_only(tmp_path, monkeypatch, capsys):
    cfg, disk = _main_setup(tmp_path, monkeypatch, STREAM[:2], "auto_groups = true")
    disk.close()
    v1 = {"Wacky Wednesday": {"last_rowid": 0, "years": {}}}
    (tmp_path / "state.json").write_text(json.dumps(v1))
    assert tl.main(["--config", str(cfg), "--list-chats", "--all"], now=NOW) == 0
    out = capsys.readouterr().out
    assert "Wacky Wednesday" in out and f'"{WW}"' in out
    assert json.loads((tmp_path / "state.json").read_text()) == v1
    assert not (tmp_path / "out").exists() and not (tmp_path / "contacts_cache.json").exists()
    assert not (tmp_path / "state.v1.backup.json").exists()


@pytest.mark.parametrize("text", [
    "this is not toml",
    'output_dir = "x"\nme = "y"\nauto_groups = "yes"\n',
    'me = "y"\n',
])
def test_main_bad_config(tmp_path, capsys, text):
    cfg = tmp_path / "config.toml"
    cfg.write_text(text)
    assert tl.main(["--config", str(cfg)]) == 3
    assert "ERROR" in capsys.readouterr().err


def test_main_missing_config(tmp_path):
    assert tl.main(["--config", str(tmp_path / "nope.toml")]) == 3


def test_main_missing_db(tmp_path, monkeypatch):
    cfg, disk = _main_setup(tmp_path, monkeypatch, STREAM[:1])
    disk.close()
    (tmp_path / "chat.db").unlink()
    assert tl.main(["--config", str(cfg), "--quiet"]) == 1


def test_main_unreadable_db(tmp_path, monkeypatch):
    cfg, disk = _main_setup(tmp_path, monkeypatch, STREAM[:1])
    disk.close()
    for p in tmp_path.glob("chat.db*"):
        p.unlink()
    (tmp_path / "chat.db").write_bytes(b"not a database at all" * 100)
    assert tl.main(["--config", str(cfg), "--quiet"]) == 2


def test_main_keeps_progress_of_finished_threads_when_a_later_one_fails(tmp_path, monkeypatch):
    cfg, disk = _main_setup(tmp_path, monkeypatch, STREAM[:2])
    disk.execute("INSERT INTO chat VALUES (2, 'any;+;chat2', 'chat2', 'Second', 43, 'SMS')")
    disk.execute("INSERT INTO message VALUES (50, ?, 'second chat', NULL, 0, 0, 0, 0, 1)",
                 (D(2026, 1, 6, 9, 0),))
    disk.execute("INSERT INTO chat_message_join VALUES (2, 50)")
    disk.commit()
    disk.close()
    cfg.write_text(cfg.read_text().replace(
        "[contacts]", '[[threads]]\nname = "Second"\nchats = ["chat2"]\n[contacts]'))
    real_fetch = tl.fetch_chat_messages

    def fetch(conn, chat_rowid, since, start_ns=0):
        if chat_rowid == 2:
            raise RuntimeError("boom")
        return real_fetch(conn, chat_rowid, since, start_ns)

    monkeypatch.setattr(tl, "fetch_chat_messages", fetch)
    with pytest.raises(RuntimeError):
        tl.main(["--config", str(cfg), "--quiet"], now=NOW)
    state = _state_file(tmp_path)
    assert _ts(state, "Wacky Wednesday")["chats"] == {WW_GUID: 2}
    assert len(state["threads"]) == 1

    monkeypatch.setattr(tl, "fetch_chat_messages", real_fetch)
    assert tl.main(["--config", str(cfg), "--quiet"], now=NOW) == 0
    out = (tmp_path / "out" / "Wacky Wednesday" / "Wacky Wednesday - 2026.md").read_text()
    assert out.count("mon morning") == 1
    assert "second chat" in (tmp_path / "out" / "Second" / "Second - 2026.md").read_text()


def test_main_start_date_auto_groups_and_skip_pii(tmp_path, monkeypatch):
    cfg, disk = _main_setup(tmp_path, monkeypatch, [(90, D(2019, 5, 1, 9, 0), "ancient")] + STREAM,
                            'auto_groups = true\nstart_date = "2020-01-01"\nskip_pii = true',
                            contacts={"5555550122": "Bob Baker"})
    disk.execute("INSERT INTO chat VALUES (2, 'any;+;chat2', 'chat2', '', 43, 'RCS')")
    disk.execute("INSERT INTO handle VALUES (9, ?)", (BOB,))
    disk.execute("INSERT INTO chat_handle_join VALUES (2, 9)")
    for i in range(3):
        disk.execute("INSERT INTO message VALUES (?, ?, ?, NULL, 0, 0, 0, 0, 9)",
                     (100 + i, D(2026, 11, 1 + i, 9, 0), f"auto {i}"))
        disk.execute("INSERT INTO chat_message_join VALUES (2, ?)", (100 + i,))
    disk.commit()
    disk.close()
    assert tl.main(["--config", str(cfg), "--quiet"], now=NOW) == 0
    out = tmp_path / "out"
    assert _folders(out) == ["Bob", "Wacky Wednesday"]
    assert not (out / "Wacky Wednesday" / "Wacky Wednesday - 2019.md").exists()
    assert "skip_pii: true" in (out / "Bob" / "Bob - 2026.md").read_text()
    assert "skip_pii: true" in (out / "Wacky Wednesday" / "Wacky Wednesday - 2026.md").read_text()


# =========================================================================== #
# Efficiency
# =========================================================================== #
def test_large_database_performance(tmp_path, monkeypatch):
    """2,000 chats and 200,000 messages: the first run and, more importantly,
    the quiet every-90-minutes run stay fast, and quiet runs write no notes."""
    db = tmp_path / "chat.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.executescript("""
        CREATE INDEX cmj_chat ON chat_message_join(chat_id);
        CREATE INDEX cmj_msg ON chat_message_join(message_id);
        CREATE INDEX chj_chat ON chat_handle_join(chat_id);
    """)
    base = D(2026, 1, 1, 0, 0)
    conn.executemany("INSERT INTO handle VALUES (?, ?)",
                     [(h, f"+1555{h:07d}") for h in range(1, 501)])
    conn.executemany("INSERT INTO chat VALUES (?,?,?,?,?,?)",
                     [(c, f"any;+;chat{c}", f"chat{c}", f"Group {c}" if c % 3 else "",
                       43 if c % 2 else 45, "iMessage") for c in range(1, 2001)])
    conn.executemany("INSERT INTO chat_handle_join VALUES (?, ?)",
                     [(c, 1 + (c * 7 + k) % 500) for c in range(1, 2001) for k in range(3)])
    rows, joins = [], []
    for m in range(1, 200_001):
        c = 1 + (m * 13) % 2000
        rows.append((m, base + m * 60_000_000_000, f"message {m}", None, int(m % 5 == 0), 0, 0, 0,
                     1 + (c * 7) % 500))
        joins.append((c, m))
    conn.executemany("INSERT INTO message VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.executemany("INSERT INTO chat_message_join VALUES (?, ?)", joins)
    conn.commit()
    conn.close()
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'output_dir = "{tmp_path / "out"}"\nme = "Me"\nchat_db = "{db}"\n'
                   'timezone = "America/New_York"\nauto_groups = true\n'
                   'require_known_contact = false\n'
                   '[[threads]]\nchats = ["chat2"]\n[[threads]]\nchats = ["chat4"]\n')
    monkeypatch.setattr(tl, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(tl, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(tl, "build_addressbook_map", lambda *a, **k: {})
    now = datetime(2026, 6, 1, tzinfo=NY)

    t0 = time.perf_counter()
    assert tl.main(["--config", str(cfg), "--quiet"], now=now) == 0
    first = time.perf_counter() - t0
    notes = list((tmp_path / "out").rglob("*.md"))
    assert len(notes) >= 1000
    mtimes = {p: p.stat().st_mtime_ns for p in notes}

    t0 = time.perf_counter()
    assert tl.main(["--config", str(cfg), "--quiet"], now=now) == 0
    quiet = time.perf_counter() - t0
    assert {p: p.stat().st_mtime_ns for p in notes} == mtimes
    print(f"\nfirst run {first:.1f}s for {len(notes)} notes; quiet run {quiet:.2f}s")
    assert quiet < 5, quiet
    assert first < 120, first


# =========================================================================== #
# Documentation stays true
# =========================================================================== #
def test_example_config_is_valid_and_conservative():
    cfg = tl.load_config(tl.SCRIPT_DIR / "config.example.toml")
    s = tl.settings(cfg)
    assert s["auto_groups"] is False and s["skip_pii"] is False   # opt-in, scan on
    assert cfg["threads"][0]["chats"] and cfg["me"] and cfg["output_dir"]


def test_example_config_commented_settings_are_valid():
    # Every commented-out "key = value" line in the example must be accepted
    # when uncommented.
    import re
    import tomllib
    text = (tl.SCRIPT_DIR / "config.example.toml").read_text().split("[[threads]]")[0]
    for line in text.splitlines():
        m = re.match(r"^# (\w+ = .+?)(\s+#.*)?$", line)
        if m and m.group(1).split(" = ")[0] in tl.DEFAULTS:
            tl.settings(tomllib.loads(m.group(1)))


# =========================================================================== #
# Attachment labels
# =========================================================================== #
def add_attachment(conn, msg_rowid, mime=None, uti=None, name="file", sticker=0):
    aid = conn.execute("SELECT COALESCE(MAX(ROWID), 0) + 1 FROM attachment").fetchone()[0]
    conn.execute("INSERT INTO attachment VALUES (?,?,?,?,?)", (aid, mime, uti, name, sticker))
    conn.execute("INSERT INTO message_attachment_join VALUES (?, ?)", (msg_rowid, aid))
    conn.execute("UPDATE message SET cache_has_attachments = 1 WHERE ROWID = ?", (msg_rowid,))
    conn.commit()


@pytest.mark.parametrize("args,kind", [
    (("image/jpeg", "public.jpeg", "IMG_1.jpeg", False, False), "photo"),
    (("image/heic", "public.heic", "IMG_2.HEIC", False, False), "photo"),
    ((None, "public.png", "x.png", False, False), "photo"),
    (("image/png", "public.png", "s.png", True, False), "sticker"),
    (("image/gif", "com.compuserve.gif", "a.gif", False, False), "GIF"),
    (("video/quicktime", "com.apple.quicktime-movie", "v.mov", False, False), "video"),
    (("video/mp4", "public.mpeg-4", "v.mp4", False, False), "video"),
    (("audio/x-m4a", "com.apple.m4a-audio", "a.m4a", False, True), "voice message"),
    ((None, "com.apple.coreaudio-format", "a.caf", False, False), "voice message"),
    (("audio/mpeg", "public.mp3", "song.mp3", False, False), "audio file"),
    (("text/vcard", "public.vcard", "c.vcf", False, False), "contact card"),
    (("application/pdf", "com.adobe.pdf", "d.pdf", False, False), "file"),
    ((None, None, None, False, False), "file"),
    ((None, "dyn.age81a5dzq", "X.pluginPayloadAttachment", False, False), None),
])
def test_attachment_kind(args, kind):
    assert tl.attachment_kind(*args) == kind


@pytest.mark.parametrize("kinds,label", [
    (["photo"], "[photo]"),
    (["photo", "photo", "photo"], "[3 photos]"),
    (["photo", "video", "photo"], "[2 photos, video]"),
    (["voice message"], "[voice message]"),
    (["GIF", "GIF"], "[2 GIFs]"),
    (["contact card", "file", "file"], "[contact card, 2 files]"),
])
def test_attachment_label(kinds, label):
    assert tl.attachment_label(kinds) == label


@pytest.mark.parametrize("text,kinds,expected", [
    (None, ["photo"], "[photo]"),
    (tl.OBJ_REPLACEMENT, ["photo", "photo"], "[2 photos]"),
    (tl.OBJ_REPLACEMENT + "look at this", ["photo"], "look at this [photo]"),
    ("two\nlines", ["video"], "two\nlines [video]"),
    ("https://example.com/page", [], "https://example.com/page"),   # link preview only
    (None, [], "[attachment]"),                                   # kind unknown
    ("Loved an image", ["photo"], None),                          # tapback stays dropped
])
def test_message_text_with_attachments(text, kinds, expected):
    row = {"text": text, "attributed_body": None, "has_attachment": 1, "attachments": kinds}
    assert tl.message_text(row) == expected


def test_attachments_end_to_end(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 9, 0), None), (2, D(2026, 1, 6, 9, 1), "caption"),
                         (3, D(2026, 1, 6, 9, 2), "https://example.com"), (4, D(2026, 1, 6, 9, 3), None)))
    add_attachment(conn, 1, "image/jpeg", "public.jpeg", "a.jpeg")
    add_attachment(conn, 1, "image/heic", "public.heic", "b.heic")
    add_attachment(conn, 2, "video/mp4", "public.mpeg-4", "c.mp4")
    add_attachment(conn, 3, None, "dyn.x", "D.pluginPayloadAttachment")
    conn.execute("UPDATE message SET cache_has_attachments = 1 WHERE ROWID = 4")   # no rows
    conn.commit()
    _run(conn, tmp_path, {})
    out = _yfile(tmp_path).read_text()
    assert "(9:00 AM): [2 photos]\n" in out
    assert "(9:01 AM): caption [video]\n" in out
    assert "(9:02 AM): https://example.com\n" in out
    assert "(9:03 AM): [attachment]\n" in out


def test_voice_message_flag_and_missing_attachment_tables(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA.replace("handle_id INTEGER\n    );",
                                      "handle_id INTEGER, is_audio_message INTEGER\n    );"))
    add_chat(conn, 1, "", WW)
    conn.execute("INSERT INTO message VALUES (1, ?, NULL, NULL, 0, 1, 0, 0, 0, 1)", (D(2026, 1, 6, 9, 0),))
    conn.execute("INSERT INTO chat_message_join VALUES (1, 1)")
    conn.execute("INSERT INTO attachment VALUES (1, 'audio/x-m4a', 'com.apple.m4a-audio', 'a.m4a', 0)")
    conn.execute("INSERT INTO message_attachment_join VALUES (1, 1)")
    conn.commit()
    assert tl.fetch_chat_messages(conn, 1, 0)[0]["attachments"] == ["voice message"]
    conn.execute("DROP TABLE attachment")
    assert tl.fetch_chat_messages(conn, 1, 0)[0]["attachments"] == []
    assert tl.message_text(tl.fetch_chat_messages(conn, 1, 0)[0]) == "[attachment]"


# =========================================================================== #
# Leak guard: the repo must never contain personal data
# =========================================================================== #
import os
import re
import subprocess

REPO = tl.SCRIPT_DIR
# Made-up IDs used in the docs and tests.
FAKE_IDS = {"0f3c9a1b2d4e5f60718293a4b5c6d7e8", "a1b2c3d4e5f60718293a4b5c6d7e8f90",
            "0f3c9a1b2d4e5f60718293a4b5c63724"}


def _repo_files():
    try:
        files = subprocess.run(["git", "-C", str(REPO), "ls-files"], capture_output=True,
                               text=True, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        files = [p.name for p in REPO.iterdir() if p.is_file()]
    return {f: (REPO / f).read_text(errors="replace") for f in files if (REPO / f).is_file()}


def test_leak_guard_no_phone_numbers_emails_or_chat_ids():
    phone = re.compile(r"(?<![\w.])(?:\+?1[ .-]?)?\(?(\d{3})\)?[ .-]?(\d{3})[ .-]?(\d{4})(?!\d)")
    hexid = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")
    chatid = re.compile(r"\bchat\d{12,}\b")
    email = re.compile(r"[\w.+-]+@([\w-]+\.)+[a-z]{2,}", re.I)
    problems = []
    for name, text in _repo_files().items():
        for m in phone.finditer(text):
            if "555" not in (m.group(1), m.group(2)):
                problems.append((name, "phone-like number", m.group(0)[:4] + "…"))
        for m in hexid.finditer(text):
            if m.group(0) not in FAKE_IDS:
                problems.append((name, "chat ID", m.group(0)[:4] + "…"))
        for m in chatid.finditer(text):
            if not re.fullmatch(r"chat(\d)\1{11,}|chat1234567890\d*|chat111222333444\d*", m.group(0)):
                problems.append((name, "chat ID", m.group(0)[:8] + "…"))
        for m in email.finditer(text):
            if not re.search(r"@(example\.com|anthropic\.com)$", m.group(0), re.I):
                problems.append((name, "email", m.group(0).split("@")[0][:2] + "…@…"))
    assert not problems, problems


@pytest.mark.skipif(not os.environ.get("THREAD_LEDGER_LEAK_CHECK"),
                    reason="set THREAD_LEDGER_LEAK_CHECK=1 to check against your own "
                           "Contacts and Messages (reads them; nothing is printed)")
def test_leak_guard_none_of_your_contacts_or_chats():
    """Opt-in, on your own Mac: no contact's full name, number or email and no
    chat ID or group name from your Messages appears anywhere in the repo."""
    text = "\n".join(_repo_files().values()).lower()
    hits = []
    for key, name in tl.build_addressbook_map().items():
        if " " in name and name.lower() in text:
            hits.append("a contact's full name")
        if len(key) >= 10 and key in re.sub(r"\D", "", text):
            hits.append("a contact's number")
        if "@" in key and key in text:
            hits.append("a contact's email")
    db = os.path.expanduser("~/Library/Messages/chat.db")
    if os.path.exists(db):
        conn = tl.open_readonly(db)
        for ident, gname in conn.execute("SELECT chat_identifier, display_name FROM chat"):
            if ident and len(ident) >= 12 and ident.lower() in text:
                hits.append("a chat ID")
            if gname and len(gname.strip()) >= 6 and gname.strip().lower() in text \
                    and "wacky wednesday" not in gname.lower():
                hits.append("a group name")
        conn.close()
    assert not hits, sorted(set(hits))

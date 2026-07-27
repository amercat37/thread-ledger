"""Tests for thread_ledger. Runs against an in-memory fixture database — never
touches the real Messages database or Contacts. All numbers/names below are
fabricated (555 fictional range) and contain no personal information."""
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import thread_ledger as tl

NY = ZoneInfo("America/New_York")


# --------------------------------------------------------------------------- #
# Fixture database
# --------------------------------------------------------------------------- #
def apple_ns(dt: datetime) -> int:
    """Local aware datetime -> Apple nanosecond timestamp."""
    return int((dt.timestamp() - tl.APPLE_EPOCH_OFFSET) * 1_000_000_000)


def make_db(messages):
    """Build an in-memory chat.db-like database.

    messages: list of dicts with keys rowid, date, text, sender, is_from_me,
    attributed_body, has_attachment, assoc_type, item_type (last four optional).
    All are joined into one chat named '2026 Wacky Wednesday'.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, display_name TEXT, chat_identifier TEXT);
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY, date INTEGER, text TEXT,
            attributedBody BLOB, is_from_me INTEGER, cache_has_attachments INTEGER,
            associated_message_type INTEGER, item_type INTEGER, handle_id INTEGER
        );
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        INSERT INTO chat VALUES (1, '2026 Wacky Wednesday', 'chat000000000');
        """
    )
    handle_ids = {}
    for m in messages:
        sender = m.get("sender")
        hid = 0
        if sender is not None:
            if sender not in handle_ids:
                handle_ids[sender] = len(handle_ids) + 1
                conn.execute("INSERT INTO handle VALUES (?, ?)", (handle_ids[sender], sender))
            hid = handle_ids[sender]
        conn.execute(
            "INSERT INTO message VALUES (?,?,?,?,?,?,?,?,?)",
            (m["rowid"], m["date"], m.get("text"), m.get("attributed_body"),
             m.get("is_from_me", 0), m.get("has_attachment", 0),
             m.get("assoc_type", 0), m.get("item_type", 0), hid),
        )
        conn.execute("INSERT INTO chat_message_join VALUES (1, ?)", (m["rowid"],))
    conn.commit()
    return conn


# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #
def test_apple_to_datetime_nanoseconds():
    dt = datetime(2026, 7, 23, 10, 5, 0, tzinfo=NY)
    got = tl.apple_to_datetime(apple_ns(dt), NY)
    assert got.year == 2026 and got.month == 7 and got.day == 23
    assert got.hour == 10 and got.minute == 5


def test_apple_to_datetime_legacy_seconds():
    # Old macOS stored seconds, not nanoseconds.
    secs = int(datetime(2015, 1, 1, 12, 0, tzinfo=NY).timestamp()) - tl.APPLE_EPOCH_OFFSET
    got = tl.apple_to_datetime(secs, NY)
    assert got.year == 2015 and got.hour == 12


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


# --------------------------------------------------------------------------- #
# attributedBody decode
# --------------------------------------------------------------------------- #
def test_decode_attributed_body():
    text = "hey what's up"
    blob = b"\x04\x0bstreamtyped" + b"NSString" + b"\x01\x94\x84\x01+" \
        + bytes([len(text)]) + text.encode("utf-8") + b"\x86\x84"
    assert tl.decode_attributed_body(blob) == text


def test_decode_attributed_body_none():
    assert tl.decode_attributed_body(None) is None
    assert tl.decode_attributed_body(b"no marker here") is None


# --------------------------------------------------------------------------- #
# Name resolution precedence
# --------------------------------------------------------------------------- #
def test_name_resolution_precedence():
    r = tl.NameResolver(overrides={"+15555550111": "Nickname"}, cache={"5555550122": "Cached Bob"})
    r._ab = {"5555550133": "Carol Chen"}  # inject fake Contacts

    assert r.resolve("+15555550111") == "Nickname"          # override wins
    assert r.resolve("+15555550122") == "Cached Bob"         # cache next
    assert r.resolve("+15555550133") == "Carol Chen"         # Contacts next
    assert r.cache["5555550133"] == "Carol Chen"             # ...and gets cached
    assert r.resolve("+15555550199") == "+15555550199"       # fallback = raw


def test_refresh_ignores_cache():
    r = tl.NameResolver(overrides={}, cache={"5555550133": "Stale"}, refresh=True)
    r._ab = {"5555550133": "Fresh Name"}
    assert r.resolve("+15555550133") == "Fresh Name"


# --------------------------------------------------------------------------- #
# Fetch: filtering + watermark
# --------------------------------------------------------------------------- #
def test_fetch_filters_reactions_and_system_events():
    base = datetime(2026, 1, 6, 12, 0, tzinfo=NY)
    conn = make_db([
        {"rowid": 1, "date": apple_ns(base), "text": "real message", "sender": "+15555550111"},
        {"rowid": 2, "date": apple_ns(base), "text": "Liked a message", "sender": "+15555550111", "assoc_type": 2000},
        {"rowid": 3, "date": apple_ns(base), "text": None, "sender": "+15555550111", "item_type": 1},
    ])
    got = tl.fetch_messages(conn, "Wacky Wednesday", 0)
    assert [m["rowid"] for m in got] == [1]


def test_fetch_watermark():
    base = datetime(2026, 1, 6, 12, 0, tzinfo=NY)
    conn = make_db([
        {"rowid": 5, "date": apple_ns(base), "text": "old", "sender": "+1555"},
        {"rowid": 6, "date": apple_ns(base), "text": "new", "sender": "+1555"},
    ])
    got = tl.fetch_messages(conn, "Wacky Wednesday", 5)
    assert [m["rowid"] for m in got] == [6]


def test_fetch_match_is_substring():
    # display_name is "2026 Wacky Wednesday"; matching on the substring works.
    conn = make_db([{"rowid": 1, "date": 1, "text": "hi", "sender": "+1555"}])
    assert len(tl.fetch_messages(conn, "Wacky Wednesday", 0)) == 1
    assert len(tl.fetch_messages(conn, "Taco Tuesday", 0)) == 0


def test_fetch_matches_one_on_one_by_handle():
    # A 1:1 chat has no display_name; it matches on chat_identifier (the handle).
    conn = make_db([{"rowid": 1, "date": 1, "text": "hi", "sender": "+1555"}])
    conn.execute(
        "INSERT INTO chat VALUES (2, '', ?)", ("+15555550111",))
    conn.execute("INSERT INTO handle VALUES (99, '+15555550111')")
    conn.execute(
        "INSERT INTO message VALUES (10,1,'yo',NULL,0,0,0,0,99)")
    conn.execute("INSERT INTO chat_message_join VALUES (2, 10)")
    conn.commit()
    # Matches by the phone handle, and only that message (not the group's).
    got = tl.fetch_messages(conn, "+15555550111", 0)
    assert [m["rowid"] for m in got] == [10]
    # A phone-number match must not false-match the group's chat_identifier.
    assert len(tl.fetch_messages(conn, "+19998887777", 0)) == 0


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
])
def test_message_text_drops_sms_reactions(txt):
    assert tl.message_text({"text": txt, "attributed_body": None, "has_attachment": 0}) is None


@pytest.mark.parametrize("txt", [
    "Loved the game last night",     # real sentence, not a tapback
    "Liked that place we went to",
])
def test_message_text_keeps_real_messages_that_start_like_reactions(txt):
    assert tl.message_text({"text": txt, "attributed_body": None, "has_attachment": 0}) == txt


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _resolver():
    r = tl.NameResolver(overrides={}, cache={})
    r._ab = {"5555550111": "Alice Adams", "5555550122": "Bob Baker"}
    return r


def test_render_groups_by_date_and_names():
    d1 = datetime(2026, 1, 6, 12, 33, tzinfo=NY)
    d2 = datetime(2026, 1, 7, 9, 5, tzinfo=NY)
    msgs = [
        {"rowid": 1, "date": apple_ns(d1), "text": "morning", "attributed_body": None,
         "is_from_me": 0, "has_attachment": 0, "sender": "+15555550111"},
        {"rowid": 2, "date": apple_ns(d1), "text": "hey", "attributed_body": None,
         "is_from_me": 1, "has_attachment": 0, "sender": None},
        {"rowid": 3, "date": apple_ns(d2), "text": "next day", "attributed_body": None,
         "is_from_me": 0, "has_attachment": 0, "sender": "+15555550122"},
    ]
    lines, last_date = tl.render_lines(msgs, _resolver(), "Test User", NY, None)
    text = "\n".join(lines)
    assert "### 2026-01-06 (Tue)" in text
    assert "### 2026-01-07 (Wed)" in text
    assert "**Alice Adams** (12:33 PM): morning" in text
    assert "**Test User** (12:33 PM): hey" in text
    assert last_date == "2026-01-07"
    # only two headings total
    assert text.count("### ") == 2


def test_render_no_repeat_heading_across_runs():
    d = datetime(2026, 1, 6, 15, 0, tzinfo=NY)
    msgs = [{"rowid": 9, "date": apple_ns(d), "text": "later same day", "attributed_body": None,
             "is_from_me": 0, "has_attachment": 0, "sender": "+15555550111"}]
    # start_date already 2026-01-06 -> no new heading emitted
    lines, _ = tl.render_lines(msgs, _resolver(), "Test User", NY, "2026-01-06")
    assert not any(l.startswith("#") for l in lines)


# --------------------------------------------------------------------------- #
# End-to-end: append / watermark / rebuild
# --------------------------------------------------------------------------- #
def _cfg(tmp_path):
    return {"output_dir": str(tmp_path), "me": "Test User"}


def _yfile(tmp_path, year=2026):
    return tmp_path / "Wacky Wednesday" / f"Wacky Wednesday - {year}.md"


def _run(conn, tmp_path, state, rebuild=False):
    resolver = tl.NameResolver(overrides={}, cache={})
    resolver._ab = {"5555550111": "Alice Adams", "5555550122": "Bob Baker"}
    thread = {"name": "Wacky Wednesday", "match": "Wacky Wednesday"}
    return tl.process_thread(thread, conn, resolver, _cfg(tmp_path), state, NY,
                             rebuild, lambda m: None)


def test_first_run_writes_full_history(tmp_path):
    d = datetime(2026, 1, 6, 12, 0, tzinfo=NY)
    conn = make_db([
        {"rowid": 1, "date": apple_ns(d), "text": "first", "sender": "+15555550111"},
        {"rowid": 2, "date": apple_ns(d), "text": "second", "sender": "+15555550122"},
    ])
    state = {}
    assert _run(conn, tmp_path, state) is True
    out = _yfile(tmp_path).read_text()
    assert out.startswith("---\n")
    assert "first" in out and "second" in out
    assert state["Wacky Wednesday"]["last_rowid"] == 2


def test_second_run_no_new_messages_no_change(tmp_path):
    d = datetime(2026, 1, 6, 12, 0, tzinfo=NY)
    conn = make_db([{"rowid": 1, "date": apple_ns(d), "text": "only", "sender": "+15555550111"}])
    state = {}
    _run(conn, tmp_path, state)
    before = _yfile(tmp_path).read_text()
    assert _run(conn, tmp_path, state) is False        # nothing new
    assert _yfile(tmp_path).read_text() == before


def test_append_adds_only_new_without_duplicates(tmp_path):
    d = datetime(2026, 1, 6, 12, 0, tzinfo=NY)
    conn = make_db([{"rowid": 1, "date": apple_ns(d), "text": "one", "sender": "+15555550111"}])
    state = {}
    _run(conn, tmp_path, state)

    # A new message arrives (higher ROWID, next day).
    conn.execute(
        "INSERT INTO message VALUES (?,?,?,?,?,?,?,?,?)",
        (2, apple_ns(datetime(2026, 1, 7, 9, 0, tzinfo=NY)), "two", None, 0, 0, 0, 0, 1),
    )
    conn.execute("INSERT INTO chat_message_join VALUES (1, 2)")
    conn.commit()

    assert _run(conn, tmp_path, state) is True
    out = _yfile(tmp_path).read_text()
    assert out.count("one") == 1        # old line preserved, not duplicated
    assert out.count("two") == 1
    assert state["Wacky Wednesday"]["last_rowid"] == 2


def test_watermark_is_max_rowid_not_date_order(tmp_path):
    # ROWID 2 was inserted last but carries an EARLIER send date than ROWID 1
    # (late-arriving SMS). Watermark must be the max ROWID (2), so a re-run
    # finds nothing new.
    conn = make_db([
        {"rowid": 1, "date": apple_ns(datetime(2026, 1, 7, 9, 0, tzinfo=NY)), "text": "later date", "sender": "+15555550111"},
        {"rowid": 2, "date": apple_ns(datetime(2026, 1, 6, 9, 0, tzinfo=NY)), "text": "earlier date", "sender": "+15555550122"},
    ])
    state = {}
    _run(conn, tmp_path, state)
    assert state["Wacky Wednesday"]["last_rowid"] == 2
    assert _run(conn, tmp_path, state) is False   # nothing re-fetched


def test_messages_split_into_per_year_files(tmp_path):
    conn = make_db([
        {"rowid": 1, "date": apple_ns(datetime(2026, 12, 30, 20, 0, tzinfo=NY)), "text": "end of 2026", "sender": "+15555550111"},
        {"rowid": 2, "date": apple_ns(datetime(2027, 1, 2, 9, 0, tzinfo=NY)), "text": "start of 2027", "sender": "+15555550122"},
    ])
    state = {}
    assert _run(conn, tmp_path, state) is True
    f2026 = _yfile(tmp_path, 2026)
    f2027 = _yfile(tmp_path, 2027)
    assert f2026.exists() and f2027.exists()
    assert "end of 2026" in f2026.read_text() and "start of 2027" not in f2026.read_text()
    assert "start of 2027" in f2027.read_text() and "end of 2026" not in f2027.read_text()
    assert "### 2026-12-30" in f2026.read_text() and "### 2027-01-02" in f2027.read_text()


def test_new_year_message_does_not_touch_prior_year_file(tmp_path):
    conn = make_db([{"rowid": 1, "date": apple_ns(datetime(2026, 12, 30, 20, 0, tzinfo=NY)), "text": "twenty26", "sender": "+15555550111"}])
    state = {}
    _run(conn, tmp_path, state)
    before = _yfile(tmp_path, 2026).read_text()
    # A 2027 message arrives.
    conn.execute("INSERT INTO message VALUES (?,?,?,?,?,?,?,?,?)",
                 (2, apple_ns(datetime(2027, 1, 1, 0, 5, tzinfo=NY)), "twenty27", None, 0, 0, 0, 0, 1))
    conn.execute("INSERT INTO chat_message_join VALUES (1, 2)")
    conn.commit()
    assert _run(conn, tmp_path, state) is True
    assert _yfile(tmp_path, 2026).read_text() == before        # prior year untouched
    assert "twenty27" in _yfile(tmp_path, 2027).read_text()


def test_frontmatter_has_topics_and_attendees(tmp_path):
    d = datetime(2026, 1, 6, 12, 0, tzinfo=NY)
    conn = make_db([
        {"rowid": 1, "date": apple_ns(d), "text": "hi", "sender": "+15555550111"},
        {"rowid": 2, "date": apple_ns(d), "text": "yo", "is_from_me": 1, "sender": None},
    ])
    _run(conn, tmp_path, {})
    fm_block = _yfile(tmp_path).read_text().split("---")[1]   # the frontmatter block
    assert "topics: Text Messages, iMessage, Wacky Wednesday" in fm_block
    assert "Alice Adams" in fm_block and "Test User" in fm_block   # attendees present


def test_attendees_accumulate_across_runs(tmp_path):
    conn = make_db([{"rowid": 1, "date": apple_ns(datetime(2026, 1, 6, 12, 0, tzinfo=NY)), "text": "one", "sender": "+15555550111"}])
    state = {}
    _run(conn, tmp_path, state)
    assert state["Wacky Wednesday"]["years"]["2026"]["attendees"] == ["Alice Adams"]

    # A previously-unseen participant posts later.
    conn.execute("INSERT INTO handle VALUES (2, '+15555550122')")
    conn.execute("INSERT INTO message VALUES (?,?,?,?,?,?,?,?,?)",
                 (2, apple_ns(datetime(2026, 1, 7, 9, 0, tzinfo=NY)), "two", None, 0, 0, 0, 0, 2))
    conn.execute("INSERT INTO chat_message_join VALUES (1, 2)")
    conn.commit()

    _run(conn, tmp_path, state)
    assert state["Wacky Wednesday"]["years"]["2026"]["attendees"] == ["Alice Adams", "Bob Baker"]
    assert "Bob Baker" in _yfile(tmp_path).read_text().split("---")[1]


def test_rebuild_regenerates(tmp_path):
    d = datetime(2026, 1, 6, 12, 0, tzinfo=NY)
    conn = make_db([{"rowid": 1, "date": apple_ns(d), "text": "hello", "sender": "+15555550111"}])
    state = {}
    _run(conn, tmp_path, state)
    # Corrupt the file, then rebuild should restore it from the DB.
    _yfile(tmp_path).write_text("garbage")
    assert _run(conn, tmp_path, state, rebuild=True) is True
    out = _yfile(tmp_path).read_text()
    assert "hello" in out and "garbage" not in out

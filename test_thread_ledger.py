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


def _run(conn, tmp_path, state, rebuild=False, **thread_opts):
    resolver = tl.NameResolver(overrides={}, cache={})
    resolver._ab = {"5555550111": "Alice Adams", "5555550122": "Bob Baker"}
    thread = {"name": "Wacky Wednesday", "match": "Wacky Wednesday", **thread_opts}
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


def test_skip_pii_absent_by_default(tmp_path):
    conn = make_db([{"rowid": 1, "date": apple_ns(datetime(2026, 1, 6, 12, 0, tzinfo=NY)),
                     "text": "hi", "sender": "+15555550111"}])
    _run(conn, tmp_path, {})
    assert "skip_pii" not in _yfile(tmp_path).read_text()


def test_skip_pii_written_when_configured(tmp_path):
    conn = make_db([{"rowid": 1, "date": apple_ns(datetime(2026, 1, 6, 12, 0, tzinfo=NY)),
                     "text": "hi", "sender": "+15555550111"}])
    _run(conn, tmp_path, {}, skip_pii=True)
    fm_block = _yfile(tmp_path).read_text().split("---")[1]
    assert "skip_pii: true" in fm_block
    # the other keys are still there
    assert "topics: " in fm_block and "attendees: " in fm_block


def test_skip_pii_survives_an_incremental_append(tmp_path):
    # The frontmatter is regenerated on every append, so the flag has to be
    # re-emitted rather than inherited from the file on disk.
    conn = make_db([{"rowid": 1, "date": apple_ns(datetime(2026, 1, 6, 12, 0, tzinfo=NY)),
                     "text": "one", "sender": "+15555550111"}])
    state = {}
    _run(conn, tmp_path, state, skip_pii=True)
    conn.execute("INSERT INTO message VALUES (?,?,?,?,?,?,?,?,?)",
                 (2, apple_ns(datetime(2026, 1, 7, 9, 0, tzinfo=NY)), "two", None, 0, 0, 0, 0, 1))
    conn.execute("INSERT INTO chat_message_join VALUES (1, 2)")
    conn.commit()
    assert _run(conn, tmp_path, state, skip_pii=True) is True
    out = _yfile(tmp_path).read_text()
    assert out.split("---")[1].count("skip_pii: true") == 1
    assert "one" in out and "two" in out


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


# =========================================================================== #
# Reliability fixes
# =========================================================================== #
import copy
import json

D = lambda *a: apple_ns(datetime(*a, tzinfo=NY))  # noqa: E731  (y, m, d, h, mi)


def _add(conn, rowid, date, text, handle=1, **kw):
    """Insert one more message into chat 1 of a make_db() database."""
    conn.execute(
        "INSERT INTO message VALUES (?,?,?,?,?,?,?,?,?)",
        (rowid, date, text, kw.get("attributed_body"), kw.get("is_from_me", 0),
         kw.get("has_attachment", 0), kw.get("assoc_type", 0), 0, handle))
    conn.execute("INSERT INTO chat_message_join VALUES (1, ?)", (rowid,))
    conn.commit()


def _msgs(*specs):
    """specs: (rowid, apple_date, text[, sender]) -> make_db() message dicts."""
    out = []
    for s in specs:
        sender = s[3] if len(s) > 3 else "+15555550111"
        out.append({"rowid": s[0], "date": s[1], "text": s[2], "sender": sender})
    return out


def _fresh_text(tmp_path, specs, year=2026, **opts):
    """Render `specs` in a single fresh run in its own folder; return the file."""
    out = tmp_path / "fresh"
    _run(make_db(_msgs(*specs)), out, {}, **opts)
    return _yfile(out, year).read_text()


# --------------------------------------------------------------------------- #
# Email handles
# --------------------------------------------------------------------------- #
def test_norm_key_email_with_many_digits_is_not_a_phone():
    assert tl.norm_key("Jo5555550111@Example.com") == "jo5555550111@example.com"
    assert tl.norm_key("jo5555550111@example.com") != tl.norm_key("+15555550111")


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
    assert ab["5555550122"] == "Acme Pizza"                    # org fallback
    assert ab["dana5555550199@example.com"] == "Dana"           # email lookup
    assert "5555550199" not in ab                               # email != phone


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
    r = tl.NameResolver(overrides={}, cache={})
    r._ab = {"dana5555550199@example.com": "Dana"}
    tl.process_thread({"name": "Wacky Wednesday"}, conn, r, _cfg(tmp_path), {}, NY,
                      False, lambda m: None)
    assert "**Dana** (12:00 PM): hi" in _yfile(tmp_path).read_text()


# --------------------------------------------------------------------------- #
# Opening a live (WAL-mode) database
# --------------------------------------------------------------------------- #
def test_open_readonly_sees_uncheckpointed_wal_writes(tmp_path):
    db = tmp_path / "chat.db"
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")   # keep new rows in the -wal file
    writer.execute("CREATE TABLE t (x)")
    writer.commit()
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    writer.execute("INSERT INTO t VALUES (1)")
    writer.commit()                                 # committed, not checkpointed
    try:
        reader = tl.open_readonly(db)
        assert reader.execute("SELECT count(*) FROM t").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("INSERT INTO t VALUES (2)")   # still read-only
        reader.close()
    finally:
        writer.close()


# --------------------------------------------------------------------------- #
# Carriage returns
# --------------------------------------------------------------------------- #
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
# Appending in batches == one fresh run
# --------------------------------------------------------------------------- #
STREAM = [
    (1, D(2026, 1, 6, 9, 0), "mon morning"),
    (2, D(2026, 1, 6, 18, 0), "mon evening", "+15555550122"),
    (3, D(2026, 1, 7, 8, 0), "tue"),
    (4, D(2026, 1, 7, 9, 0), "multi\nline\n\nmessage", "+15555550122"),
    (5, D(2026, 1, 9, 12, 0), "thu"),
    (6, D(2026, 2, 1, 12, 0), "feb"),
]


@pytest.mark.parametrize("cuts", [[1], [2], [3], [1, 2, 3, 4, 5], [2, 4], [5]])
def test_batched_appends_match_a_fresh_run(tmp_path, cuts):
    # Whatever runs the messages happen to be split across, the file must come
    # out identical to a single fresh run (blank line before each new day).
    expected = _fresh_text(tmp_path, STREAM)
    conn = make_db(_msgs(*STREAM[:cuts[0]]))
    conn.execute("INSERT OR IGNORE INTO handle VALUES (2, '+15555550122')")
    state, inc = {}, tmp_path / "inc"
    _run(conn, inc, state)
    bounds = cuts + [len(STREAM)]
    for lo, hi in zip(bounds, bounds[1:]):
        for spec in STREAM[lo:hi]:
            _add(conn, spec[0], spec[1], spec[2], 2 if len(spec) > 3 else 1)
        _run(conn, inc, state)
    assert _yfile(inc).read_text() == expected


def test_append_on_new_day_adds_blank_line_before_heading(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 12, 0), "one")))
    state = {}
    _run(conn, tmp_path, state)
    _add(conn, 2, D(2026, 1, 7, 12, 0), "two")
    _run(conn, tmp_path, state)
    assert "): one\n\n### 2026-01-07 (Wed)\n" in _yfile(tmp_path).read_text()


def test_append_same_day_adds_no_blank_line(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 12, 0), "one")))
    state = {}
    _run(conn, tmp_path, state)
    _add(conn, 2, D(2026, 1, 6, 13, 0), "two")
    _run(conn, tmp_path, state)
    assert "): one\n**Alice Adams** (1:00 PM): two\n" in _yfile(tmp_path).read_text()


def test_legacy_state_with_plain_date_strings(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 12, 0), "one")))
    state = {}
    _run(conn, tmp_path, state)
    state["Wacky Wednesday"]["years"]["2026"] = "2026-01-06"   # old format
    _add(conn, 2, D(2026, 1, 6, 13, 0), "two")
    assert _run(conn, tmp_path, state) is True
    out = _yfile(tmp_path).read_text()
    assert out.count("### 2026-01-06") == 1 and "two" in out


# --------------------------------------------------------------------------- #
# Late-arriving messages
# --------------------------------------------------------------------------- #
def _three_days(tmp_path):
    conn = make_db(_msgs(
        (1, D(2026, 1, 5, 9, 0), "mon"),
        (2, D(2026, 1, 7, 9, 0), "wed"),
        (3, D(2026, 1, 9, 9, 0), "fri"),
    ))
    state = {}
    _run(conn, tmp_path / "inc", state)
    return conn, state


def test_late_message_goes_into_its_existing_day(tmp_path):
    conn, state = _three_days(tmp_path)
    _add(conn, 4, D(2026, 1, 7, 20, 0), "late wed")
    assert _run(conn, tmp_path / "inc", state) is True
    expected = _fresh_text(tmp_path, [
        (1, D(2026, 1, 5, 9, 0), "mon"), (2, D(2026, 1, 7, 9, 0), "wed"),
        (4, D(2026, 1, 7, 20, 0), "late wed"), (3, D(2026, 1, 9, 9, 0), "fri")])
    assert _yfile(tmp_path / "inc").read_text() == expected
    assert state["Wacky Wednesday"]["years"]["2026"]["last_date"] == "2026-01-09"


def test_late_message_creates_its_missing_day_in_order(tmp_path):
    conn, state = _three_days(tmp_path)
    _add(conn, 4, D(2026, 1, 6, 12, 0), "late tue")
    _add(conn, 5, D(2026, 1, 1, 12, 0), "late thu jan 1")   # before every day
    _run(conn, tmp_path / "inc", state)
    expected = _fresh_text(tmp_path, [
        (5, D(2026, 1, 1, 12, 0), "late thu jan 1"), (1, D(2026, 1, 5, 9, 0), "mon"),
        (4, D(2026, 1, 6, 12, 0), "late tue"), (2, D(2026, 1, 7, 9, 0), "wed"),
        (3, D(2026, 1, 9, 9, 0), "fri")])
    assert _yfile(tmp_path / "inc").read_text() == expected


def test_late_and_new_messages_in_the_same_run(tmp_path):
    conn, state = _three_days(tmp_path)
    _add(conn, 4, D(2026, 1, 10, 9, 0), "sat")
    _add(conn, 5, D(2026, 1, 7, 21, 0), "late wed")
    _add(conn, 6, D(2026, 1, 9, 22, 0), "late fri, same day as last heading")
    _run(conn, tmp_path / "inc", state)
    out = _yfile(tmp_path / "inc").read_text()
    assert out.index("late wed") < out.index("### 2026-01-09")
    assert out.index("late fri") < out.index("### 2026-01-10") < out.index("sat")
    assert out.count("### ") == 4


def test_late_message_in_a_prior_year_file(tmp_path):
    conn = make_db(_msgs(
        (1, D(2026, 12, 20, 9, 0), "dec 20"), (2, D(2026, 12, 30, 9, 0), "dec 30"),
        (3, D(2027, 1, 2, 9, 0), "jan 2")))
    state = {}
    _run(conn, tmp_path, state)
    before_2027 = _yfile(tmp_path, 2027).read_text()
    _add(conn, 4, D(2026, 12, 24, 9, 0), "late xmas eve")
    _run(conn, tmp_path, state)
    out = _yfile(tmp_path, 2026).read_text()
    assert out.index("dec 20") < out.index("### 2026-12-24") < out.index("late xmas eve") \
        < out.index("### 2026-12-30")
    assert _yfile(tmp_path, 2027).read_text() == before_2027   # untouched


def test_late_insert_into_file_without_blank_lines_before_headings(tmp_path):
    # Files written by older versions have no blank line before appended days.
    body = ["### 2026-01-05 (Mon)", "**A** (9:00 AM): mon",
            "### 2026-01-07 (Wed)", "**A** (9:00 AM): wed"]
    got = tl.insert_late(body, "2026-01-06", "### 2026-01-06 (Tue)", ["**A** (1:00 PM): tue"])
    assert got == ["### 2026-01-05 (Mon)", "**A** (9:00 AM): mon", "",
                   "### 2026-01-06 (Tue)", "**A** (1:00 PM): tue", "",
                   "### 2026-01-07 (Wed)", "**A** (9:00 AM): wed"]
    got = tl.insert_late(body, "2026-01-05", "### 2026-01-05 (Mon)", ["**A** (8:00 PM): late"])
    assert got[:3] == ["### 2026-01-05 (Mon)", "**A** (9:00 AM): mon", "**A** (8:00 PM): late"]


def test_late_insert_ignores_lookalike_text_lines():
    # A message line that merely mentions a date is not a heading.
    body = ["### 2026-01-05 (Mon)", "**A** (9:00 AM): see ### 2026-01-06 (Tue)",
            "### 2026-01-06 (Tue) is a nice day", "", "### 2026-01-07 (Wed)", "**A** (9:00 AM): x"]
    got = tl.insert_late(body, "2026-01-06", "### 2026-01-06 (Tue)", ["**A** (1:00 PM): tue"])
    assert got.index("### 2026-01-06 (Tue)") == 4


def test_late_message_that_renders_nothing_changes_nothing(tmp_path):
    conn, state = _three_days(tmp_path)
    before = _yfile(tmp_path / "inc").read_text()
    _add(conn, 4, D(2026, 1, 7, 20, 0), "Loved an image")   # SMS tapback
    assert _run(conn, tmp_path / "inc", state) is False
    assert _yfile(tmp_path / "inc").read_text() == before
    assert state["Wacky Wednesday"]["last_rowid"] == 4


# --------------------------------------------------------------------------- #
# Attendees
# --------------------------------------------------------------------------- #
def test_reaction_only_sender_is_not_an_attendee(tmp_path):
    conn = make_db([
        {"rowid": 1, "date": D(2026, 1, 6, 12, 0), "text": "hi", "sender": "+15555550111"},
        {"rowid": 2, "date": D(2026, 1, 6, 12, 1), "text": "Loved an image", "sender": "+15555550122"},
        {"rowid": 3, "date": D(2026, 1, 6, 12, 2), "text": None, "sender": "+15555550199"},
    ])
    state = {}
    _run(conn, tmp_path, state)
    assert state["Wacky Wednesday"]["years"]["2026"]["attendees"] == ["Alice Adams"]
    assert "attendees: Alice Adams\n" in _yfile(tmp_path).read_text()


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


def _crashing_run(conn, out, disk_state, crash_on, rebuild=False):
    """Run once, persisting state like main() does, but blow up at `crash_on`:
    ('save', n) = the n-th state save; ('write', n) = the n-th file write.
    Returns the state as it was last saved to disk."""
    counts = {"save": 0, "write": 0}
    state = copy.deepcopy(disk_state)
    saved = [copy.deepcopy(disk_state)]
    real_write = tl.atomic_write

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

    resolver = tl.NameResolver(overrides={}, cache={})
    resolver._ab = {"5555550111": "Alice Adams", "5555550122": "Bob Baker"}
    tl.atomic_write = write
    try:
        tl.process_thread({"name": "Wacky Wednesday"}, conn, resolver, _cfg(out), state,
                          NY, rebuild, lambda m: None, save=save)
    except Crash:
        pass
    finally:
        tl.atomic_write = real_write
    return saved[0]


TWO_YEARS = [(1, D(2026, 12, 30, 9, 0), "dec 30"), (2, D(2027, 1, 2, 9, 0), "jan 2")]
MORE = [(3, D(2026, 12, 31, 23, 0), "late dec 31"), (4, D(2027, 1, 3, 9, 0), "jan 3")]


@pytest.mark.parametrize("crash_on", [
    ("save", 1),    # before anything was written
    ("write", 1),   # intent saved, no file written
    ("write", 2),   # one of two year files written
    ("save", 2),    # both files written, commit not saved
    None,           # no crash
])
def test_crash_then_rerun_matches_clean_run(tmp_path, crash_on):
    conn = make_db(_msgs(*TWO_YEARS))
    disk = {}
    _run(conn, tmp_path / "clean", disk)          # reference run: no crash
    _run(conn, tmp_path / "crash", disk_c := {})  # same first run
    for spec in MORE:
        _add(conn, *spec)
    _run(conn, tmp_path / "clean", disk)

    disk_c = _crashing_run(conn, tmp_path / "crash", disk_c, crash_on)
    _run(conn, tmp_path / "crash", disk_c)        # recovery run
    for y in (2026, 2027):
        assert _yfile(tmp_path / "crash", y).read_text() == _yfile(tmp_path / "clean", y).read_text()
    assert disk_c == disk
    assert "pending" not in disk_c["Wacky Wednesday"]
    assert _run(conn, tmp_path / "crash", disk_c) is False   # and settles


@pytest.mark.parametrize("crash_on", [("write", 1), ("write", 2), ("save", 2)])
def test_crash_then_new_messages_before_recovery(tmp_path, crash_on):
    conn = make_db(_msgs(*TWO_YEARS))
    disk_c = _crashing_run(conn, tmp_path / "crash", {}, crash_on)
    _add(conn, 3, D(2027, 1, 2, 10, 0), "arrived after the crash")
    _add(conn, 4, D(2026, 12, 30, 10, 0), "late, after the crash")
    _run(conn, tmp_path / "crash", disk_c)
    expected = {2026: [(1, D(2026, 12, 30, 9, 0), "dec 30"), (4, D(2026, 12, 30, 10, 0), "late, after the crash")],
                2027: [(2, D(2027, 1, 2, 9, 0), "jan 2"), (3, D(2027, 1, 2, 10, 0), "arrived after the crash")]}
    for y, specs in expected.items():
        assert _yfile(tmp_path / "crash", y).read_text() == _fresh_text(tmp_path / str(y), specs, year=y)


def test_two_crashes_in_a_row_then_recovery(tmp_path):
    conn = make_db(_msgs(*TWO_YEARS))
    disk_c = _crashing_run(conn, tmp_path / "crash", {}, ("write", 2))   # 2026 written
    _add(conn, 3, D(2027, 1, 2, 10, 0), "more 2027")
    disk_c = _crashing_run(conn, tmp_path / "crash", disk_c, ("save", 2))  # 2027 written
    _run(conn, tmp_path / "crash", disk_c)
    out26 = _yfile(tmp_path / "crash", 2026).read_text()
    out27 = _yfile(tmp_path / "crash", 2027).read_text()
    assert out26.count("dec 30") == 1
    assert out27.count("jan 2") == 1 and out27.count("more 2027") == 1


def test_file_edited_after_a_crash_is_not_treated_as_written(tmp_path):
    # If a file doesn't match what the interrupted run meant to write, that
    # write didn't land; the messages are added normally (no loss).
    conn = make_db(_msgs((1, D(2026, 1, 6, 9, 0), "one")))
    disk_c = _crashing_run(conn, tmp_path, {}, ("write", 1))
    assert "pending" in disk_c["Wacky Wednesday"]
    _run(conn, tmp_path, disk_c)
    assert _yfile(tmp_path).read_text().count("one") == 1


# --------------------------------------------------------------------------- #
# Rebuild
# --------------------------------------------------------------------------- #
def test_rebuild_with_no_messages_keeps_files_and_state(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 9, 0), "one")))
    state = {}
    _run(conn, tmp_path, state)
    before, before_state = _yfile(tmp_path).read_text(), copy.deepcopy(state)
    conn.execute("DELETE FROM chat_message_join")   # Messages lost the thread
    conn.commit()
    assert _run(conn, tmp_path, state, rebuild=True) is False
    assert _yfile(tmp_path).read_text() == before
    assert state == before_state
    _add(conn, 2, D(2026, 1, 7, 9, 0), "two")       # later messages: no dupes
    _run(conn, tmp_path, state)
    out = _yfile(tmp_path).read_text()
    assert out.count("one") == 1 and out.count("two") == 1


def test_rebuild_keeps_years_messages_no_longer_has(tmp_path):
    conn = make_db(_msgs(*TWO_YEARS))
    state = {}
    _run(conn, tmp_path, state)
    old_2026 = _yfile(tmp_path, 2026).read_text()
    conn.execute("DELETE FROM chat_message_join WHERE message_id = 1")   # 2026 pruned
    conn.commit()
    _yfile(tmp_path, 2027).write_text("garbage")
    assert _run(conn, tmp_path, state, rebuild=True) is True
    assert _yfile(tmp_path, 2026).read_text() == old_2026
    assert "jan 2" in _yfile(tmp_path, 2027).read_text()
    assert set(state["Wacky Wednesday"]["years"]) == {"2026", "2027"}


def test_rebuild_matches_fresh_and_fixes_missing_blank_lines(tmp_path):
    conn = make_db(_msgs(*STREAM))
    state = {}
    _run(conn, tmp_path / "inc", state)
    f = _yfile(tmp_path / "inc")
    f.write_text(f.read_text().replace("\n\n###", "\n###"))   # old-style file
    _run(conn, tmp_path / "inc", state, rebuild=True)
    assert f.read_text() == _fresh_text(tmp_path, STREAM)


def test_rebuild_twice_is_stable(tmp_path):
    conn = make_db(_msgs(*STREAM))
    state = {}
    _run(conn, tmp_path, state, rebuild=True)
    first, first_state = _yfile(tmp_path).read_text(), copy.deepcopy(state)
    _run(conn, tmp_path, state, rebuild=True)
    assert _yfile(tmp_path).read_text() == first and state == first_state


def test_thread_name_with_glob_characters(tmp_path):
    conn = make_db(_msgs((1, D(2026, 1, 6, 9, 0), "one")))
    resolver = _resolver()
    thread = {"name": "Fam [2026] *best*", "match": "Wacky"}
    state = {}
    for rebuild in (False, True, False):
        tl.process_thread(thread, conn, resolver, _cfg(tmp_path), state, NY, rebuild, lambda m: None)
    out = (tmp_path / "Fam [2026] *best*" / "Fam [2026] *best* - 2026.md").read_text()
    assert out.count("one") == 1


# --------------------------------------------------------------------------- #
# main(): the whole program, end to end
# --------------------------------------------------------------------------- #
def _main_setup(tmp_path, monkeypatch, specs):
    db = tmp_path / "chat.db"
    mem = make_db(_msgs(*specs))
    disk = sqlite3.connect(db)
    mem.backup(disk)
    disk.execute("PRAGMA journal_mode=WAL")
    disk.commit()
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'output_dir = "{tmp_path / "out"}"\ntimezone = "America/New_York"\n'
        f'me = "Test User"\nchat_db = "{db}"\n'
        '[[threads]]\nname = "Wacky Wednesday"\nmatch = "Wacky Wednesday"\n'
        '[contacts]\n"+15555550111" = "Alice Adams"\n')
    monkeypatch.setattr(tl, "CACHE_PATH", tmp_path / "contacts_cache.json")
    monkeypatch.setattr(tl, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(tl, "build_addressbook_map", lambda: {})   # no real Contacts
    return cfg, disk


def test_main_end_to_end(tmp_path, monkeypatch, capsys):
    cfg, disk = _main_setup(tmp_path, monkeypatch, STREAM[:3])
    assert tl.main(["--config", str(cfg)]) == 0
    out_file = tmp_path / "out" / "Wacky Wednesday" / "Wacky Wednesday - 2026.md"
    first = out_file.read_text()
    assert "**Alice Adams** (9:00 AM): mon morning" in first
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["Wacky Wednesday"]["last_rowid"] == 3 and "pending" not in state["Wacky Wednesday"]

    # Nothing new: file untouched, says so.
    mtime = out_file.stat().st_mtime_ns
    assert tl.main(["--config", str(cfg)]) == 0
    assert out_file.stat().st_mtime_ns == mtime
    assert "nothing to write" in capsys.readouterr().out

    # New messages land while the writer keeps the DB open (live WAL).
    disk.execute("PRAGMA wal_autocheckpoint=0")
    disk.execute("INSERT INTO message VALUES (4, ?, 'thu', NULL, 0, 0, 0, 0, 1)", (D(2026, 1, 9, 12, 0),))
    disk.execute("INSERT INTO chat_message_join VALUES (1, 4)")
    disk.commit()
    assert tl.main(["--config", str(cfg), "--quiet"]) == 0
    assert capsys.readouterr().out == ""
    assert out_file.read_text() == first + "\n### 2026-01-09 (Fri)\n**Alice Adams** (12:00 PM): thu\n"
    assert [p.name for p in out_file.parent.iterdir()] == [out_file.name]   # no temp files
    disk.close()


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
    disk.close()
    cfg.write_text(cfg.read_text().replace(
        "[contacts]", '[[threads]]\nname = "Second"\nmatch = "Wacky"\n[contacts]'))
    real_fetch = tl.fetch_messages
    calls = []

    def fetch(conn, match, since):
        calls.append(match)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real_fetch(conn, match, since)

    monkeypatch.setattr(tl, "fetch_messages", fetch)
    with pytest.raises(RuntimeError):
        tl.main(["--config", str(cfg), "--quiet"])
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["Wacky Wednesday"]["last_rowid"] == 2       # first thread saved
    assert "Second" not in state

    monkeypatch.setattr(tl, "fetch_messages", real_fetch)
    assert tl.main(["--config", str(cfg), "--quiet"]) == 0   # rerun: no dupes
    out = (tmp_path / "out" / "Wacky Wednesday" / "Wacky Wednesday - 2026.md").read_text()
    assert out.count("mon morning") == 1
    assert (tmp_path / "out" / "Second" / "Second - 2026.md").exists()

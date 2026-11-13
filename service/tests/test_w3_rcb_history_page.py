"""W3 runtime-core-b red tests: one bad historical row must not refuse the whole page.

native_connector.messages parses every row with message(..., history=True); one row that
is a tip/unsent/deleted or carries unsupported formatting raises, so every other row of
the page (including a newer valid inbound message) is never ingested. Per-row refusal is
pinned elsewhere (test_onlyfans_native_history.py) and is NOT contradicted here: the
claim is page-level only. Synthetic wire, no network, no PG.
"""
import json
import os
from unittest.mock import patch

import pytest

from onlyfans_native_protocol_fixture import native_message
from onlyfans_runtime.native_protocol import NativeProtocolError, history_text
from onlyfans_runtime.provider_connector import NativeProviderConnector
from test_onlyfans_vault_response_limits import session, OWNER

FAN = 101


def _page(rows):
    def read(method, path, body=b"", **options):
        if path.endswith("/users/me"):
            return json.dumps({"id": OWNER}).encode()
        if path == f"/api2/v2/chats/{FAN}":
            return json.dumps({"withUser": {"id": FAN}, "unreadMessagesCount": 0,
                               "hasUnreadMessages": False}).encode()
        assert f"/chats/{FAN}/messages?" in path, path
        return json.dumps({"list": rows, "hasMore": False}).encode()
    return read


def _read_page(rows, tmp_path, before_id=None):
    wire = NativeProviderConnector(session())
    with patch.dict(os.environ, {"ONLYFANS_PROCESS_ROLE": "worker",
                                 "ONLYFANS_UNREAD_JOURNAL_DIR": str(tmp_path)}), \
            patch.object(wire, "_signed_request", side_effect=_page(rows)):
        return wire.messages(fan_id=FAN, before_id=before_id, limit=10)


def test_control_valid_page_reads(tmp_path):
    # Setup sanity (green at PIN): the same newer row alone is ingested.
    page = _read_page([native_message(20, owner=OWNER, text="<p>Synthetic newer inbound</p>")], tmp_path)
    assert [event.message_id for event in page.events] == [20]


@pytest.mark.parametrize("old_row", [
    {**native_message(10, owner=OWNER), "isTip": True},
    {**native_message(10, owner=OWNER), "isDeleted": True},
    native_message(10, owner=OWNER, text='<p><img src="x"></p>'),
], ids=["old-tip", "old-deleted", "old-unsupported-formatting"])
def test_newer_valid_inbound_survives_one_bad_older_row(old_row, tmp_path):
    newer = native_message(20, owner=OWNER, text="<p>Synthetic newer inbound</p>")
    try:
        page = _read_page([newer, old_row], tmp_path)
    except NativeProtocolError as exc:
        pytest.fail(f"whole page refused by one bad older row ({exc}); newer inbound 20 never ingested")
    ids = [event.message_id for event in page.events]
    assert 20 in ids, f"newer valid inbound row 20 not returned; page events {ids}"


# (g) INFERRED shapes, not observed on the wire: whitespace after the paragraph wrapper and
# a paragraph with attributes. history_text currently raises for both.
@pytest.mark.parametrize("text", ["<p>hi</p>\n", '<p class="x">hi</p>'])
def test_history_text_accepts_benign_paragraph_variants_inferred(text):
    assert history_text(text).strip() == "hi"


# G04 guard: the relaxed paragraph wrapper still refuses non-paragraph or unsupported markup.
@pytest.mark.parametrize("text", ["<pre>hi</pre>", "<p>hi</p>x", '<p><img src="x"></p>',
                                  "<p>hi</p><script>x</script>", " hi <p>x</p>",
                                  "<p>a</p>junk<p>b</p>", " <p>hi</p><img src=x>", " <p class=x>hi"])
def test_history_text_still_refuses_unsupported_wrappers(text):
    with pytest.raises(NativeProtocolError):
        history_text(text)


# Flash W3RCB-01 r1 (blocker): only markup with NO visible text may be skipped. Fan words behind
# unsupported markup must refuse the page (protocol doc: never silently lose meaning), not vanish.
@pytest.mark.parametrize("text", ["<blockquote>fan words</blockquote>", "I <3 you", "\ud800"],
                         ids=["blockquote", "bare-lt", "lone-surrogate"])
def test_visible_text_behind_unsupported_markup_refuses_never_drops(text, tmp_path):
    newer = native_message(20, owner=OWNER, text="<p>Synthetic newer inbound</p>")
    old = native_message(10, owner=OWNER, text=text)
    with pytest.raises(NativeProtocolError):
        _read_page([newer, old], tmp_path)


# Opus-final W3RCB-01 r1 (major): a fan's markup-only row with no newer fan text kept on the page
# would leave Store._skipped_input re-arming the reply onto an older message: refuse (fail closed).
def test_newest_fan_markup_only_row_refuses_the_page(tmp_path):
    newest = native_message(20, owner=OWNER, text='<p><img src="x"></p>')
    older = native_message(10, owner=OWNER, text="<p>Synthetic older inbound</p>")
    with pytest.raises(NativeProtocolError):
        _read_page([newest, older], tmp_path)


# Batch5 DA D2 (major): a creator's newest markup-only row skipped would make the older fan message
# (already answered by a human) the latest event and re-arm a reply: refuse (fail closed, as before #129).
def test_newest_creator_markup_only_row_refuses_the_page(tmp_path):
    newest = native_message(20, owner=OWNER, outgoing=True, text='<p><img src="x"></p>')
    older = native_message(10, owner=OWNER, text="<p>Synthetic older inbound</p>")
    with pytest.raises(NativeProtocolError):
        _read_page([newest, older], tmp_path)


def test_creator_markup_only_row_below_a_newer_kept_row_is_skipped(tmp_path):
    newer = native_message(30, owner=OWNER, text="<p>Synthetic newer inbound</p>")
    creator = native_message(20, owner=OWNER, outgoing=True, text='<p><img src="x"></p>')
    older = native_message(10, owner=OWNER, text="<p>Synthetic older inbound</p>")
    page = _read_page([newer, creator, older], tmp_path)
    assert [event.message_id for event in page.events] == [30, 10] and page.skipped_inbound == ()
    assert page.next_before == 10


# Opus DA W3RCB-01b I2 (minor): on an archive page (before_id) the newest event came from a newer
# page, so a creator markup-only row at the page top is skipped rather than refusing the backfill.
def test_archive_page_top_creator_markup_only_row_is_skipped(tmp_path):
    creator = native_message(20, owner=OWNER, outgoing=True, text='<p><img src="x"></p>')
    older = native_message(10, owner=OWNER, text="<p>Synthetic older inbound</p>")
    page = _read_page([creator, older], tmp_path, before_id=50)
    assert [event.message_id for event in page.events] == [10] and page.skipped_inbound == ()


def test_archive_page_top_fan_markup_only_row_is_skipped(tmp_path):
    # Flash W3RCB-01b (major): archive/offer-import recovery re-queues a refused cursor forever.
    fan = native_message(20, owner=OWNER, text='<p><img src="x"></p>')
    older = native_message(10, owner=OWNER, outgoing=True, text="<p>Synthetic older reply</p>")
    page = _read_page([fan, older], tmp_path, before_id=50)
    assert [event.message_id for event in page.events] == [10] and page.skipped_inbound == (20,)


def test_head_fan_markup_row_below_only_creator_rows_refuses(tmp_path):
    # Flash minor: pins the fan rule (newer FAN text), not just any newer kept row.
    rows = [native_message(30, owner=OWNER, outgoing=True, text="<p>Synthetic reply</p>"),
            native_message(20, owner=OWNER, text='<p><img src="x"></p>'),
            native_message(10, owner=OWNER, text="<p>Synthetic older inbound</p>")]
    with pytest.raises(NativeProtocolError):
        _read_page(rows, tmp_path)


def test_skipped_bottom_row_still_advances_the_cursor(tmp_path):
    # Flash minor: next_before must come from the skipped row when it is the oldest.
    rows = [native_message(30, owner=OWNER, text="<p>Synthetic newer inbound</p>"),
            native_message(20, owner=OWNER, outgoing=True, text='<p><img src="x"></p>')]
    page = _read_page(rows, tmp_path)
    assert [event.message_id for event in page.events] == [30] and page.next_before == 20

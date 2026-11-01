"""Legacy archive BR fragments must not block all older messages in a page."""
import pytest

from onlyfans_runtime.native_protocol import NativeProtocolError, history_text, message, plain_text


@pytest.mark.parametrize("tag", ["<br>", "<br/>", "<br />"])
def test_legacy_breaks_preserve_text_entities_and_existing_newlines(tag):
    assert history_text(f"First &amp; second{tag}\n{tag}Last") == "First & second\n\n\nLast"


@pytest.mark.parametrize("body", [
    "text<br /><script>bad</script>", "text<br /><img src=x>",
    "text<br onclick=x>more", "text<br /></p>", "text<br /><p>open",
    "text<br /><unknown>more</unknown>", "text<br /><!--comment-->",
    "text<br />unfinished<", "text<br />literal >", "x" * 20001 + "<br />",
])
def test_legacy_breaks_do_not_accept_other_markup_or_oversize(body):
    with pytest.raises(NativeProtocolError):
        history_text(body)


def test_archive_message_preserves_identity_media_price_and_timestamp():
    row = dict(id=4813972410044, fromUser={"id": 100},
        createdAt="2025-01-18T04:32:48+00:00", text="First<br />\n<br />Last",
        media=[{"id": 500}, {"id": 501}], mediaCount=2,
        price="12.50", responseType="message", isTip=False, isOpened=False)
    event = message(row, owner_id=100, fan_id=200, history=True)
    assert (event.message_id, event.fan_id, event.direction) == (4813972410044, 200, "out")
    assert event.body == "First\n\n\nLast"
    assert event.created_at == row["createdAt"]
    assert event.media_ids == (500, 501)
    assert event.price_cents == 1250
    with pytest.raises(NativeProtocolError):
        message(row, owner_id=100, fan_id=200)
    with pytest.raises(NativeProtocolError):
        plain_text(row["text"])


def test_legacy_breaks_preserve_existing_paragraph_rules():
    assert history_text("<p>First<br />Last</p>") == "First\nLast"
    assert history_text("<p>First <strong>bold</strong></p>") == "First bold"
    with pytest.raises(NativeProtocolError):
        history_text("<p>First<strong>broken</p>")

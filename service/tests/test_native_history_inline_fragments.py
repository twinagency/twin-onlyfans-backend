"""Provider history fragments retain visible text without changing send receipts."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from onlyfans_native_protocol_fixture import OWNER, native_message
from onlyfans_runtime.native_protocol import NativeProtocolError, message


@pytest.mark.parametrize("body,expected", [
    ('See <a href="https://example.invalid/page">my page</a> here',
     "See my page here"),
    ('<a href="https://example.invalid/page">My page</a>', "My page"),
    ('Hello <strong>there</strong> &amp; <em>welcome</em>', "Hello there & welcome"),
    ('One<br><a href="https://example.invalid">two</a><br />three',
     "One\ntwo\nthree"),
])
def test_unwrapped_history_fragment_preserves_visible_text(body, expected):
    row = native_message(text=body)
    assert message(row, owner_id=OWNER, fan_id=101, history=True).body == expected
    # Provider history is separate from exact send/receipt evidence.
    with pytest.raises(NativeProtocolError):
        message(row, owner_id=OWNER, fan_id=101)


@pytest.mark.parametrize("body", [
    'See <a href="x">unfinished',
    'See <a href="x">wrong close</strong>',
    '<a href="x"><a href="y">nested links</a></a>',
    'See <script>active()</script>',
    'See <iframe>hidden</iframe>',
    'See <!-- hidden --> text',
    'See <style>hidden</style>',
    '<p>unfinished',
    '<p>wrapped</p> trailing',
])
def test_unwrapped_history_still_rejects_unsupported_or_unbalanced_markup(body):
    with pytest.raises(NativeProtocolError):
        message(native_message(text=body), owner_id=OWNER, fan_id=101, history=True)

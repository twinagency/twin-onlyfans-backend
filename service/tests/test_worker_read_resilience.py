"""A slow page or one odd row must not pin an inbox/history read position."""
from collections import OrderedDict
import json
import logging
import os
from urllib.parse import parse_qs, urlsplit

import pytest

from onlyfans_native_protocol_fixture import native_message
from onlyfans_runtime import native_connector, provider_connector as connector
from onlyfans_runtime.native_protocol import NativeProtocolError, history_text
from test_onlyfans_vault_response_limits import OWNER, session

FAN = 101


@pytest.fixture
def wire(monkeypatch, tmp_path):
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE', 'worker')
    monkeypatch.setenv('ONLYFANS_UNREAD_JOURNAL_DIR', str(tmp_path))
    monkeypatch.setattr(connector, '_PAGE_LIMITS', OrderedDict(), raising=False)
    monkeypatch.setattr(native_connector, '_SKIP_LOGGED', {}, raising=False)
    state = {'rows': [native_message(10, owner=OWNER)], 'more': False, 'fail': [], 'limits': [], 'methods': []}
    reader = connector.NativeProviderConnector(session())

    def signed(method, path, body=b'', **_):
        state['methods'].append(method)
        if path.endswith('/users/me'):
            return json.dumps({'id': OWNER}).encode()
        if path == f'/api2/v2/chats/{FAN}':
            return json.dumps({'withUser': {'id': FAN}, 'unreadMessagesCount': 0}).encode()
        state['limits'].append(int(parse_qs(urlsplit(path).query)['limit'][0]))
        if state['fail']:
            error = state['fail'].pop(0)
            raise error
        return json.dumps({'list': state['rows'], 'hasMore': state['more']}).encode()

    monkeypatch.setattr(reader, '_signed_request', signed)
    return reader, state


def timeout():
    error = connector.NativeReadTransientError()
    error.read_failure = {'failure': 'provider_timeout', 'category': 'timeout'}
    return error


def test_timeout_shrinks_then_grows_slowly_and_never_back_to_the_failing_size(wire):
    reader, state = wire
    state['fail'] = [timeout()]
    with pytest.raises(connector.NativeReadTransientError):
        reader.messages(fan_id=FAN, before_id=500, limit=100)
    for _ in range(10):
        assert reader.messages(fan_id=FAN, before_id=500, limit=100).events[0].message_id == 10
    state['fail'] = [timeout(), timeout(), timeout()]
    for _ in range(3):
        with pytest.raises(connector.NativeReadTransientError):
            reader.messages(fan_id=FAN, before_id=500, limit=100)
    assert state['limits'] == [100, 20, 20, 20, 40, 40, 40, 80, 80, 80, 80, 80, 20, 10]
    assert set(state['methods']) == {'GET'}


def test_head_and_deep_reads_keep_separate_page_caps(wire):
    reader, state = wire
    state['fail'] = [timeout()]
    with pytest.raises(connector.NativeReadTransientError):
        reader.messages(fan_id=FAN, before_id=500, limit=100)
    for _ in range(6):  # recent-inbox head reads succeed at full size
        reader.messages(fan_id=FAN, before_id=None, limit=100)
    reader.messages(fan_id=FAN, before_id=500, limit=100)
    state['fail'] = [timeout()]
    with pytest.raises(connector.NativeReadTransientError):
        reader.messages(fan_id=FAN, before_id=None, limit=100)
    reader.messages(fan_id=FAN, before_id=None, limit=100)
    reader.messages(fan_id=FAN, before_id=500, limit=100)
    assert state['limits'] == [100] + [100] * 6 + [20, 100, 20, 20]


def test_oversize_shrinks_but_other_failures_keep_page_size(wire):
    reader, state = wire
    state['fail'] = [NativeProtocolError('native HTTPS response exceeded its byte budget'),
                     connector.NativeRateLimitError(), NativeProtocolError('other')]
    for _ in range(3):
        with pytest.raises(NativeProtocolError):
            reader.messages(fan_id=FAN, before_id=None, limit=100)
    assert state['limits'] == [100, 20, 20]


def test_bare_autolinked_url_keeps_its_visible_url():
    url = 'https://onlyfans.com/123/synthetic_creator'
    assert history_text(f'look at this <a href="{url}">{url}</a>') == f'look at this {url}'
    assert history_text(f'one<br /><a href="{url}">{url}</a>') == f'one\n{url}'
    for text in [f'x <a href="{url}">click here</a>', f'x <a href="javascript:alert(1)">javascript:alert(1)</a>',
                 f'x <a href="{url}" target="_blank">{url}</a>', f'x <a href="{url}">{url}</a><img src=x>',
                 f'x <a href="{url}">{url}</a><br onclick=x>', 'x <a href="http://a b">http://a b</a>']:
        with pytest.raises(NativeProtocolError):
            history_text(text)


def test_incoming_row_with_autolink_imports(wire):
    reader, state = wire
    url = 'https://onlyfans.com/123/synthetic_creator'
    state['rows'] = [native_message(12, owner=OWNER, text=f'hey <a href="{url}">{url}</a>')]
    page = reader.messages(fan_id=FAN, before_id=None, limit=100)
    assert [(e.direction, e.body) for e in page.events] == [('in', f'hey {url}')]


def test_bare_mention_is_the_only_new_bare_markup():
    assert history_text('Thanks <a href="/some.creator_1">@some.creator_1</a> &amp; bye') == 'Thanks @some.creator_1 & bye'
    for text in ['Hi <a href="https://x.invalid/a">@a</a>', 'Hi <a href="/a" target="_blank">@a</a>',
                 'Hi <a href="/a">@a</a><b>x</b>', 'Hi <a href="/a">no-at</a>', 'Hi <a href="/a">@a</a> >']:
        with pytest.raises(NativeProtocolError):
            history_text(text)


def test_mention_row_imports(wire):
    reader, state = wire
    state['rows'] = [native_message(9669853696287, owner=OWNER, outgoing=True,
                                    text='so glad <a href="/synthetic_creator">@synthetic_creator</a> saw it')]
    page = reader.messages(fan_id=FAN, before_id=None, limit=100)
    assert [e.body for e in page.events] == ['so glad @synthetic_creator saw it']


def test_only_tip_unsent_deleted_rows_are_skipped_and_counted(wire, caplog):
    reader, state = wire
    state['rows'] = [native_message(16, owner=OWNER), {**native_message(15, owner=OWNER), 'isTip': True},
        {**native_message(14, owner=OWNER), 'isUnsent': True}, {**native_message(13, owner=OWNER), 'isDeleted': True},
        native_message(12, owner=OWNER), {**native_message(11, owner=OWNER), 'isTip': True}]
    state['more'] = True
    with caplog.at_level(logging.WARNING):
        page = reader.messages(fan_id=FAN, before_id=None, limit=100)
    assert [e.message_id for e in page.events] == [16, 12] and page.has_more
    assert page.next_before == 11  # skipped rows are behind the cursor, never re-read
    line = next(r.getMessage() for r in caplog.records if 'onlyfans_history_rows_skipped' in r.getMessage())
    assert json.loads(line.split(' ', 1)[1]) == {'account': 'acc-a', 'rows': 6, 'indexes': [1, 2, 3, 5],
        'skipped': {'deleted': 1, 'tip': 2, 'unsent': 1}, 'suppressed': 0}
    assert str(FAN) not in line


def test_skip_log_is_rate_limited_per_account(wire, caplog):
    reader, state = wire
    state['rows'] = [native_message(12, owner=OWNER), {**native_message(11, owner=OWNER), 'isTip': True}]
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            reader.messages(fan_id=FAN, before_id=None, limit=100)
    assert sum('onlyfans_history_rows_skipped' in r.getMessage() for r in caplog.records) == 1


def test_page_of_only_skipped_rows_advances_the_cursor(wire):
    reader, state = wire
    state['rows'] = [{**native_message(11, owner=OWNER), 'isTip': True}, {**native_message(10, owner=OWNER), 'isDeleted': True}]
    state['more'] = True
    page = reader.messages(fan_id=FAN, before_id=500, limit=100)
    assert (page.events, page.has_more, page.next_before) == ([], True, 10)


@pytest.mark.parametrize('change', [
    {'text': None}, {'mediaCount': 2}, {'media': [{'id': 5}, {'id': 5}], 'mediaCount': 2},
    {'media': [{'id': '5'}], 'mediaCount': 1}, {'price': None}, {'price': '0.001'},
    {'responseType': 'system'}, {'text': '<p><script>x</script></p>'},
], ids=['text', 'mediaCount', 'dup-media', 'str-media', 'null-price', 'sub-cent', 'responseType', 'markup'])
def test_malformed_or_unsupported_rows_still_refuse_the_page(wire, change):
    reader, state = wire
    row = {**native_message(11, owner=OWNER, outgoing=True, media=(7,), price=5), **change}
    if 'text' in change and change['text'] is None:
        del row['text']
    state['rows'] = [native_message(12, owner=OWNER), row]
    with pytest.raises(NativeProtocolError):
        reader.messages(fan_id=FAN, before_id=None, limit=100)


@pytest.mark.parametrize('rows,more', [
    ([native_message(11, owner=OWNER), {**native_message(10, owner=OWNER), 'fromUser': {'id': 999}, 'isTip': True}], False),
    ([native_message(11, owner=OWNER), {**native_message(10, owner=OWNER), 'createdAt': 'x', 'isTip': True}], False),
    ([native_message(10, owner=OWNER), {**native_message(11, owner=OWNER), 'isTip': True}], False),
])
def test_out_of_scope_or_disordered_skipped_rows_still_refuse(wire, rows, more):
    reader, state = wire
    state['rows'], state['more'] = rows, more
    with pytest.raises(NativeProtocolError):
        reader.messages(fan_id=FAN, before_id=None, limit=100)

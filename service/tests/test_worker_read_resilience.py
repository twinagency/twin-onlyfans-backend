"""A slow page or one odd row must not pin an inbox/history read position."""
from collections import OrderedDict
import json
import logging
import os
from urllib.parse import parse_qs, urlsplit

import pytest

from onlyfans_native_protocol_fixture import native_message
from onlyfans_runtime import provider_connector as connector
from onlyfans_runtime.native_protocol import NativeProtocolError, history_text
from test_onlyfans_vault_response_limits import OWNER, session

FAN = 101


@pytest.fixture
def wire(monkeypatch, tmp_path):
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE', 'worker')
    monkeypatch.setenv('ONLYFANS_UNREAD_JOURNAL_DIR', str(tmp_path))
    monkeypatch.setattr(connector, '_PAGE_LIMITS', OrderedDict(), raising=False)
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


def test_timeout_retries_same_position_with_smaller_page_then_grows_back(wire):
    reader, state = wire
    state['fail'] = [timeout()]
    with pytest.raises(connector.NativeReadTransientError):
        reader.messages(fan_id=FAN, before_id=500, limit=100)
    for _ in range(3):
        assert reader.messages(fan_id=FAN, before_id=500, limit=100).events[0].message_id == 10
    state['fail'] = [timeout(), timeout(), timeout(), timeout()]
    for _ in range(4):
        with pytest.raises(connector.NativeReadTransientError):
            reader.messages(fan_id=FAN, before_id=500, limit=100)
    assert state['limits'] == [100, 20, 40, 80, 100, 20, 10, 5]
    assert set(state['methods']) == {'GET'}


def test_oversize_shrinks_but_other_failures_keep_page_size(wire):
    reader, state = wire
    state['fail'] = [NativeProtocolError('native HTTPS response exceeded its byte budget'),
                     connector.NativeRateLimitError(), NativeProtocolError('other')]
    for _ in range(3):
        with pytest.raises(NativeProtocolError):
            reader.messages(fan_id=FAN, before_id=None, limit=100)
    assert state['limits'] == [100, 20, 20]


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


def test_unsupported_rows_are_skipped_and_counted_not_page_fatal(wire, caplog):
    reader, state = wire
    state['rows'] = [native_message(16, owner=OWNER), {**native_message(15, owner=OWNER), 'isTip': True},
        {**native_message(14, owner=OWNER), 'isUnsent': True}, {**native_message(13, owner=OWNER), 'isDeleted': True},
        native_message(12, owner=OWNER, text='<p><script>x</script></p>'), native_message(11, owner=OWNER)]
    state['more'] = True
    with caplog.at_level(logging.WARNING):
        page = reader.messages(fan_id=FAN, before_id=None, limit=100)
    assert [e.message_id for e in page.events] == [16, 11] and page.has_more
    line = next(r.getMessage() for r in caplog.records if 'onlyfans_history_rows_skipped' in r.getMessage())
    logged = json.loads(line.split(' ', 1)[1])
    details = logged.pop('details')
    assert logged == {'account': 'acc-a', 'rows': 6,
        'skipped': {'deleted': 1, 'tip': 1, 'unsent': 1, 'unsupported': 1}}
    assert [d['reason'] for d in details] == ['tip', 'unsent', 'deleted', 'unsupported']
    odd = details[-1]
    assert odd['fan'] == FAN and odd['tags'] == ['p', 'script'] and odd['empty'] is False
    assert odd['cause'].startswith('NativeProtocolError:') and odd['missing'] == []
    assert 'x</script>' not in line  # shape only, never message text


@pytest.mark.parametrize('rows,more', [
    ([native_message(11, owner=OWNER), {**native_message(10, owner=OWNER), 'fromUser': {'id': 999}, 'isTip': True}], False),
    ([native_message(11, owner=OWNER), {**native_message(10, owner=OWNER), 'createdAt': 'x', 'isTip': True}], False),
    ([native_message(10, owner=OWNER), {**native_message(11, owner=OWNER), 'isTip': True}], False),
    ([{**native_message(10, owner=OWNER), 'isTip': True}], True),
])
def test_out_of_scope_disordered_or_all_skipped_pages_still_refuse(wire, rows, more):
    reader, state = wire
    state['rows'], state['more'] = rows, more
    with pytest.raises(NativeProtocolError):
        reader.messages(fan_id=FAN, before_id=None, limit=100)

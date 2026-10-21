"""Inbox observations do not grant send or purchase evidence."""
import json
import os
import unittest
from unittest.mock import patch
from onlyfans_native_protocol_fixture import native_message, OWNER
from onlyfans_runtime.native_protocol import NativeProtocolError, message, load, MAX_BYTES, MAX_HISTORY_BYTES
from onlyfans_runtime.provider_connector import NativeProviderConnector
from test_onlyfans_vault_response_limits import session


class NativeHistory(unittest.TestCase):
    def test_link_history_retains_visible_text_without_link_authority(self):
        raw = native_message(10, owner=OWNER, outgoing=True,
            text='<p>Read <a href="https://synthetic.invalid/private" target="_blank">the &lt;update&gt;</a><br/>Today</p><p>Thanks &amp; bye</p>')
        event = message(raw, owner_id=OWNER, fan_id=101, history=True)
        self.assertEqual(event.body, 'Read the <update>\nToday\nThanks & bye')
        self.assertNotIn('synthetic.invalid', event.payload())
        with self.assertRaises(NativeProtocolError):
            message(raw, owner_id=OWNER, fan_id=101)

    def test_unsupported_or_unbalanced_history_html_is_not_silently_dropped(self):
        for text in ['<p><script>hidden()</script></p>', '<p><a href="x">broken</p>',
                     '<p><!-- hidden -->text</p>', '<p><img src="x"></p>',
                     '<p><p>nested</p></p>']:
            with self.subTest(text=text), self.assertRaises(NativeProtocolError):
                message(native_message(text=text), owner_id=OWNER, fan_id=101, history=True)

    def test_queued_and_opened_history_keeps_message_identity_and_price(self):
        for flags in [{'isFromQueue': True}, {'isOpened': True}]:
            raw = {**native_message(10, owner=OWNER, outgoing=True, price=10), **flags}
            event = message(raw, owner_id=OWNER, fan_id=101, history=True)
            self.assertEqual((event.message_id, event.direction, event.price_cents), (10, 'out', 1000))
            self.assertFalse(hasattr(event, 'is_paid'))
            with self.assertRaises(NativeProtocolError):
                message(raw, owner_id=OWNER, fan_id=101)

    def test_bad_history_flags_and_removed_messages_remain_refused(self):
        for flags in [{'isFromQueue': 1}, {'isOpened': 'true'}, {'isDeleted': True},
                      {'isUnsent': True}, {'isTip': True}]:
            with self.subTest(flags=flags), self.assertRaises(NativeProtocolError):
                message({**native_message(10, owner=OWNER, outgoing=True), **flags}, owner_id=OWNER, fan_id=101, history=True)

    def test_large_chat_and_message_pages_use_only_history_budget(self):
        wire = NativeProviderConnector(session())
        records = []
        def read(method, path, body=b'', **options):
            records.append((method, path, options.get('max_response_bytes', MAX_BYTES)))
            if path.endswith('/users/me'): return json.dumps({'id': 7001}).encode()
            if '/chats/101/messages?' in path:
                row = {**native_message(10, owner=7001, outgoing=True), 'isFromQueue': True}
                row['padding'] = 'x' * 70000
            else: row = {'withUser': {'id': 101}, 'padding': 'x' * 70000}
            raw = json.dumps({'list': [row], 'hasMore': False}).encode()
            self.assertGreater(len(raw), MAX_BYTES)
            return raw
        with patch.dict(os.environ, {'ONLYFANS_PROCESS_ROLE': 'worker'}), patch.object(wire, '_signed_request', side_effect=read):
            self.assertEqual(wire.chats(cursor=None, limit=2).fan_ids, [101])
            self.assertEqual(wire.messages(fan_id=101, before_id=None, limit=2).events[0].message_id, 10)
            wire._call('POST', '/api2/v2/chats/101/messages', b'{}')
        self.assertEqual([r[2] for r in records], [MAX_BYTES, MAX_HISTORY_BYTES, MAX_BYTES, MAX_HISTORY_BYTES, MAX_BYTES])
        with self.assertRaises(NativeProtocolError): load(b' ' * (MAX_HISTORY_BYTES + 1), max_bytes=MAX_HISTORY_BYTES)
        with self.assertRaises(NativeProtocolError): load(b' ' * (MAX_BYTES + 1))


if __name__ == '__main__':
    unittest.main()

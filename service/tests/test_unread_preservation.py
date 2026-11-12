import ast
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from unread_preservation import preserve_unread, chat_was_unread, UnreadPreservationError

SERVICE = Path(__file__).resolve().parents[1]

class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'ONLYFANS_UNREAD_JOURNAL_DIR': self.temp.name})
        self.env.start()
        self.calls = []
    def tearDown(self):
        self.env.stop(); self.temp.cleanup()
    def run_read(self, unread=2, read=None, restore=None, fan=7, account='of-test'):
        def snap():
            self.calls.append('snapshot')
            return {'withUser': {'id': fan}, 'unreadMessagesCount': unread}
        def fetch(): self.calls.append('read'); return {'messages': [1]}
        def undo(target): self.calls.append(('restore', target)); return {'success': True}
        return preserve_unread(account_id=account, fan_id=fan, snapshot=snap, read=read or fetch, restore=restore or undo)
    def test_unread_restored_after_read(self):
        self.assertEqual(self.run_read(), {'messages': [1]})
        self.assertEqual(self.calls, ['snapshot', 'read', ('restore', 7)])
        self.assertFalse(list(Path(self.temp.name).glob('*.pending')))
    def test_already_read_stays_read(self):
        self.run_read(unread=0)
        self.assertEqual(self.calls, ['snapshot', 'read'])
    def test_fetch_error_still_restores(self):
        def fetch(): raise ValueError('fetch failed')
        with self.assertRaises(ValueError): self.run_read(read=fetch)
        self.assertEqual(self.calls, ['snapshot', ('restore', 7)])
    def test_restore_failure_persists_then_recovers_before_next_snapshot(self):
        def fail(target): raise OSError('secret proxy url')
        with self.assertRaisesRegex(UnreadPreservationError, 'pending undo retained') as caught:
            self.run_read(restore=fail)
        self.assertNotIn('secret', str(caught.exception))
        self.assertEqual(len(list(Path(self.temp.name).glob('*.pending'))), 1)
        self.calls.clear(); self.run_read(unread=0)
        self.assertEqual(self.calls, [('restore', 7), 'snapshot', 'read'])
    def test_pending_failure_does_not_fetch_again(self):
        def fail(target): return {'success': False}
        with self.assertRaises(UnreadPreservationError): self.run_read(restore=fail)
        self.calls.clear()
        with self.assertRaises(UnreadPreservationError): self.run_read(restore=fail)
        self.assertEqual(self.calls, [])
    def test_unknown_state_refuses_fetch(self):
        for state in (None, {}, {'withUser': {'id': 8}, 'unreadMessagesCount': 2}, {'withUser': {'id': 7}}, {'withUser': {'id': 7}, 'unreadMessagesCount': True}):
            with self.subTest(state=state), self.assertRaises(UnreadPreservationError):
                preserve_unread(account_id='of-test', fan_id=7, snapshot=lambda: state, read=lambda: self.fail('must not fetch'), restore=lambda _: self.fail('must not restore'))
    def test_boolean_state(self):
        self.assertTrue(chat_was_unread({'withUser': {'id': 7}, 'hasUnreadMessages': True}, 7))
        self.assertFalse(chat_was_unread({'withUser': {'id': 7}, 'hasUnreadMessages': False}, 7))
    def test_journal_is_account_and_fan_scoped(self):
        def fail(target): return None
        with self.assertRaises(UnreadPreservationError): self.run_read(restore=fail)
        self.calls.clear(); self.run_read(account='other', unread=0)
        self.assertEqual(self.calls, ['snapshot', 'read'])
        self.assertEqual(len(list(Path(self.temp.name).glob('*.pending'))), 1)
    def test_invalid_scope_refuses_fetch(self):
        for acct, fan in [('../x',7),('valid','../x'),('valid',0)]:
            with self.subTest(acct=acct,fan=fan), self.assertRaises(UnreadPreservationError):
                self.run_read(account=acct,fan=fan)
    def test_legacy_client_wraps_actual_message_get(self):
        source=(SERVICE/'of_client.py').read_text()
        cl=next(n for n in ast.parse(source).body if isinstance(n,ast.ClassDef) and n.name=='OFClient')
        method=next(n for n in cl.body if isinstance(n,ast.FunctionDef) and n.name=='get_messages')
        ns={'Any':object,'API_BASE':'/api2/v2'}
        exec(compile(ast.Module(body=[method],type_ignores=[]),'<legacy>','exec'),ns)
        calls=[]
        fake=SimpleNamespace(account_id='legacy',get_chat=lambda fan:{'withUser':{'id':fan},'unreadMessagesCount':1},get_json=lambda path,**kw:calls.append(('get',path,kw)) or {'list':[]},mark_chat_unread=lambda fan:calls.append(('restore',fan)) or {'success':True})
        self.assertEqual(ns['get_messages'](fake,7,before_id=100), {'list':[]})
        self.assertEqual(calls[0][2]['params']['id'],100)
        self.assertEqual(calls[1],('restore',7))
    def test_native_wrapper_uses_selected_fan_and_keeps_parent_parser(self):
        source=(SERVICE/'onlyfans_runtime/provider_connector.py').read_text()
        cl=next(n for n in ast.parse(source).body if isinstance(n,ast.ClassDef) and n.name=='NativeProviderConnector')
        method=next(n for n in cl.body if isinstance(n,ast.FunctionDef) and n.name=='messages')
        # Keep super()'s real class cell by compiling the actual method in a class.
        wrapper=ast.ClassDef(name='NativeProviderConnector',bases=[ast.Name(id='Parent',ctx=ast.Load())],keywords=[],body=[method],decorator_list=[])
        calls=[]; state={'unread':1}
        class Parent:
            def messages(self,**kw): calls.append(('parent',kw)); state['unread']=0; return 'parsed'
        ns={'Parent':Parent,'API_PATH':'/api2/v2','MAX_HISTORY_BYTES':999999,'load':lambda raw,**kw:json.loads(raw),'Refused':ValueError,'_page_limit':lambda account,fan,limit:limit,'_page_outcome':lambda *a:None,'_id':lambda n: n if type(n) is int and n>0 else (_ for _ in ()).throw(ValueError())}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper],type_ignores=[])),'<native>','exec'),ns)
        obj=ns['NativeProviderConnector']();obj.account_id='native';obj._handle=SimpleNamespace(owner_id=99);obj._ready=lambda:None;obj._limit=lambda n:None;obj.verify_identity=lambda:calls.append('identity')
        def signed(method,path):
            calls.append((method,path))
            if method=='DELETE':state['unread']=1
            return json.dumps({'withUser':{'id':7},'unreadMessagesCount':state['unread']} if method=='GET' else {'success':True})
        obj._signed_request=signed
        obj.restore_unread=lambda fan:json.loads(signed('DELETE','/api2/v2/chats/'+str(fan)+'/mark-as-read'))
        self.assertEqual(obj.messages(fan_id=7,before_id=None,limit=10),'parsed')
        self.assertEqual(calls,['identity',('GET','/api2/v2/chats/7'),('parent',{'fan_id':7,'before_id':None,'limit':10}),('GET','/api2/v2/chats/7'),('DELETE','/api2/v2/chats/7/mark-as-read'),('GET','/api2/v2/chats/7')])
        with self.assertRaises(ValueError):obj.messages(fan_id=99,before_id=None,limit=10)

if __name__ == '__main__': unittest.main()

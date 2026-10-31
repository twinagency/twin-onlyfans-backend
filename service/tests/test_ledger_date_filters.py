"""Provider query contract and safe restart of pre-filter financial scans."""
from datetime import datetime, timedelta
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import verified, TENANT
from test_onlyfans_ledger_pg import transaction, payload
from test_onlyfans_provider_adapters import credential
from db.models import Transaction, Fan
from db.onlyfans_ledger_models import OfLedgerScan, OfLedgerHistory
from onlyfans_runtime import ledger, provider_read_budget
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.store import utcnow


def test_wire_filters_cover_fractional_window_and_projection_keeps_exact_bounds(monkeypatch):
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE', 'worker')
    start=datetime(2026, 10, 1, 0, 0, 0, 123456)
    end=datetime(2026, 10, 2, 23, 59, 59, 654321)
    source=[transaction(id='before', createdAt=(start-timedelta(microseconds=1)).isoformat()+'Z'),
            transaction(id='inside', createdAt=end.isoformat()+'Z'),
            transaction(id='after', createdAt=(end+timedelta(microseconds=1)).isoformat()+'Z')]
    calls=[]
    wire=NativeProviderConnector(credential())
    monkeypatch.setattr(wire, 'verify_identity', lambda: calls.append('identity'))
    def get(method, path):
        assert method=='GET' and calls==['identity']
        q=parse_qs(urlsplit(path).query)
        assert q=={'limit':['20'], 'offset':['0'],
                   'startDate':['2026-10-01 00:00:00'], 'endDate':['2026-10-03 00:00:00']}
        return payload(source)
    monkeypatch.setattr(wire, '_call', get)
    raw=wire.transaction_page(offset=0, start=start, end=end)
    rows,more=ledger.parse_page(raw, owner_id=7001, start=start, end=end)
    assert [r['provider_transaction_id'] for r in rows]==['inside'] and more is False


@pytest.mark.parametrize('history', [False, True])
def test_changed_provider_filter_restarts_cursor_without_erasing_or_double_counting_money(database, monkeypatch, history):
    store,account,_=verified(database,monkeypatch)
    clock=[utcnow()]
    monkeypatch.setattr(ledger,'utcnow',lambda:clock[0])
    monkeypatch.setattr(provider_read_budget,'utcnow',lambda:clock[0])
    source=transaction(id='retained', description='Tip from Synthetic Fan')
    calls=[]
    def page(self, **kw):
        calls.append(kw)
        assert kw['offset']==0 and 'marker' not in kw
        return payload([source], more=len(calls)==1)
    monkeypatch.setattr(NativeProviderConnector,'transaction_page',page)
    assert ledger.process_ledger_pages(store,TENANT,history=history)==1
    model=OfLedgerHistory if history else OfLedgerScan
    with Session(store.engine) as s,s.begin():
        scan=s.get(model,account)
        window=(scan.window_start,scan.window_end)
        old=json.loads(scan.continuation_json)
        old['scope']=hashlib.sha256(ledger.canonical([
            ledger._generation(scan), scan.window_start, scan.window_end]).encode()).hexdigest()
        scan.continuation_json=ledger.canonical(old)
        assert scan.page_offset==1 and old['marker']=='retained'
        assert s.scalar(select(func.count()).select_from(Transaction))==1
    clock[0]+=timedelta(seconds=2)
    assert ledger.process_ledger_pages(store,TENANT,history=history)==1
    with Session(store.engine) as s:
        scan=s.get(model,account)
        assert (scan.window_start,scan.window_end)==window
        assert scan.status=='idle' and ledger.coverage_proven(scan)
        assert s.scalar(select(func.count()).select_from(Transaction))==1
        assert s.get(Fan,(account,101)).lifetime_spend_cents==1000
    store.close()

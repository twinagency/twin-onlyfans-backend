"""Legacy HTML may match visible native text; actual conflicts remain held."""
from dataclasses import replace
from types import SimpleNamespace
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database, seed, event
from db.models import Message
from db.onlyfans_models import OfIngress, OfRuntime, OfWork
from onlyfans_runtime.history_identity import same_message, reconcile_formatting_conflicts
from onlyfans_runtime.store import Refused


@pytest.mark.parametrize("raw", [None, {"id":1,"text":"<p>Hello &amp; welcome<br>friend</p>"}])
def test_legacy_formatting_matches_without_rewriting(raw):
    e=event(body="Hello & welcome\nfriend").normalized()
    from datetime import datetime
    old=SimpleNamespace(body="<p>Hello &amp; welcome<br>friend</p>",direction=e.direction,
        price_cents=0,media_ids="[]",message_id=1,raw_json=json.dumps(raw) if raw else None,
        created_at=datetime.fromisoformat(e.created_at).replace(tzinfo=None))
    assert same_message(old,e)
    for changed in (replace(e,body="Different"),replace(e,direction="out"),
                    replace(e,price_cents=1),replace(e,media_ids=(9,))):
        assert not same_message(old,changed)
    old.raw_json=json.dumps({"body":old.body,"message_id":1})
    assert not same_message(old,e), "literal native markup is not legacy formatting"
    old.raw_json=json.dumps({"id":2,"text":old.body})
    assert not same_message(old,e), "provider evidence belongs to the same message"


def test_formatting_repair_preserves_canonical_controls_and_real_conflicts(database):
    url,engine=database
    store=seed(url,engine)
    e=event(body="Hello & welcome").normalized()
    from datetime import datetime
    stamp=datetime.fromisoformat(e.created_at).replace(tzinfo=None)
    with Session(engine) as s,s.begin():
        s.add(Message(account_id="acc-a",fan_id=101,message_id=1,direction="in",
            body="<p>Hello &amp; welcome</p>",created_at=stamp))
    store.record("tenant-a","acc-a",e,source="catchup")
    store.drain("tenant-a","acc-a")
    with Session(engine) as s,s.begin():
        receipt=s.scalar(select(OfIngress))
        assert receipt.status=="applied"
        # Reproduce the receipt left by the old strict HTML/plain comparison.
        receipt.status,receipt.reason="quarantined","conflicting_message_identity"
        retained=receipt.payload
    real=replace(e,price_cents=100)
    store.record("tenant-a","acc-a",real,source="import")
    store.drain("tenant-a","acc-a")
    result=reconcile_formatting_conflicts(store,"tenant-a","acc-a")
    assert result["examined"]==2 and result["repaired"]==1
    assert reconcile_formatting_conflicts(store,"tenant-a","acc-a")["repaired"]==0
    with pytest.raises(Refused,match="scope"):
        reconcile_formatting_conflicts(store,"other-tenant","acc-a")
    with Session(engine) as s:
        rows=s.scalars(select(OfIngress)).all()
        assert sum(r.status=="quarantined" for r in rows)==1
        repaired=next(r for r in rows if r.status=="applied")
        assert repaired.payload==retained and repaired.reason=="legacy_html_equivalent"
        assert s.get(Message,("acc-a",101,1)).body=="<p>Hello &amp; welcome</p>"
        assert s.get(Message,("acc-a",101,1)).price_cents==0
        assert s.get(OfRuntime,"acc-a").pending_events==0
        assert s.get(OfWork,("acc-a",101)) is None
    store.close()

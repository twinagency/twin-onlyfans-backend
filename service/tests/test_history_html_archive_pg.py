from datetime import datetime
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_onlyfans_launch_history_memory_recovery_pg import ready,history,TENANT,ACCOUNT,FAN
from db.models import Message
from db.onlyfans_models import OfIngress,OfRuntime,OfWork
from onlyfans_runtime.launch_history_memory.recovery import OfHistoryCursor


def test_html_duplicate_advances_archive_without_rewriting_message_or_sending(ready):
    store,recovery,engine=ready
    event=history(8,body='Hello & welcome')
    with Session(engine) as session,session.begin():
        session.add(Message(account_id=ACCOUNT,fan_id=FAN,message_id=8,direction='in',
            body='<p>Hello &amp; welcome</p>',price_cents=0,media_ids='[]',
            created_at=datetime.fromisoformat(event.created_at).replace(tzinfo=None)))
    recovery.request_fan(TENANT,ACCOUNT,FAN)
    claim=recovery.claim(TENANT,ACCOUNT)
    assert recovery.page(TENANT,claim,[event],has_more=False)=='complete'
    with Session(engine) as session:
        assert session.get(Message,(ACCOUNT,FAN,8)).body=='<p>Hello &amp; welcome</p>'
        assert session.get(OfHistoryCursor,(ACCOUNT,FAN)).status=='complete'
        assert session.get(OfRuntime,ACCOUNT).pending_events==0
        assert session.scalar(select(func.count()).select_from(OfIngress).where(OfIngress.status=='quarantined'))==0
        assert session.scalar(select(func.count()).select_from(OfWork))==0

"""Rich history stays canonical text and never becomes a purchase."""
from sqlalchemy import func,select
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database,seed
from onlyfans_native_protocol_fixture import native_message,OWNER
from onlyfans_runtime.native_protocol import message
from db.models import Message,Transaction
from ownership import owned_media_in_session


def test_opened_queued_link_history_does_not_record_payment(database):
    store=seed(*database)
    raw={**native_message(10,outgoing=True,price=10,media=[11],
        text='<p>View <a href="https://synthetic.invalid/private">the update</a>.</p>'),
        'isFromQueue':True,'isOpened':True}
    event=message(raw,owner_id=OWNER,fan_id=101,history=True)
    assert store.record('tenant-a','acc-a',event,source='import')=='accepted'
    assert store.drain('tenant-a','acc-a')==1
    with Session(database[1]) as session:
        row=session.get(Message,('acc-a',101,10))
        assert row.body=='View the update.' and row.price_cents==1000
        # Historical opened state is not payment evidence; preserve unknown.
        assert row.is_paid is None and row.purchased_at is None
        assert session.scalar(select(func.count()).select_from(Transaction))==0
        assert owned_media_in_session(session,'acc-a',101)==set()
    store.close()

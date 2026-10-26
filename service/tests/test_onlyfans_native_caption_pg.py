"""The final paid caption is reply first and entirely covered by the existing guard."""
import pytest
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_onlyfans_media_pg import native, ASK
from test_onlyfans_text_delivery_pg import ACCOUNT,TENANT
from db.models import CatalogItem
from db.onlyfans_delivery_models import OfNativePlan
from onlyfans_runtime.native_planning import SyntheticNativeModel
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from onlyfans_runtime.store import Refused


def test_reply_precedes_exact_description_and_price_on_the_wire(database,monkeypatch):
    store,authority,claim,planner,item=native(database,monkeypatch)
    try:
        with Session(database[1]) as s,s.begin():
            row=s.get(CatalogItem,item)
            row.description_for_ai="A saved set of three synthetic photographs."
            row.pricing_mode,row.price_cents="fixed",1007
        model=SyntheticNativeModel("Glad you asked. How was your afternoon?",sale={"item_id":item,"quote":ASK})
        plan=planner.prepare(TENANT,claim,model)
        identity=planner.queue(TENANT,ACCOUNT,plan)
        transport=SyntheticTextTransport(ACCOUNT)
        assert authority.dispatch(TENANT,ACCOUNT,identity,transport)=="accepted"
        body=transport.calls[0].body
        assert body.startswith("Glad you asked. How was your afternoon?\n\n")
        assert body.index("A saved set of three synthetic photographs.")>body.index("afternoon?")
        assert body.endswith("USD $10.07 to unlock.")
        assert len(model.calls)==1
    finally:store.close()


@pytest.mark.parametrize("description",[
    "Visit https://example.com to get the saved photographs.",
    "Contact me on telegram @synthetic_creator for the photographs.",
])
def test_unsafe_saved_description_is_refused_before_queue_or_send(database,monkeypatch,description):
    store,authority,claim,planner,item=native(database,monkeypatch)
    try:
        with Session(database[1]) as s,s.begin():
            s.get(CatalogItem,item).description_for_ai=description
        model=SyntheticNativeModel("Glad you asked.",sale={"item_id":item,"quote":ASK})
        identity=planner.prepare(TENANT,claim,model)
        with Session(database[1]) as s:
            plan=s.get(OfNativePlan,(ACCOUNT,identity))
            assert plan.status=="refused",plan.reason
            assert plan.body is None and plan.sale_json is None
        with pytest.raises(Refused,match="not ready"):
            planner.queue(TENANT,ACCOUNT,identity)
        assert len(model.calls)==1
    finally:store.close()

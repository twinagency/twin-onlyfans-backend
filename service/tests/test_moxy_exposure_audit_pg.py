"""Full Moxy -> shared TextAuthority regression audit; synthetic content and wire only."""
from copy import deepcopy
from datetime import timedelta
import json
import pytest
from sqlalchemy.orm import Session
from db.models import Message, ContentOffer, CatalogItem, VaultSend, Fan
from db.onlyfans_moxy_models import MoxyEvent
from onlyfans_runtime.store import utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from test_moxy_integrated_pg import setup, decision, run, TENANT, ACCOUNT
from test_onlyfans_foundation_pg import database

CASES = [
    ("fresh", True),
    ("purchased", False),
    ("unpaid_same_catalogue", False),
    ("unpaid_different_catalogue", False),
    ("free_message", False),
    ("free_vaultsend", False),
    ("overlapping_unpaid_bundle", False),
    ("other_fan_history", True),
    ("purchased_after_queue", False),
]

@pytest.mark.parametrize("creator", ["amelia", "cristina"])
@pytest.mark.parametrize("case,allow", CASES)
def test_no_reoffer_full_dispatch(database, monkeypatch, tmp_path, creator, case, allow):
    # Exercise the native connector's extra preflight as well as the shared
    # consume_dispatch gate. The replacement transport remains purely synthetic.
    original_send = SyntheticTextTransport.send
    def send_with_native_preflight(self, dispatch, *, authority, tenant_id):
        authority.check_dispatch_preflight(tenant_id, dispatch, transport_generation=dispatch.policy_revision)
        return original_send(self, dispatch, authority=authority, tenant_id=tenant_id)
    monkeypatch.setattr(SyntheticTextTransport, 'send', send_with_native_preflight)
    engine, store, service, authority, claim, settings = setup(database, monkeypatch, creator=creator)
    try:
        if case == "overlapping_unpaid_bundle":
            settings = deepcopy(settings)
            settings["products"][0]["assets"].append(deepcopy(settings["products"][1]["assets"][0]))
            with Session(engine) as s, s.begin():
                s.get(CatalogItem, settings["catalogue_bindings"]["first"]).media_ids = "[7101,7102]"
            service.configure(TENANT, ACCOUNT, expected_revision=1, mode="on", settings=settings, actor="fixture-operator")
        if case not in {"fresh", "purchased_after_queue"}:
            with Session(engine) as s, s.begin():
                old = utcnow() - timedelta(days=2)
                target_fan = 202 if case == "other_fan_history" else 101
                if target_fan == 202:
                    s.add(Fan(account_id=ACCOUNT, fan_id=202))
                    s.flush()
                if case == "free_vaultsend":
                    s.add(VaultSend(account_id=ACCOUNT, fan_id=101, media_id=7101,
                                    message_id=7000, price_cents=0, was_purchased=False, sent_at=old))
                else:
                    is_free = case == "free_message"
                    is_paid = case == "purchased"
                    s.add(Message(account_id=ACCOUNT, fan_id=target_fan, message_id=7000,
                                  direction="out", body="Historical synthetic media", media_ids="[7101]",
                                  media_count=1, price_cents=0 if is_free else 7500,
                                  is_paid=None if is_free else is_paid,
                                  purchased_at=old if is_paid else None, created_at=old, ingested_at=old))
                    s.flush()
                    if case in {"unpaid_same_catalogue", "unpaid_different_catalogue", "overlapping_unpaid_bundle"}:
                        item_id = settings["catalogue_bindings"]["first"]
                        if case != "unpaid_same_catalogue":
                            item = CatalogItem(account_id=ACCOUNT, kind="image_set", label="Older synthetic catalogue entry",
                                               description_for_ai="Synthetic earlier item", media_ids="[7101]",
                                               preview_media_ids="[]", price_cents=7500, pricing_mode="fixed",
                                               enabled=True, is_free_teaser=False, created_at=old, updated_at=old)
                            s.add(item); s.flush(); item_id = item.id
                        s.add(ContentOffer(account_id=ACCOUNT, fan_id=101, item_id=item_id, mode="ppv",
                                           price_cents=7500, offer_message_id=7000, status="open",
                                           offered_at=old, updated_at=old))
        identity, model = run(service, engine,
            decision("offer", "Here is the synthetic outfit content.", "first", sequence="fixture_level"),
            key="exposure-audit")
        with Session(engine) as s:
            ev = s.get(MoxyEvent, (ACCOUNT, identity))
            prepared, reason = ev.status, ev.reason
        transport = SyntheticTextTransport(ACCOUNT)
        outcome = prepared
        if prepared == "ready":
            intent = service.queue(TENANT, ACCOUNT, identity)
            if case == "purchased_after_queue":
                with Session(engine) as s, s.begin():
                    s.add(VaultSend(account_id=ACCOUNT, fan_id=101, media_id=7101,
                                    message_id=7000, price_cents=7500, was_purchased=True))
            outcome = authority.dispatch(TENANT, ACCOUNT, intent, transport)
        calls = [{"media_ids":list(c.media_ids), "price_cents":c.price_cents} for c in transport.calls]
        record = {"creator":creator,"case":case,"expected_allow":allow,"prepared":prepared,
                  "reason":reason,"dispatch":outcome,"wire_calls":calls}
        with (tmp_path / "exposure-results.jsonl").open("a") as f:
            f.write(json.dumps(record)+"\n")
        print("AUDIT "+json.dumps(record))
        if allow:
            assert outcome == "accepted" and len(calls) == 1, record
        else:
            assert not calls, record
    finally:
        store.close()

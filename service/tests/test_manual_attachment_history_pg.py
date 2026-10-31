"""A manually selected photo does not depend on archive recovery; controls still apply."""
from datetime import timedelta
import json

import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_manual_vault_composer_pg import prepared, prepare, spec, FAN
from test_onlyfans_connections_pg import TENANT
from db.models import Fan, OfConversationState, ContentOffer, Message, VaultSend
from db.onlyfans_models import OfIngress, OfRuntime
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from onlyfans_runtime.store import Refused, utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport


def history_hold(engine, account, *, stale=False):
    with Session(engine) as s, s.begin():
        runtime = s.get(OfRuntime, account)
        s.add(OfIngress(account_id=account, binding_id=runtime.binding_id,
            event_key="synthetic-unrelated-history", fan_id=202, message_id=99,
            source="import", payload='{"synthetic":"retain"}', status="quarantined",
            reason="synthetic_conflict", received_at=utcnow()))
        if stale:
            runtime.recovery_needed = True
            runtime.pending_events = 1
            runtime.last_sync_at = utcnow() - timedelta(hours=2)


@pytest.mark.parametrize("price", [0, 1200])
@pytest.mark.parametrize("stage", ["prepare", "queued", "admitted"])
@pytest.mark.parametrize("stale", [False, True])
def test_manual_attachment_survives_archive_hold_at_every_send_boundary(database, monkeypatch, stage, stale, price):
    store, authority, account = prepared(database, monkeypatch)
    try:
        if stage == "prepare":
            history_hold(database[1], account, stale=stale)
        identity = prepare(authority, account, spec(price), body="")
        assert prepare(authority, account, spec(price), body="") == identity
        if stage == "queued":
            history_hold(database[1], account, stale=stale)
        if stage == "admitted":
            grant = authority.admit(TENANT, account, identity)
            assert not isinstance(grant, str)
            history_hold(database[1], account, stale=stale)
            assert authority.consume_dispatch(TENANT, grant) is not None
        else:
            wire = SyntheticTextTransport(account)
            assert authority.dispatch(TENANT, account, identity, wire) == "accepted"
            assert authority.dispatch(TENANT, account, identity, wire) == "accepted"
            assert len(wire.calls) == 1
            assert wire.calls[0].media_ids == (11, 13) and wire.calls[0].price_cents == price
            with Session(database[1]) as s:
                assert s.scalar(select(func.count()).select_from(ContentOffer)) == (1 if price else 0)
                assert s.scalar(select(func.count()).select_from(Message)) == 1
        with Session(database[1]) as s:
            evidence = s.get(OfIngress, (account, "synthetic-unrelated-history"))
            assert evidence.status == "quarantined" and json.loads(evidence.payload) == {"synthetic": "retain"}
            assert s.get(OfConversationState, (account, FAN)).mode == "human_controlled"
            assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 1
    finally:
        store.close()


@pytest.mark.parametrize("kind", ["text", "automated", "campaign", "engagement"])
def test_exception_does_not_bypass_history_for_other_send_origins(database, monkeypatch, kind):
    store, authority, account = prepared(database, monkeypatch)
    try:
        history_hold(database[1], account)
        with Session(database[1]) as s:
            runtime = s.get(OfRuntime, account)
            policy = s.get(OfDeliveryPolicy, account)
            state = s.get(OfConversationState, (account, FAN))
            fan = s.get(Fan, (account, FAN))
            assert authority._eligibility(s, runtime, policy, state, fan,
                "manual" if kind == "text" else kind, utcnow(),
                manual_attachment=kind != "text") == "history_quarantined"
    finally:
        store.close()


@pytest.mark.parametrize("price", [0, 1200])
@pytest.mark.parametrize("change", ["paused", "muted", "takeover_pending", "unknown", "disabled", "media_disabled"])
def test_manual_photo_still_respects_operator_and_delivery_controls(database, monkeypatch, change, price):
    store, authority, account = prepared(database, monkeypatch)
    try:
        history_hold(database[1], account, stale=True)
        with Session(database[1]) as s, s.begin():
            state = s.get(OfConversationState, (account, FAN))
            policy = s.get(OfDeliveryPolicy, account)
            if change == "paused": state.mode = "paused"
            elif change == "muted": s.get(Fan, (account, FAN)).is_muted = True
            elif change == "takeover_pending": state.takeover_state = "requested"
            elif change == "unknown": state.unresolved_attempt_id = "synthetic-unknown"
            elif change == "disabled": policy.enabled = False
            elif change == "media_disabled": policy.media_enabled = False
        with pytest.raises(Refused):
            prepare(authority, account, spec(price))
        with Session(database[1]) as s:
            assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 0
    finally:
        store.close()


@pytest.mark.parametrize("stage", ["prepare", "queued", "admitted"])
@pytest.mark.parametrize("guard", ["owned", "commerce"])
def test_paid_photo_retains_purchase_checks_at_every_boundary(database, monkeypatch, stage, guard):
    store, authority, account = prepared(database, monkeypatch)
    try:
        history_hold(database[1], account, stale=True)
        identity = None if stage == "prepare" else prepare(authority, account, spec(1200))
        grant = authority.admit(TENANT, account, identity) if stage == "admitted" else None
        if guard == "owned":
            with Session(database[1]) as s, s.begin():
                s.add(VaultSend(account_id=account, fan_id=FAN, media_id=13, was_purchased=True))
        else:
            from onlyfans_runtime import commerce
            monkeypatch.setattr(commerce, "manual_held", lambda *args, **kwargs: True)
        if stage == "prepare":
            with pytest.raises(Refused, match="already owned|commerce evidence"):
                prepare(authority, account, spec(1200))
        elif stage == "queued":
            assert authority.admit(TENANT, account, identity) == "cancelled"
        else:
            assert authority.consume_dispatch(TENANT, grant) is None
        with Session(database[1]) as s:
            assert s.scalar(select(func.count()).select_from(Message)) == 0
            assert s.scalar(select(func.count()).select_from(ContentOffer)) == 0
            if identity is not None:
                assert s.get(OfTextIntent, (account, identity)).wire_started_at is None
    finally:
        store.close()

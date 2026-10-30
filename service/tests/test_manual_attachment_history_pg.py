"""A chosen free photo does not depend on archive recovery; controls still apply."""
from datetime import timedelta
import json

import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_manual_vault_composer_pg import prepared, prepare, spec, FAN
from test_onlyfans_connections_pg import TENANT
from db.models import Fan, OfConversationState, ContentOffer, Message
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


@pytest.mark.parametrize("stage", ["prepare", "queued", "admitted"])
@pytest.mark.parametrize("stale", [False, True])
def test_free_attachment_survives_archive_hold_at_every_send_boundary(database, monkeypatch, stage, stale):
    store, authority, account = prepared(database, monkeypatch)
    try:
        if stage == "prepare":
            history_hold(database[1], account, stale=stale)
        identity = prepare(authority, account, spec(0), body="")
        assert prepare(authority, account, spec(0), body="") == identity
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
            assert wire.calls[0].media_ids == (11, 13) and wire.calls[0].price_cents == 0
            with Session(database[1]) as s:
                assert s.scalar(select(func.count()).select_from(ContentOffer)) == 0
                assert s.scalar(select(func.count()).select_from(Message)) == 1
        with Session(database[1]) as s:
            evidence = s.get(OfIngress, (account, "synthetic-unrelated-history"))
            assert evidence.status == "quarantined" and json.loads(evidence.payload) == {"synthetic": "retain"}
            assert s.get(OfConversationState, (account, FAN)).mode == "human_controlled"
            assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 1
    finally:
        store.close()


@pytest.mark.parametrize("kind", ["paid", "text", "automated", "campaign", "engagement"])
def test_exception_does_not_bypass_history_for_other_send_origins(database, monkeypatch, kind):
    store, authority, account = prepared(database, monkeypatch)
    try:
        history_hold(database[1], account)
        if kind == "paid":
            with pytest.raises(Refused, match="history_quarantined"):
                prepare(authority, account, spec(1200))
        else:
            with Session(database[1]) as s:
                runtime = s.get(OfRuntime, account)
                policy = s.get(OfDeliveryPolicy, account)
                state = s.get(OfConversationState, (account, FAN))
                fan = s.get(Fan, (account, FAN))
                assert authority._eligibility(s, runtime, policy, state, fan,
                    "manual" if kind == "text" else kind, utcnow(),
                    free_manual_attachment=kind != "text") == "history_quarantined"
    finally:
        store.close()


@pytest.mark.parametrize("change", ["paused", "muted", "takeover_pending", "unknown", "disabled", "media_disabled"])
def test_free_photo_still_respects_operator_and_delivery_controls(database, monkeypatch, change):
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
            prepare(authority, account, spec(0))
        with Session(database[1]) as s:
            assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 0
    finally:
        store.close()

"""W7 ci4-send red test (REVIEW-ONLY lane): D429 on the native/Fastt send path.

Rule (D429, c9ff2c98 / handoff section 8): a 429 on SEND is pacing evidence, never proof that the send did not
happen. Delivery must stay `unknown` (reservation kept, fan held) and must never be retried automatically.

At 374c536e `TextAuthority.dispatch` turns any `ProviderThrottled` (raised by `fastt_manual.post_operator_once`
AFTER the POST reached OnlyFans) into a synthesized `NotAccepted("fastt-rate-limited:")` -> `not_accepted`, and
Moxy then re-dispatches the same reply under a new command key (moxy_replacement/runtime.py `provider_rate_limit_retry`).
Here the 429 is raised from `on_send`, i.e. after `consume_dispatch` started the wire, exactly like the real Fastt
transport. Synthetic data only; real PostgreSQL via ONLYFANS_TEST_PG_URL.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401
from test_moxy_integrated_pg import setup, run, decision, model
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT
from test_moxy_provider_recovery_pg import expire_cooldown
from db.onlyfans_delivery_models import OfTextIntent
from onlyfans_runtime.text_delivery import SyntheticTextTransport, ProviderThrottled


def _wire_then_429(dispatch):
    raise ProviderThrottled(retry_after=1)


def test_w7_ci4_send_429_after_wire_start_stays_unknown(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    identity, _ = run(service, engine, decision())
    first = service.queue(TENANT, ACCOUNT, identity)
    authority.dispatch(TENANT, ACCOUNT, first, SyntheticTextTransport(ACCOUNT, on_send=_wire_then_429))
    with Session(engine) as s:
        row = s.get(OfTextIntent, (ACCOUNT, first))
        assert row.status == 'unknown', (
            f'D429: a 429 after the wire started proves nothing; got status={row.status!r} '
            f'evidence_ref={row.evidence_ref!r} (must stay unknown, reservation held)')
        assert row.reservation_held
    store.close()


def test_w7_ci4_send_429_after_wire_start_is_never_auto_resent(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    identity, _ = run(service, engine, decision())
    first = service.queue(TENANT, ACCOUNT, identity)
    authority.dispatch(TENANT, ACCOUNT, first, SyntheticTextTransport(ACCOUNT, on_send=_wire_then_429))
    expire_cooldown(engine)
    service.step(TENANT, ACCOUNT, model(decision()))
    with Session(engine) as s:
        intents = s.scalars(select(OfTextIntent).where(OfTextIntent.moxy_event_id == identity)).all()
        assert len(intents) == 1, (
            f'D429: Moxy prepared {len(intents)} intents for one reply after a post-wire 429 '
            f'(auto re-send of a possibly delivered message)')
    store.close()


def test_w7cs01_native_rate_limit_error_after_wire_is_unknown_and_cools_down(database, monkeypatch):
    # Opus-final W7CS-01 minor: NativeRateLimitError (no status_code) must count as throttled -> cooldown.
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    from onlyfans_runtime.provider_connector import NativeRateLimitError
    from datetime import timedelta
    from onlyfans_runtime.store import utcnow

    def _native_429(dispatch):
        raise NativeRateLimitError(retry_after=120)

    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    identity, _ = run(service, engine, decision())
    first = service.queue(TENANT, ACCOUNT, identity)
    authority.dispatch(TENANT, ACCOUNT, first, SyntheticTextTransport(ACCOUNT, on_send=_native_429))
    with Session(engine) as s:
        assert s.get(OfTextIntent, (ACCOUNT, first)).status == 'unknown'
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        until = policy.provider_cooldown_until or policy.next_dispatch_at  # concurrent vs serial admission
        assert until is not None and until.replace(tzinfo=None) > utcnow().replace(tzinfo=None) + timedelta(seconds=60), until
    store.close()

"""W3CEA-07 (= W3RCB-08; REVIEW inv. 9: unknown ownership fails closed).

Ported from 1398269d (hit ImportError on `history_support`; head has no history_ownership/ownership_gate).
A FansAPI account with no history import, one fan, a paid card. While that fan has UNRESOLVED (pending/held)
FansAPI unlock or transaction evidence we cannot say what he owns, so a new paid card must be refused on:
  (a) manual paid path  -> onlyfans_runtime.media_delivery.assert_unowned(..., manual=True)
  (b) commercial path   -> conversation_engine.commerce._unowned
Resolved evidence (applied / dismissed), no evidence, or another fan's evidence does not block.
Real PostgreSQL (throwaway); no network, no sends. Synthetic ids only.
"""
import pytest
from sqlalchemy.orm import Session

from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_fans_intake_models import OfFansEvidence
from onlyfans_runtime.media_delivery import catalog_terms
from onlyfans_runtime.store import Refused, utcnow
from test_engine_catalogue_pg import setup as engine_setup
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN

CARD, OTHER_FAN = 997001, 202


def manual_gate(s, terms):
    from onlyfans_runtime.media_delivery import assert_unowned
    assert_unowned(s, ACCOUNT, FAN, terms, manual=True)


def commercial_gate(s, terms):
    from conversation_engine.commerce import _unowned
    _unowned(s, ACCOUNT, FAN, terms)


def world(database):
    store, authority, engine, model, red, blue = engine_setup(database)
    with Session(database[1]) as s, s.begin():
        s.get(OfDeliveryPolicy, ACCOUNT).transport_kind = 'fansapi'     # real-provider account, no history import
        terms = catalog_terms(s, ACCOUNT, blue, lock=False)
    return store, terms


def evidence(database, kind, status, fan_id=FAN):
    now = utcnow()
    with Session(database[1]) as s, s.begin():
        s.add(OfFansEvidence(account_id=ACCOUNT, evidence_id='w3cea-%s-%s' % (kind, status), kind=kind,
            dedupe_key='%s:w3cea:%s' % (kind, status), source='webhook', fan_id=fan_id,
            message_id=CARD if kind == 'unlock' else None,
            provider_transaction_id='tx-w3cea' if kind == 'transaction' else None, amount_cents=1200,
            occurred_at=now, status=status, raw_sha256='a' * 64, created_at=now, updated_at=now))


GATES = pytest.mark.parametrize('gate', [manual_gate, commercial_gate], ids=['a_manual', 'b_commercial'])
KINDS = pytest.mark.parametrize('kind', ['unlock', 'transaction'])


@GATES
@KINDS
@pytest.mark.parametrize('status', ['pending', 'held'])
def test_unresolved_money_evidence_refuses_a_new_paid_card_for_that_fan(database, gate, kind, status, request):
    if (kind, status) == ('transaction', 'pending'):
        # Open conflict, not loosened: test_fans_intake_pg asserts an unlinked (pending) transaction must NOT freeze
        # the fan, and test_engine_real_worker_pg sells a second card beside one. Needs an owner decision.
        request.applymarker(pytest.mark.xfail(strict=True, raises=pytest.fail.Exception,
            reason='W3CEA-07 pending transaction vs payment_evidence design (unlinked money does not freeze)'))
    store, terms = world(database)
    try:
        with Session(database[1]) as s:
            gate(s, terms)                                              # control: without evidence the card sells
        evidence(database, kind, status)
        with Session(database[1]) as s:
            with pytest.raises(Refused):
                gate(s, terms)
    finally:
        store.close()


@GATES
@KINDS
@pytest.mark.parametrize('status', ['applied', 'dismissed'])
def test_resolved_money_evidence_does_not_block(database, gate, kind, status):
    store, terms = world(database)
    try:
        evidence(database, kind, status)
        with Session(database[1]) as s:
            gate(s, terms)
    finally:
        store.close()


@GATES
def test_no_evidence_or_another_fans_pending_evidence_still_sells(database, gate):
    store, terms = world(database)
    try:
        with Session(database[1]) as s:
            gate(s, terms)                                              # no evidence at all
        evidence(database, 'unlock', 'pending', fan_id=OTHER_FAN)
        evidence(database, 'transaction', 'pending', fan_id=OTHER_FAN)
        with Session(database[1]) as s:
            gate(s, terms)                                              # fan-scoped, not account-wide
    finally:
        store.close()

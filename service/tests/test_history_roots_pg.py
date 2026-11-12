"""History roots H2/H3: ownership never reconciles money by counting, and a card opened LATER still proves its access.

H2  A provider transaction carries no message id. It is explained only by an actual identity (a reconciliation that names
    the opened message) or by a person's reconciliation (dismissed). Counting payments against cards, matching amounts or
    timing never explains it: an unrelated opened card must not make another product "clear_as_of".
H3  A card first seen unopened / unknown and observed opened later is the one legitimate fact transition. The later
    observation is a different wire row (different raw hash) of the SAME immutable card terms. It must keep external
    access proven, in either arrival order, without an engine grant, a settlement, or a rewritten original wire row.

Synthetic provider, model and PostgreSQL only.
"""
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from commercial_worker_fixture import FAN_A, build_world
from history_support import card, fan_message, tx_row
from test_commercial_preparation_pg import TENANT, config
from test_commercial_sale_rules_pg import catalogue
from test_history_ownership_pg import world as ownership_world
from test_onlyfans_foundation_pg import database  # noqa: F401
from conversation_engine.history_ownership import derive
from conversation_engine.writer_context import set_active_profiles
from db.onlyfans_engine_models import OfEngineAccessEvidence, OfEngineOfferGrant
from db.onlyfans_fans_intake_models import OfFansEvidence
from db.onlyfans_history_models import OfFansMessageFact
from fans_api_adapter.events import FansEventIntake
from model_transport.pilot_supervised import build_supervised_pilot

MID = 7102
FAN_OTHER = 402


def history_of(first_opened, media=(31,), price=12, previews=()):
    def history(cfg):
        return {FAN_A: [fan_message(7101, FAN_A, 'hi'), card(MID, cfg, list(media), price, previews=list(previews), opened=first_opened)]}
    return history


def fact_and_unlock(database, w, mid=MID):
    with Session(database[1]) as s:
        fact = s.get(OfFansMessageFact, (w.account, FAN_A, mid))
        rows = list(s.scalars(select(OfFansEvidence).where(OfFansEvidence.account_id == w.account,
                                                           OfFansEvidence.kind == 'unlock', OfFansEvidence.message_id == mid)))
        s.expunge_all()
        return fact, rows


def states(database, w, media, fan=FAN_A):
    with Session(database[1]) as s:
        return {m: (v.state, v.reasons) for m, v in derive(s, w.account, fan, media).items()}


def reopen(w, *, media=(31,), price=12, previews=(), changed=None):
    """The same card observed again through history, now opened (a different raw row of the same terms)."""
    intake = FansEventIntake(w.store)
    row = card(MID, w.cfg, list(media), price, previews=list(previews), opened=True)
    if changed is not None:
        row['changedAt'] = changed
    intake.ingest_history_item(TENANT, w.account, FAN_A, row)
    intake.drive(w.account, FAN_A)
    return intake


def not_proof(rows):
    return all((r.status, r.reason) != ('paired', 'external_access_proven') for r in rows)


def assert_access_only(database, w):
    """External access is neither an engine grant, a settlement nor a financial link."""
    with Session(database[1]) as s:
        row = s.scalar(select(OfFansEvidence).where(OfFansEvidence.account_id == w.account, OfFansEvidence.message_id == MID,
                                                    OfFansEvidence.kind == 'unlock'))
        assert (row.status, row.reason) == ('paired', 'external_access_proven')
        assert row.intent_id is None and row.grant_id is None and row.peer_id is None
        assert s.scalar(select(func.count()).select_from(OfEngineAccessEvidence).where(OfEngineAccessEvidence.account_id == w.account)) == 0
        assert s.scalar(select(func.count()).select_from(OfEngineOfferGrant).where(OfEngineOfferGrant.account_id == w.account,
                                                                                    OfEngineOfferGrant.fan_id == FAN_A,
                                                                                    OfEngineOfferGrant.used_intent_id.is_not(None))) == 0
        assert s.scalar(select(func.count()).select_from(OfFansEvidence).where(OfFansEvidence.account_id == w.account,
                                                                                OfFansEvidence.kind == 'transaction')) == 0


# ---------------------------------------------------------------------------------------------------- H3: later opening
@pytest.mark.parametrize('first_opened', [True, False, None], ids=['already-opened-control', 'unopened-then-opened', 'unknown-then-opened'])
def test_later_opening_preserves_external_access_and_a_distinct_authorized_sale(database, tmp_path, monkeypatch, first_opened):
    w = build_world(database, tmp_path, monkeypatch, i=4, history=history_of(first_opened))
    try:
        catalogue(w)
        w.launch()
        reopen(w)
        fact, rows = fact_and_unlock(database, w)
        assert fact.is_opened is True and not fact.conflicted and len(rows) == 1
        assert states(database, w, [31, 41])[31][0] == 'owned'
        assert_access_only(database, w)
        w.converse('show me the beach photo', FAN_A)
        assert not w.cards(), 'the owned photo is never sold again'
        w.converse('send me the pool video', FAN_A)
        assert [p['body']['mediaFiles'] for p in w.cards()] == [['41']], 'a different authorized product is sold exactly once'
        assert [p['body']['price'] for p in w.cards()] == [25.0]
    finally:
        set_active_profiles(None)
        w.store.close()


def test_the_first_raw_row_and_its_terms_are_retained_not_rewritten(database, tmp_path, monkeypatch):
    w = build_world(database, tmp_path, monkeypatch, i=4, history=history_of(False))
    try:
        catalogue(w)
        w.launch()
        before, _ = fact_and_unlock(database, w)
        reopen(w)
        after, rows = fact_and_unlock(database, w)
        assert after.raw_sha256 == before.raw_sha256, 'the original wire evidence is never rewritten'
        assert (after.price_cents, after.media_ids_json, after.preview_ids_json) == (before.price_cents, before.media_ids_json, before.preview_ids_json)
        assert rows[0].raw_sha256 != before.raw_sha256, 'the later observation keeps its own authentic raw hash'
        assert (rows[0].status, rows[0].reason) == ('paired', 'external_access_proven')
    finally:
        set_active_profiles(None)
        w.store.close()


@pytest.mark.parametrize('changed', [dict(previews=[31]), dict(price=13), dict(media=(31, 32))], ids=['previews', 'price', 'media'])
def test_a_later_opening_whose_terms_differ_is_a_conflict_never_access(database, tmp_path, monkeypatch, changed):
    w = build_world(database, tmp_path, monkeypatch, i=4, history=history_of(False))
    try:
        catalogue(w)
        w.launch()
        reopen(w, **changed)
        fact, rows = fact_and_unlock(database, w)
        assert fact.conflicted
        assert not_proof(rows)
        assert states(database, w, [31])[31][0] == 'unknown'
        w.converse('send me the pool video', FAN_A)
        assert not w.cards(), 'conflicting card terms hold commercial sends for the fan'
    finally:
        set_active_profiles(None)
        w.store.close()


def test_a_late_conflict_after_a_proven_opening_withdraws_the_proof(database, tmp_path, monkeypatch):
    w = build_world(database, tmp_path, monkeypatch, i=4, history=history_of(False))
    try:
        catalogue(w)
        w.launch()
        reopen(w)
        assert_access_only(database, w)
        intake = FansEventIntake(w.store)
        intake.ingest_history_item(TENANT, w.account, FAN_A, card(MID, w.cfg, [31], 12, previews=[31], opened=False))
        intake.drive(w.account, FAN_A)
        fact, rows = fact_and_unlock(database, w)
        assert fact.conflicted
        assert not_proof(rows)
        assert states(database, w, [31])[31][0] == 'unknown'
        w.converse('send me the pool video', FAN_A)
        assert not w.cards(), 'conflicting card evidence holds the fan; it is never resolved by guess'
    finally:
        set_active_profiles(None)
        w.store.close()


def test_a_late_unopened_snapshot_never_closes_an_opening_and_duplicates_change_nothing(database, tmp_path, monkeypatch):
    w = build_world(database, tmp_path, monkeypatch, i=4, history=history_of(False))
    try:
        catalogue(w)
        w.launch()
        reopen(w)
        reopen(w)                                           # duplicate opened observation
        reopen(w, changed='2026-10-03T09:00:00Z')           # same card, a later changedAt
        stale = FansEventIntake(w.store)
        stale.ingest_history_item(TENANT, w.account, FAN_A, card(MID, w.cfg, [31], 12, previews=[], opened=False))
        stale.drive(w.account, FAN_A)
        fact, rows = fact_and_unlock(database, w)
        assert fact.is_opened is True and not fact.conflicted and len(rows) == 1
        assert_access_only(database, w)
        with Session(database[1]) as s:
            assert s.scalar(select(func.count()).select_from(OfFansMessageFact).where(OfFansMessageFact.account_id == w.account)) == 1
    finally:
        set_active_profiles(None)
        w.store.close()


def test_restart_keeps_the_proof_and_sells_one_distinct_card(database, tmp_path, monkeypatch):
    w = build_world(database, tmp_path, monkeypatch, i=4, history=history_of(None))
    try:
        catalogue(w)
        w.launch()
        reopen(w)
        restarted = build_supervised_pilot(w.store, w.env, model_transport=w.model_transport, fans_transport=w.fans_mock.transport)
        restarted.tick()
        FansEventIntake(w.store).drive(w.account, FAN_A)    # a fresh intake over the same durable rows
        assert_access_only(database, w)
        w.converse('send me the pool video', FAN_A)
        assert [p['body']['mediaFiles'] for p in w.cards()] == [['41']]
        restarted.tick()
        assert len(w.cards()) == 1, 'no replay after restart'
    finally:
        set_active_profiles(None)
        w.store.close()


@pytest.mark.parametrize('first_opened', [False, None], ids=['unopened', 'unknown'])
def test_a_webhook_unlock_before_the_card_is_retained_and_proves_access_when_the_card_arrives(database, tmp_path, monkeypatch, first_opened):
    """Arrival order B: the authenticated unlock webhook is seen BEFORE the card's first-seen fact exists."""
    w = build_world(database, tmp_path, monkeypatch, i=4, history=lambda cfg: {FAN_A: [fan_message(7101, FAN_A, 'hi')]})
    try:
        catalogue(w)
        w.launch()
        w.deliver(w.unlock_event(MID, 12, FAN_A), 'early-unlock-7102')
        fact, rows = fact_and_unlock(database, w)
        assert fact is None and [r.status for r in rows] == ['pending'], 'nothing is proven before the card is known'
        intake = FansEventIntake(w.store)
        intake.ingest_history_item(TENANT, w.account, FAN_A, card(MID, w.cfg, [31], 12, previews=[], opened=first_opened))
        w.pilot.tick()                       # the real worker projects the imported card, then re-drives the fan's evidence
        fact, rows = fact_and_unlock(database, w)
        assert fact.is_opened is True and not fact.conflicted, 'the retained authenticated unlock is the opening observation'
        assert [(r.source, r.status, r.reason) for r in rows] == [('webhook', 'paired', 'external_access_proven')]
        assert states(database, w, [31, 41])[31][0] == 'owned'
        w.converse('send me the pool video', FAN_A)
        assert [p['body']['mediaFiles'] for p in w.cards()] == [['41']]
    finally:
        set_active_profiles(None)
        w.store.close()


def test_a_webhook_unlock_after_an_unopened_card_proves_access(database, tmp_path, monkeypatch):
    """Arrival order A: the card is seen unopened, the authenticated unlock arrives later."""
    w = build_world(database, tmp_path, monkeypatch, i=4, history=history_of(False))
    try:
        catalogue(w)
        w.launch()
        w.deliver(w.unlock_event(MID, 12, FAN_A), 'late-unlock-7102')
        FansEventIntake(w.store).drive(w.account, FAN_A)
        fact, rows = fact_and_unlock(database, w)
        assert fact.is_opened is True and not fact.conflicted
        assert [(r.source, r.status, r.reason) for r in rows] == [('webhook', 'paired', 'external_access_proven')]
        assert states(database, w, [31, 41])[31][0] == 'owned'
    finally:
        set_active_profiles(None)
        w.store.close()


def test_a_webhook_unlock_with_another_amount_is_never_proof(database, tmp_path, monkeypatch):
    w = build_world(database, tmp_path, monkeypatch, i=4, history=history_of(False))
    try:
        catalogue(w)
        w.launch()
        w.deliver(w.unlock_event(MID, 99, FAN_A), 'wrong-amount-unlock')
        FansEventIntake(w.store).drive(w.account, FAN_A)
        _, rows = fact_and_unlock(database, w)
        assert rows and not_proof(rows)
    finally:
        set_active_profiles(None)
        w.store.close()


def test_ordinary_chat_after_a_later_opening_is_unaffected(database, tmp_path, monkeypatch):
    w = build_world(database, tmp_path, monkeypatch, i=4, history=history_of(False))
    try:
        catalogue(w)
        w.launch()
        reopen(w)
        staged, tick = w.converse('How is your day?', FAN_A)
        assert staged.get('outcome') == 'awaiting_approval' and tick is not None, 'the evidence guard does not freeze ordinary chat'
        assert len(w.fans_mock.posts) == 1 and not w.cards()
    finally:
        set_active_profiles(None)
        w.store.close()


def test_a_later_opened_bundle_member_excludes_the_overlapping_bundle_but_not_a_distinct_photo(database, tmp_path, monkeypatch):
    # media 52 is the video inside the weekend bundle (51, 52, 53)
    w = build_world(database, tmp_path, monkeypatch, i=4, history=history_of(False, media=(52,), price=25))
    try:
        catalogue(w)
        w.launch()
        reopen(w, media=(52,), price=25)
        assert states(database, w, [31, 51, 52])[52][0] == 'owned'
        w.converse('I want the weekend bundle', FAN_A)
        assert not w.cards(), 'a bundle overlapping an owned item is excluded'
        w.converse('show me the beach photo', FAN_A)
        assert [p['body']['mediaFiles'] for p in w.cards()] == [['31']], 'the distinct single is still sold once'
    finally:
        set_active_profiles(None)
        w.store.close()


# ---------------------------------------------------------------------------------------------------- H2: no count heuristic
@pytest.mark.parametrize('unopened_card', ['none', 'known-previews', 'unknown-previews'])
def test_an_unopened_card_cannot_explain_an_unlinked_transaction(database, unopened_card):
    cfg = config(1)
    messages = [] if unopened_card == 'none' else [card(9501, cfg, [191], 15, previews=[] if unopened_card == 'known-previews' else None, opened=False)]
    store, account, cfg, wire = ownership_world(database, 1, messages=messages, tx=[tx_row('h2-unlinked-t1', FAN_A, 15)])
    try:
        with Session(database[1]) as s:
            got = derive(s, account, FAN_A, [191, 193])
            assert got[193].state == 'unknown' and 'payment_without_unlock' in got[193].reasons
    finally:
        store.close()


def test_an_opened_card_does_not_numerically_explain_an_unlinked_payment(database):
    cfg = config(1)
    store, account, cfg, wire = ownership_world(database, 1, messages=[card(9502, cfg, [191, 192], 15, previews=[191], opened=True)],
                                                tx=[tx_row('h2-t1', FAN_A, 15)])
    try:
        with Session(database[1]) as s:
            got = derive(s, account, FAN_A, [191, 192, 193])
            assert got[192].state == 'owned', 'the opened card itself still proves its paid media'
            assert got[193].state == 'unknown' and got[193].reasons == ('payment_without_unlock',), 'same price and fan are a coincidence, not a link'
    finally:
        store.close()


def test_a_payment_that_names_the_opened_message_is_explained_and_a_reconciled_one_is_cleared(database):
    cfg = config(1)
    store, account, cfg, wire = ownership_world(database, 1, messages=[card(9503, cfg, [191, 192], 15, previews=[191], opened=True)],
                                                tx=[tx_row('h2-t1', FAN_A, 15), tx_row('h2-t2', FAN_A, 20)])
    try:
        with database[1].begin() as c:   # an actual identity: the reconciliation records which message the payment is for
            c.execute(text("UPDATE of_fans_evidence SET message_id=9503, status='applied' WHERE account_id=:a AND provider_transaction_id='h2-t1'"), {'a': account})
        with Session(database[1]) as s:
            assert derive(s, account, FAN_A, [193])[193].state == 'unknown', 'the second payment is still unexplained'
        with database[1].begin() as c:   # a person reconciles it
            c.execute(text("UPDATE of_fans_evidence SET status='dismissed', resolved_by='op-session:resolved' WHERE account_id=:a AND provider_transaction_id='h2-t2'"), {'a': account})
        with Session(database[1]) as s:
            assert derive(s, account, FAN_A, [193])[193].state == 'clear_as_of'
    finally:
        store.close()


def test_a_payment_naming_a_message_that_is_not_an_opened_card_stays_unknown(database):
    cfg = config(1)
    store, account, cfg, wire = ownership_world(database, 1, messages=[card(9504, cfg, [191, 192], 15, previews=[191], opened=True)],
                                                tx=[tx_row('h2-t1', FAN_A, 15)])
    try:
        with database[1].begin() as c:
            c.execute(text("UPDATE of_fans_evidence SET message_id=424242, status='applied' WHERE account_id=:a AND provider_transaction_id='h2-t1'"), {'a': account})
        with Session(database[1]) as s:
            assert derive(s, account, FAN_A, [193])[193].state == 'unknown'
    finally:
        store.close()


def test_a_signed_unrelated_transaction_is_not_explained_by_an_opened_card(database, tmp_path, monkeypatch):
    w = build_world(database, tmp_path, monkeypatch, i=4, history=history_of(True))
    try:
        catalogue(w)
        w.launch()
        w.deliver({'event': 'transactions.new', 'account_id': w.provider, 'payload': tx_row('unrelated-77-dollar-payment', FAN_A, 77.00)},
                  event_key='unrelated-transaction')
        with Session(database[1]) as s:
            row = s.scalar(select(OfFansEvidence).where(OfFansEvidence.account_id == w.account, OfFansEvidence.kind == 'transaction'))
            got = derive(s, w.account, FAN_A, [31, 41])
            from conversation_engine.pilot_scope import delivery_block
            hold = delivery_block(s, w.store._runtime(s, TENANT, w.account, lock=False), FAN_A, commercial=True)
        assert (row.status, row.reason) == ('pending', 'unlinked_transaction') and row.amount_cents == 7700
        assert row.message_id is None and row.intent_id is None
        assert hold == 'pilot_evidence_pending'
        assert got[31].state == 'owned'
        assert got[41].state == 'unknown' and 'payment_without_unlock' in got[41].reasons
    finally:
        set_active_profiles(None)
        w.store.close()

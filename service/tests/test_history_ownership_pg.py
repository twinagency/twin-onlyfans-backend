"""Read-only ownership derived from history: owned / unknown / clear_as_of, and never "unowned".

Real PostgreSQL, mocked wire. A missing record is not proof of non-ownership; the latest-1,000 vault window is a
catalogue selection, not ownership evidence; unknown ownership blocks only a new paid card of that product to that
fan. Counterexamples first: before this change an imported opened card left its media looking unowned.
"""
import inspect
import json
import re
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_commercial_preparation_pg import TENANT, ACTOR, config
from history_support import (Provider, _item, card, cb_page, cb_row, chat_page, creator_message, fan_message, key, next_url, page,
                             prepared, run, tx_page, tx_row)
from test_history_import_pg import cursor_of, drive, hist, json_cursor, source_of
from test_history_coverage_pg import at_end, read_everything
from onlyfans_runtime.store import Refused, utcnow

FAN, OTHER = 401, 402


def own():
    from conversation_engine import history_ownership
    return history_ownership


def world(database, i=1, *, messages=(), tx=(), cb=(), other=(), fans=(FAN,), webhooks=True):
    """A prepared account whose listed fans have been read to the provider's end."""
    store, account, cfg = prepared(database, i, webhooks=webhooks)
    wire = Provider(cfg)
    wire.chats[FAN] = [chat_page(list(messages))]
    wire.chats[OTHER] = [chat_page(list(other))]
    wire.tx, wire.cb = [tx_page(list(tx))], [cb_page(list(cb))]
    read_everything(store, database, account, wire, fans)
    return store, account, cfg, wire


def derive(database, account, fan, media):
    with Session(database[1]) as s:
        return own().derive(s, account, fan, media)


def states(result):
    return {m: v.state for m, v in result.items()}


def reconcile_transactions(database, account):
    """A person reconciles every imported payment (the operator route marks it dismissed). H2: a payment is never explained by
    counting cards, matching an amount or timing, so a test that wants a fan with imported history to be clear says so here."""
    with database[1].begin() as c:
        c.execute(text("UPDATE of_fans_evidence SET status='dismissed', reason='operator_reconciled', resolved_by='op-session:resolved' "
                       "WHERE account_id=:a AND kind='transaction' AND status='pending'"), {"a": account})


def terms(media, previews=(), price=1500):
    return {"media_ids": list(media), "preview_ids": list(previews), "price_cents": price}


def opened(cfg, mid, media, previews, price=15, **extra):
    return card(mid, cfg, media, price, previews=previews, opened=True, **extra)


# ---- the counterexample: history purchases were invisible to ownership -----------------------------------

def test_an_imported_opened_card_makes_its_paid_media_owned_and_excludes_any_bundle_containing_it(database):
    cfg = config(1)
    store, account, cfg, wire = world(database, 1, messages=[opened(cfg, 9002, [111, 112, 113], [111])], tx=[tx_row("t1", FAN)])
    try:
        reconcile_transactions(database, account)
        got = derive(database, account, FAN, [111, 112, 113, 114])
        assert states(got) == {111: "clear_as_of", 112: "owned", 113: "owned", 114: "clear_as_of"}
        assert got[112].evidence == ("message:9002",) and got[111].as_of is not None and got[112].as_of is None
        with Session(database[1]) as s:
            single = own().evaluate_card(s, account, FAN, terms([112]))
            assert single["decision"] == "owned" and single["owned"] == [112]
            assert own().evaluate_card(s, account, FAN, terms([112, 114]))["decision"] == "owned", "a bundle is excluded when any paid asset is owned"
            assert own().evaluate_card(s, account, FAN, terms([111, 114], previews=[111]))["decision"] == "ok", "a shown free preview is not a purchase"
            fresh = own().evaluate_card(s, account, FAN, terms([114, 115]))
            assert fresh["decision"] == "ok" and set(fresh["states"]) == {114, 115}
        # the legacy final-dispatch fence sees the imported purchase too (it did not before)
        from onlyfans_runtime.media_delivery import assert_unowned
        with Session(database[1]) as s:
            with pytest.raises(Refused, match="owned"):
                assert_unowned(s, account, FAN, terms([112, 114]))
            assert_unowned(s, account, FAN, terms([114, 115]))
    finally:
        store.close()


def test_missing_previews_make_that_cards_media_unknown_and_block_only_that_fan_and_product(database):
    cfg = config(1)
    store, account, cfg, wire = world(database, 1, messages=[card(9102, cfg, [121, 122], 15, opened=True)], fans=(FAN, OTHER))
    try:
        got = derive(database, account, FAN, [121, 122, 123])
        assert states(got) == {121: "unknown", 122: "unknown", 123: "clear_as_of"}
        assert got[121].reasons == ("previews_unknown",) and got[121].evidence == ("message:9102",)
        with Session(database[1]) as s:
            assert own().evaluate_card(s, account, FAN, terms([121]))["decision"] == "unknown"
            assert own().evaluate_card(s, account, FAN, terms([123]))["decision"] == "ok", "a different product is not blocked"
            assert own().evaluate_card(s, account, OTHER, terms([121]))["decision"] == "ok", "a different fan is not blocked"
            with pytest.raises(Refused, match="ownership coverage unknown"):
                own().assert_card_allowed(s, account, FAN, terms([121, 123]))
            own().assert_card_allowed(s, account, FAN, terms([123]))
            own().assert_card_allowed(s, account, OTHER, terms([121]))
            own().assert_card_allowed(s, account, FAN, terms([121], price=0))          # free content is never blocked
    finally:
        store.close()


def test_missing_evidence_is_unknown_until_coverage_is_complete_then_clear_as_of_and_never_unowned(database):
    store, account, cfg = prepared(database)
    wire = Provider(cfg)
    wire.chats[FAN] = [chat_page([fan_message(9801, FAN)], next_url(cfg, FAN, 9800)), chat_page([fan_message(9799, FAN)])]
    try:
        hist().enlist_fan(store, account, FAN, priority=0)
        assert states(derive(database, account, 999, [301, 302])) == {301: "unknown", 302: "unknown"}, "a fan nobody listed"
        run(store, account, wire)                                             # one page: mid-history
        mid = derive(database, account, FAN, [301])
        assert states(mid) == {301: "unknown"} and "coverage_partial" in mid[301].reasons
        drive(store, account, wire, lambda: at_end(database, account) and cursor_of(database, account).status == "complete")
        done = derive(database, account, FAN, [301, 302])
        assert states(done) == {301: "clear_as_of", 302: "clear_as_of"} and done[301].as_of is not None
        everything = repr([(v.state, v.reasons, v.evidence) for v in list(mid.values()) + list(done.values())])
        assert "unowned" not in everything and {v.state for v in done.values()} <= {"owned", "unknown", "clear_as_of"}
    finally:
        store.close()


def test_a_priced_card_that_was_never_opened_and_a_tip_are_not_ownership(database):
    cfg = config(1)
    store, account, cfg, wire = world(database, 1, messages=[
        card(9201, cfg, [131, 132], 15, previews=[131], opened=False),
        card(9202, cfg, [141], 9, isTip=True, opened=True, previews=[]),
        card(9203, cfg, [151], 15, previews=[])])
    try:
        assert states(derive(database, account, FAN, [131, 132, 141, 151])) == {m: "clear_as_of" for m in (131, 132, 141, 151)}
    finally:
        store.close()


def test_a_reversal_makes_owned_media_unknown_until_a_person_resolves_it(database):
    cfg = config(1)
    store, account, cfg, wire = world(database, 1, messages=[opened(cfg, 9301, [161, 162], [161])],
                                      tx=[tx_row("t1", FAN)], cb=[cb_row("t1", FAN)])
    try:
        reconcile_transactions(database, account)
        got = derive(database, account, FAN, [161, 162, 163])
        assert states(got) == {161: "clear_as_of", 162: "unknown", 163: "clear_as_of"}
        assert got[162].reasons == ("reversal_unresolved",), "a reversed purchase is unknown, neither owned nor clear"
        with database[1].begin() as c:
            c.execute(text("UPDATE of_fans_evidence SET status='dismissed', resolved_by='op-session:resolved' WHERE account_id=:a AND kind='chargeback'"), {"a": account})
        assert states(derive(database, account, FAN, [162])) == {162: "owned"}
    finally:
        store.close()


def test_a_transaction_the_provider_calls_undone_is_a_reversal_too(database):
    cfg = config(1)
    store, account, cfg, wire = world(database, 1, messages=[opened(cfg, 9311, [171, 172], [171])], tx=[tx_row("t9", FAN, status="undo")])
    try:
        assert states(derive(database, account, FAN, [172])) == {172: "unknown"}
    finally:
        store.close()


def test_an_unlock_with_no_card_terms_is_unknown_and_never_clear(database):
    cfg = config(1)
    store, account, cfg, wire = world(database, 1, messages=[creator_message(9400, cfg, "ordinary reply")], fans=(FAN, OTHER))
    try:
        from db.onlyfans_fans_intake_models import OfFansEvidence
        with Session(database[1]) as s, s.begin():                              # an unlock for a message we never saw
            s.add(OfFansEvidence(account_id=account, evidence_id="e-1", dedupe_key="unlock:9999", kind="unlock", source="webhook", fan_id=FAN,
                                 message_id=9999, amount_cents=1500, occurred_at=utcnow(), status="pending", raw_sha256="a" * 64,
                                 created_at=utcnow(), updated_at=utcnow()))
        got = derive(database, account, FAN, [181, 182])
        assert states(got) == {181: "unknown", 182: "unknown"} and got[181].reasons == ("unlock_message_unseen",)
        assert states(derive(database, account, OTHER, [181])) == {181: "clear_as_of"}, "another fan is unaffected"
    finally:
        store.close()


def test_payments_the_unlock_evidence_cannot_explain_make_the_fan_unknown(database):
    cfg = config(1)
    store, account, cfg, wire = world(database, 1, messages=[opened(cfg, 9501, [191, 192], [191])], tx=[tx_row("t1", FAN), tx_row("t2", FAN, 20.0)])
    try:
        got = derive(database, account, FAN, [192, 193])
        assert states(got) == {192: "owned", 193: "unknown"} and got[193].reasons == ("payment_without_unlock",)
    finally:
        store.close()
    # one payment, one opened card: the count matches, but a count is not an identity. The payment stays unexplained until a
    # person reconciles it (H2; this assertion formerly expected 193 clear_as_of from the numeric comparison).
    store, account, cfg, wire = world(database, 2, messages=[opened(config(2), 9502, [191, 192], [191])], tx=[tx_row("t1", FAN)])
    try:
        got = derive(database, account, FAN, [192, 193])
        assert states(got) == {192: "owned", 193: "unknown"} and got[193].reasons == ("payment_without_unlock",)
        reconcile_transactions(database, account)
        assert states(derive(database, account, FAN, [192, 193])) == {192: "owned", 193: "clear_as_of"}
    finally:
        store.close()


def test_a_tip_or_subscription_payment_is_not_an_unexplained_purchase(database):
    cfg = config(1)
    store, account, cfg, wire = world(database, 1, messages=[opened(cfg, 9601, [201, 202], [201])],
                                      tx=[tx_row("t1", FAN), {**tx_row("t2", FAN), "type": "tip"}, {**tx_row("t3", FAN), "type": "subscription"}])
    try:
        reconcile_transactions(database, account)           # only the message payment is a purchase payment; tip/subscription never were
        assert states(derive(database, account, FAN, [203])) == {203: "clear_as_of"}
    finally:
        store.close()


def test_the_latest_1000_window_is_a_catalogue_selection_not_ownership_proof(database):
    from db.models import VaultItem
    from db.onlyfans_vault_models import OfVaultSyncJob
    cfg = config(1)
    store, account, cfg = prepared(database)
    wire = Provider(cfg)
    wire.vault = [lambda: page([_item(i) for i in (501, 502)])]
    try:
        hist().enlist_fan(store, account, FAN, priority=3)
        run(store, account, wire)                                             # the window completes; the fan is still unread
        with Session(database[1]) as s:
            job = s.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id == account))
            assert job.status == "complete" and {"501", "502"} <= set(json.loads(job.window_json)["selected"])
        got = derive(database, account, FAN, [501, 502])
        assert states(got) == {501: "unknown", 502: "unknown"}, "being in the selection proves nothing about ownership"
        assert view_state(store, account, "vault_window") == "complete"
    finally:
        store.close()
    # media that rotated out of (or never entered) the window stays owned when history says it was bought
    store, account, cfg, wire = world(database, 2, messages=[opened(config(2), 9701, [501, 777], [])])
    try:
        assert states(derive(database, account, FAN, [501, 777])) == {501: "owned", 777: "owned"}
        with database[1].begin() as c:
            c.execute(text("UPDATE of_vault_sync_job SET window_json='{\"selected\":[]}' WHERE account_id=:a"), {"a": account})
            c.execute(text("UPDATE vault_items SET removed_at=now() WHERE account_id=:a AND media_id=501"), {"a": account})
        assert states(derive(database, account, FAN, [501, 777])) == {501: "owned", 777: "owned"}
    finally:
        store.close()


def view_state(store, account, source):
    return hist().coverage_view(store, TENANT, account)["sources"][source]["state"]


def test_unknown_blocks_only_a_new_paid_card_not_free_content_other_fans_or_other_products(database):
    cfg = config(1)
    store, account, cfg, wire = world(database, 1, messages=[card(9801, cfg, [211, 212], 15, opened=True)], fans=(FAN, OTHER))
    try:
        with Session(database[1]) as s:
            for fan, media, price, blocked in ((FAN, [211], 1500, True), (FAN, [211], 0, False), (FAN, [213], 1500, False), (OTHER, [211], 1500, False)):
                if blocked:
                    with pytest.raises(Refused):
                        own().assert_card_allowed(s, account, fan, terms(media, price=price))
                else:
                    own().assert_card_allowed(s, account, fan, terms(media, price=price))
    finally:
        store.close()


def test_two_accounts_with_identical_fan_and_media_ids_never_share_ownership(database):
    a_store, a, a_cfg, _ = world(database, 1, messages=[opened(config(1), 9901, [221, 222], [221])], tx=[tx_row("t1", FAN)], cb=[cb_row("t1", FAN)])
    b_store, b, b_cfg, _ = world(database, 2, messages=[creator_message(9901, config(2), "same id, no card")])
    try:
        reconcile_transactions(database, a)
        assert states(derive(database, a, FAN, [221, 222])) == {221: "clear_as_of", 222: "unknown"}
        assert states(derive(database, b, FAN, [221, 222])) == {221: "clear_as_of", 222: "clear_as_of"}, "A's purchase and reversal do not leak into B"
        with Session(database[1]) as s:
            with pytest.raises(Refused):
                own().assert_card_allowed(s, a, FAN, terms([222]))
            own().assert_card_allowed(s, b, FAN, terms([222]))
    finally:
        a_store.close(); b_store.close()


def test_positive_ownership_already_recorded_by_the_engine_still_counts(database):
    import json as _json
    from db.models import Message
    cfg = config(1)
    store, account, cfg, wire = world(database, 1)
    try:
        with Session(database[1]) as s, s.begin():
            s.add(Message(account_id=account, fan_id=FAN, message_id=5551, direction="out", body="", media_ids=_json.dumps([231]),
                          media_count=1, price_cents=1500, is_paid=True, created_at=utcnow()))
        assert states(derive(database, account, FAN, [231, 232])) == {231: "owned", 232: "clear_as_of"}
    finally:
        store.close()


def test_the_derivation_module_writes_nothing(database):
    cfg = config(1)
    store, account, cfg, wire = world(database, 1, messages=[opened(cfg, 9951, [241, 242], [241])])
    try:
        tables = ("of_fans_evidence", "of_fans_message_fact", "of_fans_import_gap", "of_fans_import_source", "of_fans_history_cursor", "messages", "of_fans_read_meter")
        def snapshot():
            with database[1].connect() as c:
                return {t: c.scalar(text(f"SELECT md5(string_agg(x::text, '' ORDER BY x::text)) FROM {t} x")) for t in tables}
        before = snapshot()
        derive(database, account, FAN, [241, 242, 243])
        with Session(database[1]) as s:
            own().evaluate_card(s, account, FAN, terms([241]))
        assert snapshot() == before
        source = inspect.getsource(own())
        assert not re.search(r"\bs\.(add|merge|delete|flush|commit|rollback|begin)\(|\b(insert|update|delete)\(|\.execute\(", source), "read-only by construction"
    finally:
        store.close()


# ---- the wire-in: commerce, final dispatch and the journey gate ------------------------------------------

def test_commerce_and_final_dispatch_refuse_owned_and_unknown_cards_only_for_configured_accounts(database):
    from conversation_engine.commerce import _unowned
    from onlyfans_runtime.media_delivery import assert_unowned
    cfg = config(1)
    store, account, cfg, wire = world(database, 1, messages=[opened(cfg, 9961, [251, 252], [251]), card(9962, cfg, [261], 15, opened=True)])
    legacy_store, legacy, legacy_cfg = prepared(database, 2, begin=False)
    try:
        with Session(database[1]) as s:
            for check in (_unowned, assert_unowned):
                with pytest.raises(Refused, match="owned"):
                    check(s, account, FAN, terms([252, 253]))
                with pytest.raises(Refused, match="unknown"):
                    check(s, account, FAN, terms([261, 262]))
                check(s, account, FAN, terms([253, 254]))
                check(s, account, FAN, terms([261], price=0))
        with Session(database[1]) as s:           # an account that never started an import keeps its old behaviour exactly
            _unowned(s, legacy, FAN, terms([252, 261]))
            assert_unowned(s, legacy, FAN, terms([252, 261]))
    finally:
        store.close(); legacy_store.close()


def test_backfill_complete_follows_per_fan_coverage_once_an_import_is_configured(database):
    from db.onlyfans_fans_intake_models import OfFansBackfill
    from conversation_engine.journey import backfill_complete
    from datetime import timedelta
    store, account, cfg = prepared(database)
    legacy_store, legacy, legacy_cfg = prepared(database, 2, begin=False)
    wire = Provider(cfg)
    def weak_run(acct):
        with Session(database[1]) as s, s.begin():
            s.add(OfFansBackfill(account_id=acct, run_id="run-1", reason="operator", start_date=utcnow() - timedelta(days=1), status="complete",
                                 pages_used=1, pages_budget=5, marker=json.dumps({"done_chats": [str(FAN)], "chats": []}),
                                 rows_seen=0, rows_new=0, started_at=utcnow(), updated_at=utcnow()))
    try:
        weak_run(account); weak_run(legacy)
        with Session(database[1]) as s:
            assert backfill_complete(s, legacy, FAN) is True, "an account that never started an import keeps the legacy gate"
            assert backfill_complete(s, account, FAN) is False, "a run that started yesterday is not lifetime coverage of this fan"
        read_everything(store, database, account, wire)
        with Session(database[1]) as s:
            assert backfill_complete(s, account, FAN) is True
            assert backfill_complete(s, account, OTHER) is False and backfill_complete(s, account, 999) is False
    finally:
        store.close(); legacy_store.close()

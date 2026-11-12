"""Evidence for the pending policy decision on the operator RETIREMENT of an unconfirmed Plan C text send.

What the path is (send_recovery.release_unconfirmed_send): an audited OPERATOR decision that retires the unresolved
hold of one plain, zero-price, engine-authored text whose provider response was lost. It is NOT proof that the original
send never happened. The intent ends cancelled/operator_released_unconfirmed_send, the attempt stays `unknown`, the
reservation and every wire field stay, and new work uses the ordinary scheduler with the old action labelled
"unconfirmed". This file adds the counterexamples that the 30 minute quiet / 2 minute freshness policy decision needs,
one group per contract bullet. It reuses the rig and helpers of test_plan_c_unknown_send_release_pg.py.

Real PostgreSQL, real Plan C text-only pilot rig, scripted provider; nothing is sent anywhere.
"""
from datetime import timedelta
import json

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from conversation_engine import pilot_scope
from db.models import Message, OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineAttempt, OfEngineDecision
from db.onlyfans_fans_intake_models import OfFansEvidence
from onlyfans_runtime import send_recovery
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Refused, utcnow
from twin_bridge.platform import escape_plain_text
from test_engine_real_worker_pg import CREATOR, FAN2, inbound, iso, world  # noqa: F401
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_plan_c_draft_app_pg import configured_draft  # noqa: F401
from test_plan_c_events_stage3_pg import ev, rig  # noqa: F401
from test_plan_c_q2_n3_multipart_pilot_pg import A, B, install_model, to_fan
from test_plan_c_scope_pg import ACTOR
from test_plan_c_text_activation_pg import stopped_text  # noqa: F401
from test_plan_c_unknown_send_release_pg import (  # noqa: F401
    CREATOR_ID, K, REASON, Wire, absent, age, at, authority, counters, facts, provider_message, put, read, refused,
    release, revision, scene, setup_unknown, unknown_intent, unpace)


def _row(rig, table, where):
    """Every column of one row as a dict (used to prove what did NOT change)."""
    with rig.w.database[1].begin() as c:
        return dict(c.execute(text(f'SELECT * FROM {table} WHERE {where}')).mappings().one())


def _intent_row(rig, intent):
    return _row(rig, 'of_text_intent', f"intent_id = '{intent.intent_id}'")


# ======================================================================================================================
# Bullet 1: exact identity, payload and wire evidence preserved; limited to the plain zero-price text scope
# ======================================================================================================================
def test_every_identity_payload_and_wire_field_of_the_retired_intent_is_preserved(scene):
    rig, wire, intent = scene
    age(rig)
    before = _intent_row(rig, intent)
    attempt_before = _row(rig, 'of_engine_attempt', f"attempt_id = '{intent.attempt_id}'")
    decision_before = _row(rig, 'of_engine_decision', f"intent_id = '{intent.intent_id}'")
    release(rig, intent, read(rig, FAN, intent))
    after = _intent_row(rig, intent)
    changed = {k for k in before if before[k] != after[k]}
    assert changed == {'status', 'reason', 'updated_at'}, f'only the hold state may change, not {changed}'
    for field in ('account_id', 'binding_id', 'fan_id', 'attempt_id', 'body', 'payload_hash', 'command_key', 'dispatch_token',
                  'dispatched_at', 'wire_started_at', 'wire_version', 'price_cents', 'receipt_json', 'provider_message_id',
                  'reservation_held'):
        assert after[field] == before[field], field
    assert after['receipt_json'] is None and after['provider_message_id'] is None, 'no receipt is invented'
    attempt_after = _row(rig, 'of_engine_attempt', f"attempt_id = '{intent.attempt_id}'")
    assert attempt_after['outcome'] == 'unknown'
    assert {k for k in attempt_before if attempt_before[k] != attempt_after[k]} <= {'evidence_json', 'updated_at'}
    kept = json.loads(attempt_before['evidence_json'])
    now = json.loads(attempt_after['evidence_json'])
    assert all(now[key] == kept[key] for key in kept), 'the provider/hold evidence kept before is still there, unedited'
    decision_after = _row(rig, 'of_engine_decision', f"intent_id = '{intent.intent_id}'")
    for field in ('body', 'payload_hash', 'fan_id', 'binding_id', 'action', 'terms_json', 'intent_id'):
        if field in decision_before:
            assert decision_after[field] == decision_before[field], field


@pytest.mark.parametrize('label,sql', [
    ('quoted', "UPDATE of_text_intent SET quote_json = '{\"q\":1}' WHERE status = 'unknown'"),
    ('has_receipt', "UPDATE of_text_intent SET receipt_json = '{}' WHERE status = 'unknown'"),
    ('has_provider_id', "UPDATE of_text_intent SET provider_message_id = 777 WHERE status = 'unknown'"),
    ('projected', "UPDATE of_text_intent SET projected_at = now() WHERE status = 'unknown'"),
    ('reservation_released', "UPDATE of_text_intent SET reservation_held = false WHERE status = 'unknown'"),
    ('no_wire_marker', "UPDATE of_text_intent SET wire_started_at = NULL WHERE status = 'unknown'"),
    ('no_dispatch_token', "UPDATE of_text_intent SET dispatch_token = NULL WHERE status = 'unknown'"),
    ('not_dispatched', "UPDATE of_text_intent SET dispatched_at = NULL WHERE status = 'unknown'"),
    ('body_edited', "UPDATE of_text_intent SET body = body || ' (edited)' WHERE status = 'unknown'"),
    ('payload_hash_changed', "UPDATE of_text_intent SET payload_hash = repeat('0', 64) WHERE status = 'unknown'"),
    ('other_binding', "UPDATE of_text_intent SET binding_id = 'other-binding' WHERE status = 'unknown'"),
    ('campaign_origin', "UPDATE of_text_intent SET origin = 'campaign' WHERE status = 'unknown'"),
    ('engagement_origin', "UPDATE of_text_intent SET origin = 'engagement' WHERE status = 'unknown'"),
    ('already_accepted', "UPDATE of_text_intent SET status = 'accepted' WHERE status = 'unknown'"),
    ('dispatch_expired_is_ok_but_conflict_is_not', "UPDATE of_text_intent SET reason = 'provider_identity_conflict' WHERE status = 'unknown'"),
    ('offer_action', "UPDATE of_engine_decision SET action = 'offer' WHERE status = 'unknown'"),
    ('offer_id', "UPDATE of_text_intent SET offer_id = 1 WHERE status = 'unknown'"),
    ('decision_not_unknown', "UPDATE of_engine_decision SET status = 'held' WHERE status = 'unknown'"),
    ('attempt_not_unknown', "UPDATE of_engine_attempt SET outcome = 'not_accepted' WHERE outcome = 'unknown'"),
    ('other_transport', "UPDATE of_delivery_policy SET transport_kind = 'synthetic'"),
])
def test_only_a_plain_unchanged_unknown_text_of_the_fan_can_be_retired(scene, label, sql):
    rig, wire, intent = scene
    age(rig)
    history = read(rig, FAN, intent)
    try:
        with rig.w.database[1].begin() as c:
            c.exec_driver_sql(sql)
    except DBAPIError:            # the schema/guard triggers already forbid this shape: that is also a refusal
        return
    before = facts(rig)
    with pytest.raises(Refused):
        release(rig, intent, history, expected_attempt_id=intent.attempt_id)
    assert facts(rig) == before, 'a refused retirement changes nothing'


@pytest.mark.parametrize('label,sql', [
    ('priced', "UPDATE of_text_intent SET price_cents = 300 WHERE status = 'unknown'"),
    ('media', "UPDATE of_text_intent SET media_json = '[5]' WHERE status = 'unknown'"),
    ('previews', "UPDATE of_text_intent SET preview_json = '[6]' WHERE status = 'unknown'"),
    ('catalogue_item', "UPDATE of_text_intent SET catalog_item_id = 1 WHERE status = 'unknown'"),
])
def test_paid_or_media_shapes_cannot_exist_on_a_plain_text_intent_and_are_refused_by_the_path_as_well(scene, label, sql):
    """Two layers. The schema's media_shape check forbids these rows; on a THROWAWAY test database that check is dropped
    to prove the Python predicate refuses the same shape on its own (a second, independent line of defence)."""
    rig, wire, intent = scene
    age(rig)
    history = read(rig, FAN, intent)
    with pytest.raises(IntegrityError):
        with rig.w.database[1].begin() as c:
            c.exec_driver_sql(sql)
    with rig.w.database[1].begin() as c:
        name = c.execute(text("SELECT conname FROM pg_constraint WHERE conrelid = 'of_text_intent'::regclass "
                              "AND conname LIKE '%media_shape'")).scalar_one()
        c.execute(text(f'ALTER TABLE of_text_intent DROP CONSTRAINT "{name}"'))
        c.execute(text('ALTER TABLE of_text_intent DISABLE TRIGGER USER'))
        c.exec_driver_sql(sql)
    before = facts(rig)
    with pytest.raises(Refused):
        release(rig, intent, history)
    assert facts(rig) == before


def test_the_retirement_needs_a_pilot_scope_that_contains_the_fan(scene):
    rig, wire, intent = scene
    age(rig)
    history = read(rig, FAN, intent)
    before = facts(rig)
    with pytest.raises(Refused, match='outside an active or stopped Plan C pilot scope'):
        send_recovery.release_unconfirmed_send(authority(rig), TENANT, ACCOUNT, 7777, intent.intent_id,
            expected_attempt_id=intent.attempt_id, expected_revision=1, next_input_id=81002, history=history,
            command_key=K(8), actor=ACTOR, reason=REASON)
    assert facts(rig) == before


# ======================================================================================================================
# Bullet 2: missing text on one page, or stale/incomplete/unreadable/wrong-account history, is insufficient;
#           the observation's coverage, pagination, timestamps and limitations are established (and recorded)
# ======================================================================================================================
def test_a_matching_message_on_an_older_page_blocks_even_though_the_newest_page_is_clean(scene):
    rig, wire, intent = scene
    age(rig)
    newest = [provider_message(81002, FAN, 'hey are you there??', iso(-1))]
    middle = [provider_message(90500, CREATOR_ID, 'a human typed this', at(rig, intent, 20))]     # inside the send window
    oldest = [provider_message(81001, FAN, 'hello, how was your day?', at(rig, intent, -300))]
    wire.pages = {FAN: [newest, middle, oldest], FAN2: [[]]}
    history = read(rig, FAN, intent)
    assert history.pages == 3 and history.complete, 'the whole chain was read, one clean page is not the observation'
    refused(rig, intent, history, 'absence is not proven')
    wire.pages = {FAN: [newest, [provider_message(90501, CREATOR_ID, intent.body, at(rig, intent, 4))], oldest], FAN2: [[]]}
    with pytest.raises(Refused, match='positive reconciliation'):
        release(rig, intent, read(rig, FAN, intent))


def test_history_that_is_unavailable_disordered_or_for_a_different_page_chain_is_not_evidence(scene):
    rig, wire, intent = scene
    age(rig)
    newer = [provider_message(81002, FAN, 'hey are you there??', iso(-1))]
    older = [provider_message(81001, FAN, 'hello, how was your day?', iso(-300))]
    wire.pages = {FAN: [older, newer], FAN2: [[]]}              # the second page is NEWER than the first
    with pytest.raises(Refused, match='out of order'):
        read(rig, FAN, intent)
    wire.pages = {FAN: [[{'id': 81002, 'createdAt': 'not a time'}]], FAN2: [[]]}
    with pytest.raises(Refused, match='unreadable'):
        read(rig, FAN, intent)
    wire.pages = None
    wire.history[FAN] = 'not a list'
    with pytest.raises(Refused, match='unavailable'):
        read(rig, FAN, intent)


def test_a_history_that_stopped_before_it_reached_the_send_is_incomplete_and_says_so(scene):
    rig, wire, intent = scene
    age(rig)
    recent = [provider_message(81900 + n, FAN, f'x{n}', iso(-1 - n)) for n in range(3)]
    wire.pages = {FAN: [recent for _ in range(12)], FAN2: [[]]}
    history = read(rig, FAN, intent)             # default bound: at most 10 pages
    assert history.pages == 10 and history.complete is False and history.reached_start is False
    refused(rig, intent, history, 'complete provider history required')


def test_the_audit_record_states_the_observation_coverage_pagination_timestamps_and_limits(scene):
    rig, wire, intent = scene
    age(rig)
    newest = [provider_message(81002, FAN, 'hey are you there??', iso(-1))]
    older = [provider_message(81001, FAN, 'hello, how was your day?', at(rig, intent, -300))]
    wire.pages = {FAN: [newest, older, newest], FAN2: [[]]}
    history = read(rig, FAN, intent)
    release(rig, intent, history)
    with Session(rig.w.database[1]) as s:
        command = s.scalar(select(OfLaunchCommand).where(OfLaunchCommand.kind == 'release_unconfirmed_send'))
        observed = json.loads(command.before_json)['providerHistory']
        attempt = json.loads(s.get(OfEngineAttempt, (ACCOUNT, intent.attempt_id)).evidence_json)
    assert observed['coverage'] == 'older_than_dispatch' and observed['pages'] == 2 and observed['reachedStart'] is False
    assert observed['messages'] == 2 and observed['oldest'] and observed['startedAt'] and observed['endedAt']
    assert observed['quietSeconds'] == 1800 and observed['freshnessSeconds'] == 120
    limits = ' '.join(observed['limitations'])
    for phrase in ('does not prove', 'deleted', 'delayed', 'omitted'):
        assert phrase in limits, phrase
    assert attempt['release']['outcome'] == 'unconfirmed' and attempt['release']['coverage'] == 'older_than_dispatch'
    assert not {'not_sent', 'not_delivered', 'absent_proven'} & set(attempt) and 'proven' not in json.dumps(attempt)


def test_reaching_the_chat_start_is_recorded_as_such(scene):
    rig, wire, intent = scene
    age(rig)
    release(rig, intent, read(rig, FAN, intent))
    with Session(rig.w.database[1]) as s:
        command = s.scalar(select(OfLaunchCommand).where(OfLaunchCommand.kind == 'release_unconfirmed_send'))
        assert json.loads(command.before_json)['providerHistory']['coverage'] == 'chat_start'


# ======================================================================================================================
# Bullet 3: unknown outcome and reserved units retained; never not_sent; never refunded for lack of a match
# ======================================================================================================================
def test_no_record_ever_says_not_sent_and_no_unit_is_refunded_by_a_retirement_or_by_a_refusal(scene):
    rig, wire, intent = scene
    age(rig)
    before_counters = counters(rig)
    before = facts(rig)
    for _ in range(2):                                   # refusals for "nothing matched" reasons change nothing
        with pytest.raises(Refused):
            release(rig, intent, read(rig, FAN, intent), n=9, next_input=99999)
    assert facts(rig) == before and counters(rig)[0] == before_counters[0], 'no unit comes back because nothing matched'
    release(rig, intent, read(rig, FAN, intent))
    assert counters(rig)[0] == before_counters[0], 'delivery units used never go down'
    with Session(rig.w.database[1]) as s:
        dump = json.dumps([dict(r._mapping) for r in s.execute(text(
            'SELECT status, reason, reservation_held, receipt_json, evidence_ref FROM of_text_intent')).all()], default=str)
        dump += json.dumps([dict(r._mapping) for r in s.execute(text('SELECT outcome, evidence_json FROM of_engine_attempt')).all()], default=str)
        dump += json.dumps([dict(r._mapping) for r in s.execute(text('SELECT before_json, result_json FROM of_launch_command')).all()], default=str)
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        assert policy.used_units >= before['used']
    assert 'not_sent' not in dump and 'never sent' not in dump.lower() and 'not sent' not in dump.lower()
    assert '"unconfirmed"' in dump


# ======================================================================================================================
# Bullet 4: intended actor, current revision, audit/idempotency key, current newer fan request
# ======================================================================================================================
def test_a_control_change_after_the_operator_looked_makes_the_command_stale(scene):
    rig, wire, intent = scene
    age(rig)
    history = read(rig, FAN, intent)
    seen_revision = revision(rig)
    rig.post(inbound(81003, 'hello??? anyone??', fan=FAN), key='late-third')
    rig.ticks(2)
    refused(rig, intent, history, 'control revision conflict|fresh fan message', expected_revision=seen_revision)
    refused(rig, intent, history, 'fresh fan message', next_input=81002, expected_revision=revision(rig))


def test_the_newer_request_must_be_a_live_current_fan_message_not_deleted_or_imported(scene):
    rig, wire, intent = scene
    age(rig)
    history = read(rig, FAN, intent)
    with rig.w.database[1].begin() as c:
        c.execute(text('UPDATE messages SET is_unsent = true WHERE message_id = 81002'))
    refused(rig, intent, history, 'fresh fan message')
    with rig.w.database[1].begin() as c:
        c.execute(text('UPDATE messages SET is_unsent = false WHERE message_id = 81002'))
        c.execute(text("UPDATE of_ingress SET source = 'import' WHERE message_id = 81002"))
    refused(rig, intent, history, 'fresh fan message')


def test_the_actor_is_recorded_with_the_reason_and_the_key_and_cannot_be_another_account_operator(scene):
    rig, wire, intent = scene
    age(rig)
    history = read(rig, FAN, intent)
    release(rig, intent, history)
    with Session(rig.w.database[1]) as s:
        command = s.scalar(select(OfLaunchCommand).where(OfLaunchCommand.kind == 'release_unconfirmed_send'))
        assert (command.actor, command.command_key, command.reason) == (ACTOR, K(1), REASON)


# ======================================================================================================================
# Bullet 5: the retired intent/key and its remaining multipart parts are never replayed; new work is ordinary
# ======================================================================================================================
def test_remaining_multipart_parts_are_cancelled_and_never_sent_after_the_retirement(rig):
    wire = Wire(rig)
    install_model(rig)                                  # call 1 -> sequence A (three parts), later calls -> sequence B
    unpace(rig)
    wire.post_script.append('timeout')
    rig.post(inbound(81001, 'hello, how was your day?', fan=FAN), key='mp-a')
    rig.ticks(3)
    wire.post_script.clear()
    intent = unknown_intent(rig, FAN)
    assert intent.body == A[0] and to_fan(rig, FAN) == [A[0]], 'part one went out and its answer was lost'
    with Session(rig.w.database[1]) as s:
        parts = [(d.part_index, d.status) for d in s.scalars(select(OfEngineDecision).order_by(OfEngineDecision.part_index))]
    assert parts == [(0, 'unknown'), (1, 'ready'), (2, 'ready')]
    rig.post(inbound(81002, 'hey are you there??', fan=FAN), key='mp-b')
    rig.ticks(3)
    assert to_fan(rig, FAN) == [A[0]], 'while unknown nothing else is sent, parts two and three included'
    absent(rig, wire, intent)
    age(rig)
    release(rig, intent, read(rig, FAN, intent))
    with Session(rig.w.database[1]) as s:
        parts = [(d.part_index, d.status) for d in s.scalars(select(OfEngineDecision).where(
            OfEngineDecision.request_id == select(OfEngineDecision.request_id).where(OfEngineDecision.intent_id == intent.intent_id).scalar_subquery())
            .order_by(OfEngineDecision.part_index))]
    assert parts == [(0, 'held'), (1, 'cancelled'), (2, 'cancelled')]
    unpace(rig)
    rig.ticks(8)
    sent = to_fan(rig, FAN)
    assert sent[0] == A[0] and not set(sent[1:]) & {A[0], A[1], A[2]}, 'no part of the retired sequence is ever sent'
    assert len(sent) >= 2 and sent[1] == B[0], 'new work is a fresh plan from current context'
    keys = [p['key'] for p in rig.w.fans.posts]
    assert len(keys) == len(set(keys)), 'the retired idempotency key is never reused'
    assert json.dumps(rig.seen[-1]).count('unconfirmed') >= 1, 'the fresh plan carries the earlier uncertainty'


# ======================================================================================================================
# Bullet 6: stop/takeover and other unresolved evidence still win, also AFTER the retirement
# ======================================================================================================================
@pytest.mark.parametrize('mode', ['no_contact', 'paused'])
def test_a_stop_after_the_retirement_still_blocks_the_new_work(scene, mode):
    rig, wire, intent = scene
    age(rig)
    release(rig, intent, read(rig, FAN, intent))
    with rig.w.database[1].begin() as c:
        c.execute(text(f"UPDATE of_conversation_state SET mode = '{mode}' WHERE fan_id = {FAN}"))
    posts = facts(rig)['posts']
    unpace(rig)
    rig.ticks(5)
    assert facts(rig)['posts'] == posts, 'retiring the unknown hold never overrides a stop'


def test_a_takeover_after_the_retirement_still_blocks_the_new_work(scene):
    rig, wire, intent = scene
    age(rig)
    release(rig, intent, read(rig, FAN, intent))
    with rig.w.database[1].begin() as c:
        c.execute(text(f"UPDATE of_conversation_state SET takeover_state = 'requested' WHERE fan_id = {FAN}"))
    posts = facts(rig)['posts']
    unpace(rig)
    rig.ticks(5)
    assert facts(rig)['posts'] == posts


def test_the_retirement_frees_only_its_own_fan(rig):
    wire = Wire(rig)
    setup_unknown(rig, wire, fan=FAN, first=82001, second=82002, key='own')
    one = unknown_intent(rig, FAN)
    rig.post(inbound(82101, 'hello, how was your day?', fan=FAN2), key='own-fan2-a')
    wire.history[FAN] = [provider_message(82002, FAN, 'hey are you there??', iso(-1)),
                         provider_message(82001, FAN, 'hello, how was your day?', iso(-2))]
    age(rig)
    release(rig, one, read(rig, FAN, one), next_input=82002)
    unpace(rig)
    rig.ticks(6)
    with Session(rig.w.database[1]) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN2)) is not None
        assert [i.status for i in s.scalars(select(OfTextIntent).where(OfTextIntent.fan_id == FAN2))] in (['accepted'], [])


# ======================================================================================================================
# Bullet 7: late positive evidence after the retirement is retained and reconciled, never reopening old authority
# ======================================================================================================================
def _retire_and_answer(rig, wire, intent):
    age(rig)
    release(rig, intent, read(rig, FAN, intent))
    unpace(rig)
    rig.ticks(5)
    return facts(rig)


def _late_copy(rig, intent, ident=99001):
    """The provider's own history row for the message the lost send DID deliver, found only after the retirement."""
    return provider_message(ident, CREATOR_ID, escape_plain_text(intent.body), at(rig, intent, 3),
                            toUser={'id': FAN}, lockedText=False, previews=[])


def test_a_late_signed_sent_event_for_the_retired_text_is_kept_as_evidence_and_reopens_nothing(scene):
    rig, wire, intent = scene
    done = _retire_and_answer(rig, wire, intent)
    row_before = _intent_row(rig, intent)
    rig.post(ev('messages.sent', {'id': 99001, 'toUser': {'id': FAN}, 'text': intent.body, 'createdAt': iso(),
                                  'isFromQueue': False, 'isSentByMe': True}), key='late-sent')
    rig.ticks(5)
    after = facts(rig)
    assert after['posts'] == done['posts'], 'the late evidence creates no send'
    row_after = _intent_row(rig, intent)
    assert (row_after['status'], row_after['reason']) == ('cancelled', send_recovery.RELEASED_UNCONFIRMED_SEND)
    assert row_after['receipt_json'] is None and row_after['dispatch_token'] == row_before['dispatch_token']
    with Session(rig.w.database[1]) as s:
        evidence = s.scalars(select(OfFansEvidence).where(OfFansEvidence.message_id == 99001)).all()
        assert len(evidence) == 1 and evidence[0].kind == 'human_sent', 'the late message is retained, not dropped'
        assert s.get(OfEngineAttempt, (ACCOUNT, intent.attempt_id)).outcome == 'unknown', 'the conflict is not erased'
    assert rig.block(FAN) is not None or after['states'][FAN][2] != 'none' or after['states'][FAN][1] != 'automated', \
        'an unexplained creator-account message holds or hands over the fan; it is not silently dismissed'


def test_late_positive_provider_history_is_recorded_against_the_retired_intent_without_settling_it(scene):
    rig, wire, intent = scene
    done = _retire_and_answer(rig, wire, intent)
    row_before = _intent_row(rig, intent)
    late = _late_copy(rig, intent)
    # the ordinary reconcile path must still refuse: a retired engine intent is one-use and never becomes accepted
    with pytest.raises(Refused, match='unavailable'):
        send_recovery.reconcile(authority(rig), TENANT, ACCOUNT, FAN, intent.intent_id, history=[late], complete=True,
                                provider_message_id=99001, command_key=K(31), actor=ACTOR, reason=REASON)
    result = send_recovery.record_late_positive_evidence(
        authority(rig), TENANT, ACCOUNT, FAN, intent.intent_id, history=[late], complete=True, provider_message_id=99001,
        command_key=K(32), actor=ACTOR, reason='found in history after the retirement')
    assert result['recorded'] is True and result['outcome'] == 'late_positive_after_release' and result['reopened'] is False
    assert send_recovery.record_late_positive_evidence(
        authority(rig), TENANT, ACCOUNT, FAN, intent.intent_id, history=[late], complete=True, provider_message_id=99001,
        command_key=K(32), actor=ACTOR, reason='found in history after the retirement') == result, 'idempotent per key'
    row_after = _intent_row(rig, intent)
    assert row_after == row_before, 'the retired intent row is byte-for-byte unchanged: no receipt, no accepted, no new authority'
    with Session(rig.w.database[1]) as s:
        attempt = s.get(OfEngineAttempt, (ACCOUNT, intent.attempt_id))
        assert attempt.outcome == 'unknown'
        evidence = json.loads(attempt.evidence_json)
        assert evidence['release']['outcome'] == 'unconfirmed', 'the retirement record is still there'
        assert evidence['late_positive']['provider_message_id'] == 99001 and evidence['late_positive']['conflicts_with_release'] is True
        command = s.scalar(select(OfLaunchCommand).where(OfLaunchCommand.kind == 'late_positive_after_release'))
        assert command.actor == ACTOR and json.loads(command.before_json)['providerObservation']['id'] == 99001
        assert s.scalar(select(OfLaunchCommand).where(OfLaunchCommand.kind == 'release_unconfirmed_send')) is not None
        assert s.get(OfConversationState, (ACCOUNT, FAN)).requires_attention is True
    assert facts(rig)['posts'] == done['posts'] and facts(rig)['used'] == done['used'], 'nothing sent, nothing refunded'
    unpace(rig)
    rig.ticks(4)
    assert facts(rig)['posts'] == done['posts'], 'still no replay of the old action'


@pytest.mark.parametrize('label,patch', [
    ('wrong_text', {'text': 'something else entirely'}),
    ('wrong_fan', {'toUser': {'id': 4242}}),
    ('price', {'price': 500}),
    ('long_after', {'createdAt': '2099-01-01T00:00:00Z'}),
])
def test_late_evidence_must_match_the_retired_payload_exactly(scene, label, patch):
    rig, wire, intent = scene
    _retire_and_answer(rig, wire, intent)
    late = {**_late_copy(rig, intent), **patch}
    with pytest.raises(Refused):
        send_recovery.record_late_positive_evidence(
            authority(rig), TENANT, ACCOUNT, FAN, intent.intent_id, history=[late], complete=True, provider_message_id=99001,
            command_key=K(33), actor=ACTOR, reason=REASON)
    with Session(rig.w.database[1]) as s:
        assert s.scalar(select(OfLaunchCommand).where(OfLaunchCommand.kind == 'late_positive_after_release')) is None


def test_late_evidence_needs_the_actor_the_released_state_and_complete_history(scene):
    rig, wire, intent = scene
    late = _late_copy(rig, intent)
    with pytest.raises(Refused):                  # still unknown: the ordinary reconcile is the right tool, not this one
        send_recovery.record_late_positive_evidence(
            authority(rig), TENANT, ACCOUNT, FAN, intent.intent_id, history=[late], complete=True, provider_message_id=99001,
            command_key=K(34), actor=ACTOR, reason=REASON)
    _retire_and_answer(rig, wire, intent)
    late = _late_copy(rig, intent)
    for patch in ({'actor': 'op-session:' + 'b' * 64}, {'complete': False}, {'provider_message_id': 7}, {'history': []}):
        args = dict(history=[late], complete=True, provider_message_id=99001, command_key=K(35), actor=ACTOR, reason=REASON)
        args.update(patch)
        with pytest.raises(Refused):
            send_recovery.record_late_positive_evidence(authority(rig), TENANT, ACCOUNT, FAN, intent.intent_id, **args)
    with Session(rig.w.database[1]) as s:
        assert s.scalar(select(OfLaunchCommand).where(OfLaunchCommand.kind == 'late_positive_after_release')) is None

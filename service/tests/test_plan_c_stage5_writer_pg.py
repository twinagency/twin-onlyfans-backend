"""Plan C Stage 5 (ordinary writing/context repairs): acceptance through the real chain.

Every test here goes storage/intake (a real inbound row) -> the actual rendered prompt (captured at the model client) ->
the real judge and engine (ModelRuntime, ledger, budgets) -> simulated authorized delivery (TextAuthority.dispatch through
the synthetic transport). For each repaired behaviour there is an ALLOWED reply that now goes out and the NEAREST FORBIDDEN
reply that is still stopped. Regex-level tables live in test_plan_c_stage5_diagnostic.py and are diagnostic only.
"""
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, event  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, unthrottle
from test_engine_model_pg import (BOUND, PROFILE, IDENT, _cases, config, ok, rows, run, system_of, with_model)
from test_engine_simulated_journey_pg import incoming
from conversation_engine.model_client import ChatResult, Usage
from conversation_engine.model_runtime import ModelRuntime
from conversation_engine.writer_context import CreatorProfile, PromptBudget, StaticCreatorProfiles
from db.models import OfConversationState
from fans_api_adapter.synthetic_transport import SyntheticFansProvider
from onlyfans_runtime.store import Refused


def profile(**kw):
    base = dict(name='Riley', voice='warm, teasing', facts=(('job', 'barista'),), limits=('no meetups',))
    base.update(kw)
    return CreatorProfile(**base)


def converse(database, script, fan_text, *, prof=None, mid=1, **kw):
    """One fan message -> real ModelRuntime -> delivery. Returns (store, authority, engine, client, provider, outcome)."""
    store, authority, engine, runtime, client, memory = with_model(
        database, script, profiles=StaticCreatorProfiles({ACCOUNT: prof or PROFILE}), **kw)
    incoming(store, mid, fan_text)
    provider = SyntheticFansProvider(message_id=9000 + mid)
    _, outcome = run(store, authority, engine, mid, provider)
    return store, authority, engine, runtime, client, provider, outcome


def sent_text(provider):
    return provider.calls[0]['body']['text']


def prompt_of(client, i=0):
    return client.requests[i].messages[1]['content']


def handed_over(database, script, fan_text, *, prof=None, calls=None, reason=None, **kw):
    """The nearest-forbidden side: nothing is delivered, the request is human work, no more calls than `calls`."""
    store, authority, engine, runtime, client, memory = with_model(
        database, script, profiles=StaticCreatorProfiles({ACCOUNT: prof or PROFILE}), **kw)
    incoming(store, 1, fan_text)
    with pytest.raises(Refused, match='handed to a human'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    if calls is not None:
        assert len(client.requests) == calls
    if reason is not None:
        [(category, _, _, why, evidence)] = _cases(database)
        assert (category, why) == ('model_handover', reason)
    return store, client


# ===================================================================================================================
# 1. Benign phrasing is not a refusal; a genuine refusal still goes to a human with no bypass retry
# ===================================================================================================================
@pytest.mark.parametrize('body', ['I understand you had a rough day.', "let me know if you like it babe",
                                  "that's so inappropriate haha", 'of course I do'])
def test_benign_empathy_and_style_is_delivered_not_handed_over(database, body):
    store, _, _, _, client, provider, outcome = converse(database, [ok(body)], 'i had a rough day, you there?')
    assert outcome == 'accepted' and sent_text(provider) == body and len(client.requests) == 1
    assert [r.outcome for r in rows(database)] == ['ok']
    store.close()


@pytest.mark.parametrize('body', ["I'm sorry, but I can't help with that.", 'This request is inappropriate. I cannot continue.',
                                  'As a language model I cannot continue', 'I must decline to write that.'])
def test_genuine_refusal_inside_valid_json_still_goes_to_a_human_with_no_bypass_retry(database, body):
    store, client = handed_over(database, [ok(body), ok('hey you, how was your day')], 'i had a rough day, you there?',
                                calls=1, reason='model_refused')
    assert [r.outcome for r in rows(database)] == ['refusal']   # never re-asked, never routed to another model
    store.close()


def test_assistant_sounding_style_gets_the_one_normal_correction_not_a_handover(database):
    store, _, _, _, client, provider, outcome = converse(
        database, [ok("i'm here to help babe, how can I assist you"), ok('so tell me about your day')], 'hey you')
    assert outcome == 'accepted' and sent_text(provider) == 'so tell me about your day'
    assert [r.outcome for r in rows(database)] == ['invalid', 'ok']
    assert 'sounds like an assistant' in prompt_of(client, 1)
    store.close()


def test_plain_text_empathy_that_is_not_json_is_malformed_with_one_correction_not_refusal(database):
    store, _, _, _, client, provider, outcome = converse(
        database, ['I understand you had a rough day.', ok('rough day? tell me everything')], 'i had a rough day')
    assert outcome == 'accepted' and [r.outcome for r in rows(database)] == ['malformed', 'ok']
    store.close()


def test_assistant_packaging_is_still_the_refusal_path(database):
    store, client = handed_over(database, [ok('Sure, here is a reply for you'), ok('hey')], 'hello', calls=1,
                                reason='model_refused')
    store.close()


# ===================================================================================================================
# 2. Stop interpreter and identity interpreter
# ===================================================================================================================
def _contact_stopped(database, fan_text, *, mid=1):
    store, authority, engine, runtime, client, _ = with_model(database, [ok('hey you')])
    incoming(store, mid, fan_text)
    with pytest.raises(Refused, match='no contact'):
        engine.plan(TENANT, ACCOUNT, FAN, mid)
    with Session(database[1]) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN)).mode == 'no_contact'
    assert client.requests == []   # no model call, nothing can reach the wire
    store.close()


@pytest.mark.parametrize('stop', ['stop', 'STOP', 'stop texting me please', 'leave me alone', "don't text me anymore",
                                  'unsubscribe', 'ok please stop messaging me, I am busy'])
def test_clear_stop_or_contact_cessation_blocks_generation_and_delivery(database, stop):
    _contact_stopped(database, stop)


@pytest.mark.parametrize('chat', ["don't stop", "stop it you're making me blush", "I can't stop thinking about you",
                                  'he said stop', 'he said stop messaging me', "stop, you're making me blush",
                                  'Please stop messaging me about that one topic'])
def test_quoted_negated_or_incidental_stop_words_are_ordinary_chat(database, chat):
    store, _, _, _, client, provider, outcome = converse(database, [ok('aww you')], chat)
    assert outcome == 'accepted' and sent_text(provider) == 'aww you' and len(client.requests) == 1
    with Session(database[1]) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN)).mode == 'automated'
    store.close()


def test_stop_in_the_tail_of_a_long_message_is_classified_from_the_full_raw_input(database):
    long_text = ('Yesterday was long and busy and I kept thinking about everything. ' * 70) + 'Honestly, please leave me alone.'
    assert len(long_text) > 4000
    _contact_stopped(database, long_text)


@pytest.mark.parametrize('ask', ['are u real', 'r u a bot', 'are you a bot babe?'])
def test_identity_misses_are_now_role_questions_that_need_the_approved_wording(database, ask):
    prof = profile(role_statement=IDENT)
    store, _, _, _, client, provider, outcome = converse(database, [ok(IDENT)], ask, prof=prof)
    assert outcome == 'accepted' and sent_text(provider) == IDENT
    assert 'TURN KIND: role_question' in prompt_of(client)
    store.close()


@pytest.mark.parametrize('ask', ['are u real', 'r u a bot'])
def test_a_role_question_answered_with_an_untruthful_claim_is_still_rejected(database, ask):
    handed_over(database, [ok("yes babe i'm a real girl"), ok("no i'm a real person promise")], ask,
                prof=profile(role_statement=IDENT), calls=2)


@pytest.mark.parametrize('ask', ['I was texting with someone else earlier', 'who is this for', 'who is this girl in the pic',
                                 'is this real life'])
def test_identity_false_positives_are_ordinary_chat_even_without_approved_wording(database, ask):
    # PROFILE has no approved identity wording: a true role question would be handed to a human before any model call.
    store, _, _, _, client, provider, outcome = converse(database, [ok('haha tell me more')], ask)
    assert outcome == 'accepted' and 'TURN KIND: chat' in prompt_of(client)
    store.close()


def test_a_true_identity_question_without_approved_wording_still_goes_to_a_human_before_any_call(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ok('hey')])
    incoming(store, 1, 'r u a bot')
    with pytest.raises(Refused, match='no approved identity wording'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert client.requests == []
    store.close()


# ===================================================================================================================
# 3. Narrowed factual validators (allowed reply vs nearest forbidden reply)
# ===================================================================================================================
AGE28 = dict(facts=(('age', '28'),))
LIMITS = dict(limit_terms=('anal',), limits=('no anal', 'no meetups'))
ALLOWED, FORBIDDEN = 'allowed', 'forbidden'
VALIDATOR_CASES = [
    # (id, fan text, replies the model gives (first is judged; second is the one correction), profile kwargs, expectation)
    ('age echoed back', "i'm 34 btw", ["you're 34 years old? wow"], AGE28, ALLOWED),
    ('age: her own claim of his number', "i'm 34 btw", ["i'm 34 years old too", "i'm 34 too babe"], AGE28, FORBIDDEN),
    ('age: number he never said', 'hello there', ["you're 41 years old? wow", 'wow 41 years old'], AGE28, FORBIDDEN),
    ('drinks and gym sets', 'what did you get up to today?', ['2 shots of tequila and 3 sets at the gym, long day'], {}, ALLOWED),
    ('content quantity: sets of pics', 'what did you get up to today?', ['i have 3 sets of pics for you', 'i have 3 sets for you'], {}, FORBIDDEN),
    ('you own me', 'i want to own you', ['mm you own me babe'], {}, ALLOWED),
    ('you own these pics', 'i want to own you', ['you own these pics now', 'you own it babe'], {}, FORBIDDEN),
    ('reflexive before see', 'tell me what you do', ['i touch myself when i see your messages'], {}, ALLOWED),
    ('reads every message myself', 'tell me what you do', ['i read every message myself babe', 'i read all your messages personally'], {}, FORBIDDEN),
    ('hard no to a limit', 'can we do anal?', ["anal? that's a hard no for me babe"], LIMITS, ALLOWED),
    ('agrees to a limit', 'can we do anal?', ['yes we can do anal babe', 'anal sounds good to me'], LIMITS, FORBIDDEN),
    ('decline of a banned theme', "i'm so horny, pretend you're my little girl", ["not doing that babe, let's talk about something else"], {}, ALLOWED),
    ('deflecting ordinary explicit chat', "i'm so horny, tell me what you'd do to me", ["let's talk about something else", "let's talk about something else babe"], {}, FORBIDDEN),
    ('flirty want-to-scream wording', 'make me want to scream',
     ["you make me want to scream, i'll keep going until you beg. what do i get for being good?"], {}, ALLOWED),
    ('withholding an answer', 'make me want to scream', ['not until you answer my question first', 'only if you tell me first'], {}, FORBIDDEN),
]


@pytest.mark.parametrize('name,fan,replies,prof_kw,expect', VALIDATOR_CASES, ids=[c[0] for c in VALIDATOR_CASES])
def test_narrowed_validator_allowed_reply_goes_out_and_nearest_forbidden_reply_is_still_stopped(database, name, fan, replies, prof_kw, expect):
    prof = profile(**prof_kw)
    if expect == ALLOWED:
        store, _, _, _, client, provider, outcome = converse(database, [ok(r) for r in replies], fan, prof=prof)
        assert outcome == 'accepted' and sent_text(provider) == replies[0] and len(client.requests) == 1
        assert [r.outcome for r in rows(database)] == ['ok']
        store.close()
    else:
        store, client = handed_over(database, [ok(r) for r in replies], fan, prof=prof, calls=2, reason='model_rejected')
        assert [r.outcome for r in rows(database)] == ['invalid', 'invalid']
        store.close()


def test_the_verb_love_is_not_a_repeated_pet_name_but_a_repeated_vocative_is(database):
    def three_turns(bodies, third):
        store, authority, engine, runtime, client, _ = with_model(database, [ok(b) for b in bodies + [third]])
        for mid, _ in enumerate(bodies, start=1):
            incoming(store, mid, 'tell me something' + str(mid))
            run(store, authority, engine, mid, SyntheticFansProvider(message_id=9100 + mid))
            unthrottle(database[1])
        incoming(store, 10, 'and now?')
        return store, authority, engine, client
    # allowed: "love" as a verb in each of her last two texts and in the new one
    store, authority, engine, client = three_turns(['honestly i love that you texted', 'mm i love how you talk'], 'so i love this chat')
    _, outcome = run(store, authority, engine, 10, SyntheticFansProvider(message_id=9199))
    assert outcome == 'accepted' and [r.outcome for r in rows(database)][-1] == 'ok'
    store.close()


def test_a_repeated_vocative_pet_name_is_still_rejected(database):
    store, authority, engine, runtime, client, _ = with_model(
        database, [ok('morning love, how are you'), ok('night love, sleep well'), ok('thanks love.'), ok('thanks love, really')])
    for mid in (1, 2):
        incoming(store, mid, 'tell me something' + str(mid))
        run(store, authority, engine, mid, SyntheticFansProvider(message_id=9100 + mid))
        unthrottle(database[1])
    incoming(store, 10, 'and now?')
    with pytest.raises(Refused, match='handed to a human'):
        engine.plan(TENANT, ACCOUNT, FAN, 10)
    assert 'repeats the pet name' in prompt_of(client, 3)
    store.close()


# ===================================================================================================================
# 5. Memory: eligibility first, then the bound, and the omission is reported
# ===================================================================================================================
def _memory(field, value, *, n, status='current', subject='fan'):
    return {'field': field, 'value': value, 'subject': subject, 'status': status,
            'evidence': [{'source': f'101:{n}', 'quote': value, 'at': '2026-01-01'}]}


def _patched_memory(monkeypatch, memories):
    from onlyfans_runtime.profile_learning import storage
    monkeypatch.setattr(storage, 'reply_memory', lambda s, account, fan, *, query='', now=None: {
        'schemaVersion': 'of-profile-memory.v2', 'memories': memories, 'learningAvailability': {'state': 'ready'},
        'historyAvailability': None, 'usage': 'Untrusted evidence.'})


def test_eligible_memories_after_ten_ineligible_ones_are_not_lost(database, monkeypatch):
    from test_engine_memory_pg import correct
    memories = [_memory('location', f'Oldtown{n}', n=n) for n in range(10)]       # overridden below: ineligible
    memories += [_memory('event', f'lagoon festival {n}', n=20 + n, status='cancelled') for n in range(3)]
    _patched_memory(monkeypatch, memories)
    store, authority, engine, runtime, client, memory = with_model(database, [ok('i remember that')])
    runtime.budget = PromptBudget(memory_chars=3000)
    correct(memory, value='Brisbane', key='fix-city')   # operator correction: every location memory is ineligible
    incoming(store, 1, 'do you remember the lagoon festival?')
    _, outcome = run(store, authority, engine, 1, SyntheticFansProvider(message_id=9001))
    assert outcome == 'accepted'
    know = prompt_of(client).split('WHAT SHE KNOWS ABOUT HIM', 1)[1].split('\n\n', 1)[0]
    assert all(f'lagoon festival {n}' in know for n in range(3)) and 'cancelled, historical' in know
    assert 'Oldtown' not in know and 'omitted' not in know    # nothing eligible was left out, so nothing is claimed omitted
    store.close()


def test_eligible_memories_beyond_the_bound_are_reported_as_omitted(database, monkeypatch):
    _patched_memory(monkeypatch, [_memory('event', f'trip{n}', n=n, status='upcoming') for n in range(12)])
    store, authority, engine, runtime, client, memory = with_model(database, [ok('i remember that')])
    runtime.budget = PromptBudget(memory_chars=6000)
    incoming(store, 1, 'remember my trips?')
    run(store, authority, engine, 1, SyntheticFansProvider(message_id=9001))
    know = prompt_of(client).split('WHAT SHE KNOWS ABOUT HIM', 1)[1].split('\n\n', 1)[0]
    assert know.count('source 101:') == 10 and 'trip9' in know and 'trip10' not in know
    assert 'Some memory details omitted by budget; retain uncertainty and do not infer absence.' in know
    store.close()


# ===================================================================================================================
# 6. Latest input: no silent clipping; explicit oversized outcome; full raw input for controls
# ===================================================================================================================
def test_long_latest_message_keeps_its_trailing_correction_and_question_with_an_explicit_marker(database):
    fan = ('Yesterday was busy. ' * 80) + 'Correction: My name is Morgan, not Alex. What is your favorite color?'
    assert len(fan) > 1200
    store, _, _, _, client, provider, outcome = converse(database, [ok('morgan, i like purple. what about you')], fan)
    assert outcome == 'accepted'
    latest = prompt_of(client).split('LATEST:\nFAN: ', 1)[1].split('\n\nTHIS TURN:', 1)[0]
    assert 'My name is Morgan, not Alex. What is your favorite color?' in latest and latest.startswith('Yesterday was busy.')
    assert 'characters omitted from the middle of this long message' in latest and len(latest) <= 1200
    store.close()


def test_a_message_that_fits_the_budget_is_shown_unchanged_without_a_marker(database):
    store, _, _, _, client, provider, outcome = converse(database, [ok('hey')], 'short question: what is your favorite color?')
    latest = prompt_of(client).split('LATEST:\nFAN: ', 1)[1].split('\n\nTHIS TURN:', 1)[0]
    assert latest == 'short question: what is your favorite color?'
    store.close()


def test_a_three_thousand_character_message_is_answered_not_silently_stalled_by_the_catalogue_query_bound(database):
    fan = ('I have been thinking about this for a while and wanted to say it properly. ' * 40) + 'So, what is your favorite color?'
    assert 2000 < len(fan.encode()) < 6000
    store, _, _, _, client, provider, outcome = converse(database, [ok('purple babe, what about you')], fan)
    assert outcome == 'accepted'
    assert 'What is' not in prompt_of(client) and 'what is your favorite color?' in prompt_of(client).split('LATEST:', 1)[1]
    store.close()


def test_oversized_latest_message_is_an_explicit_human_handover_with_no_call_and_no_invented_answer(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ok('hey')])
    incoming(store, 1, 'I need to tell you everything about my week. ' * 200)     # about 9000 characters
    with pytest.raises(Refused, match='too long to answer automatically'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert client.requests == [] and rows(database) == []
    [(category, _, _, why, evidence)] = _cases(database)
    assert (category, why) == ('model_handover', 'latest_input_oversized') and evidence['chars'] > 6000
    store.close()


# ===================================================================================================================
# 7. Bursts through the existing request records; media-only inbound
# ===================================================================================================================
def test_burst_where_only_the_newest_message_is_planned_still_shows_every_earlier_unanswered_message(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ok('purple, and yes i do')])
    incoming(store, 1, 'hey, how was your day?')
    incoming(store, 2, 'what is your favorite color?')
    incoming(store, 3, 'also do you like coffee?')
    with pytest.raises(Refused, match='newer incoming'):      # the worker's old request is superseded by newer input
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    _, outcome = run(store, authority, engine, 3, SyntheticFansProvider(message_id=9003))
    assert outcome == 'accepted' and len(client.requests) == 1
    prompt = prompt_of(client)
    unresolved = prompt.split('UNRESOLVED REQUESTS AND ACTIONS', 1)[1]
    assert 'UNANSWERED {"message_id": 1, "text": "hey, how was your day?"}' in unresolved
    assert 'UNANSWERED {"message_id": 2, "text": "what is your favorite color?"}' in unresolved
    assert 'UNANSWERED lines are his earlier messages' in unresolved
    store.close()


def test_burst_where_each_message_has_an_open_request_shows_them_as_requests_not_duplicates(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ok('one'), ok('two'), ok('three')])
    for mid, text in ((1, 'first question?'), (2, 'second question?'), (3, 'third question?')):
        incoming(store, mid, text)
        engine.plan(TENANT, ACCOUNT, FAN, mid)     # planned, never delivered: the requests stay open
        unthrottle(database[1])
    unresolved = prompt_of(client, 2).split('UNRESOLVED REQUESTS AND ACTIONS', 1)[1]
    assert '"source_text": "first question?"' in unresolved and '"source_text": "second question?"' in unresolved
    assert 'UNANSWERED' not in unresolved
    store.close()


def test_a_burst_larger_than_the_unanswered_bound_defers_the_oldest_explicitly(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ok('ok, one at a time babe')])
    for mid in range(1, 9):
        incoming(store, mid, f'question number {mid}?')
    run(store, authority, engine, 8, SyntheticFansProvider(message_id=9008))
    unresolved = prompt_of(client).split('UNRESOLVED REQUESTS AND ACTIONS', 1)[1]
    assert unresolved.count('UNANSWERED {') == 5 and 'question number 7?' in unresolved and 'question number 2?' not in unresolved
    assert 'Additional unresolved records omitted' in unresolved
    store.close()


def test_messages_she_already_answered_are_not_listed_as_unanswered(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ok('first answer'), ok('second answer')])
    incoming(store, 1, 'hello?')
    run(store, authority, engine, 1, SyntheticFansProvider(message_id=9001))
    unthrottle(database[1])
    incoming(store, 2, 'and another thing?')
    run(store, authority, engine, 2, SyntheticFansProvider(message_id=9002))
    assert 'UNANSWERED' not in prompt_of(client, 1)
    store.close()


def test_media_only_inbound_is_an_attachment_fact_not_empty_and_not_a_description(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ok('mm what did you send me, tell me about it')])
    assert store.record(TENANT, ACCOUNT, event(mid=1, fan=FAN, body='', media_ids=(77,))) in {'accepted', 'duplicate'}
    store.drain(TENANT, ACCOUNT)
    provider = SyntheticFansProvider(message_id=9001)
    _, outcome = run(store, authority, engine, 1, provider)
    assert outcome == 'accepted'      # an empty text with media neither crashes nor holds the conversation
    latest = prompt_of(client).split('LATEST:\nFAN: ', 1)[1].split('\n\nTHIS TURN:', 1)[0]
    assert latest == '[attachment only: 1 file; contents not visible to you, do not guess what it shows]'
    store.close()


def test_earlier_media_only_message_appears_in_history_as_an_attachment_fact(database):
    store, authority, engine, runtime, client, _ = with_model(database, [ok('nice one')])
    assert store.record(TENANT, ACCOUNT, event(mid=1, fan=FAN, body='', media_ids=(77, 78))) in {'accepted', 'duplicate'}
    store.drain(TENANT, ACCOUNT)
    incoming(store, 2, 'did you get that?')
    run(store, authority, engine, 2, SyntheticFansProvider(message_id=9002))
    prompt = prompt_of(client)
    history = prompt.split('\n\nLATEST:\n', 1)[0]
    assert 'FAN: [attachment only: 2 files; contents not visible to you' in history
    store.close()


# ===================================================================================================================
# 8. finish_reason length / incomplete: one bounded, costed regeneration; empty and refused stay as they were
# ===================================================================================================================
def result(text, finish):
    return ChatResult(text=text, usage=Usage(prompt_tokens=100, completion_tokens=40, cost_microusd=1000), model='m/primary',
                      finish_reason=finish)


def test_a_length_cut_off_answer_gets_exactly_one_costed_regeneration_and_is_then_delivered(database):
    store, _, _, runtime, client, provider, outcome = converse(
        database, [result('{"body": "hey ba', 'length'), ok('hey you, how are you')], 'hello')
    assert outcome == 'accepted' and sent_text(provider) == 'hey you, how are you' and len(client.requests) == 2
    assert [r.outcome for r in rows(database)] == ['invalid', 'ok']
    assert 'cut off by the output length limit' in prompt_of(client, 1)
    assert all(r.reserved_microusd > 0 for r in rows(database)) and runtime.ledger.committed(day=rows(database)[0].cost_day) == 2000
    store.close()


def test_an_empty_completion_that_hit_the_length_limit_is_retried_once(database):
    store, _, _, _, client, provider, outcome = converse(database, [result('', 'length'), ok('hey you')], 'hello')
    assert outcome == 'accepted' and [r.outcome for r in rows(database)] == ['invalid', 'ok']
    store.close()


def test_a_plain_empty_completion_is_still_never_re_asked(database):
    store, client = handed_over(database, [result('', 'stop'), ok('hey you')], 'hello', calls=1, reason='model_empty')
    assert [r.outcome for r in rows(database)] == ['empty']
    store.close()


def test_two_length_cut_offs_are_bounded_and_become_human_work(database):
    store, client = handed_over(database, [result('{"body": "hey', 'length'), result('{"body": "hello', 'length'), ok('hey')],
                                'hello', calls=2, reason='model_rejected')
    assert [r.outcome for r in rows(database)] == ['invalid', 'invalid']
    store.close()


def test_a_refusal_is_not_turned_into_a_retry_because_it_was_also_cut_off(database):
    store, client = handed_over(database, [result("I'm sorry, but I can't help with that, it would", 'length'), ok('hey')],
                                'hello', calls=1, reason='model_refused')
    assert [r.outcome for r in rows(database)] == ['refusal']
    store.close()

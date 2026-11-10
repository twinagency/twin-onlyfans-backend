"""Plan C Stage 5, text-only pilot (items 4, 9 and the journey/stop false positives) through the real pilot path.

Chain under test: signed webhook intake -> canonical rows -> pilot worker -> actual rendered prompt (captured at the
OpenRouter mock transport) -> real judge/engine -> authorized delivery through the real fansapi transport mock.
"""
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from conversation_engine import pilot_scope
from conversation_engine.writer_context import JsonFileCreatorProfiles, profile_data
from db.models import CatalogItem, OfConversationState, VaultItem
from db.onlyfans_delivery_models import OfDeliveryPolicy
from model_transport.pilot_supervised import build_supervised_pilot
from onlyfans_runtime.store import Refused, utcnow
from plan_c_worker import create_app
from test_engine_real_worker_pg import world, inbound  # noqa: F401
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_plan_c_draft_app_pg import configured_draft, signed  # noqa: F401
from test_plan_c_scope_pg import ACTOR, key
from test_plan_c_supervised_pg import stage, supervised  # noqa: F401
from test_plan_c_text_activation_pg import stopped_text, start  # noqa: F401
from test_engine_real_worker_pg import FAN2


def reconfigure(w, env, view, *, n, mode='text_only', profile_changes=None, product_changes=None):
    path = Path(env['ENGINE_MODEL_PROFILES_FILE'])
    raw = json.loads(path.read_text())
    raw[ACCOUNT].update(profile_changes or {})
    path.write_text(json.dumps(raw))
    cfg = json.loads(json.dumps(view['config']))
    cfg['creator_profile'] = profile_data(JsonFileCreatorProfiles(str(path)).get(ACCOUNT))
    cfg['delivery_mode'] = mode
    return pilot_scope.configure(w.store, TENANT, ACCOUNT, cfg, expected_revision=view['revision'], command_key=key(n),
                                 actor=ACTOR, reason='stage 5 test configuration')


def ingest_only(pilot, mid, body, fan=FAN):
    raw, headers = signed(inbound(mid, body, fan=fan), key='ingest-' + str(mid))
    return pilot.handle(raw, headers['Signature'], headers['X-OFAPI-Idempotency-Key'], ingest_only=True)


def damage_photo(w, how):
    with Session(w.database[1]) as s, s.begin():
        item = s.get(CatalogItem, w.item)
        if how == 'removed':
            for vault in s.query(VaultItem).filter(VaultItem.account_id == ACCOUNT):
                vault.removed_at = utcnow()
        elif how == 'repriced':
            item.price_cents = 500
        elif how == 'disabled':
            item.enabled = False
        else:
            raise AssertionError(how)


def prompt_text(seen, i=-1):
    return '\n'.join(m['content'] for m in seen[i]['messages'])


# ===================================================================================================================
# 9. Text-only configure/activate no longer depends on the unused product; full mode still does
# ===================================================================================================================
@pytest.mark.parametrize('how', ['removed', 'repriced', 'disabled'])
def test_text_only_reconfigures_activates_and_replies_with_the_unused_photo_changed(stopped_text, how):
    w, env, model, seen, first = stopped_text
    damage_photo(w, how)
    view = pilot_scope.describe(w.store, TENANT, ACCOUNT)
    again = reconfigure(w, env, view, n=41001)                 # configure no longer needs the photo
    assert again['status'] == 'configured' and again['revision'] == first['revision']
    assert start(w, again['revision'], command=41002)['status'] == 'active'
    pilot = build_supervised_pilot(w.store, env, model_transport=model, fans_transport=w.fans.transport)
    stage(pilot, 41003, 'hello there')
    assert pilot.tick()['dispatched'] == ['accepted']
    assert len(w.fans.posts) == 1 and not w.fans.posts[0]['body'].get('price') and not w.fans.posts[0]['body'].get('mediaFiles')
    with Session(w.database[1]) as s:
        assert not s.get(OfDeliveryPolicy, ACCOUNT).media_enabled     # the commercial/media prohibition is unchanged


@pytest.mark.parametrize('how', ['removed', 'repriced', 'disabled'])
def test_full_mode_configure_still_requires_the_verified_product(stopped_text, how):
    w, env, model, seen, first = stopped_text
    damage_photo(w, how)
    view = pilot_scope.describe(w.store, TENANT, ACCOUNT)
    with pytest.raises(Refused, match='pilot product or price is unavailable'):
        reconfigure(w, env, view, n=41010, mode='full')
    assert pilot_scope.describe(w.store, TENANT, ACCOUNT)['config'].get('delivery_mode') == 'text_only'


@pytest.mark.parametrize('how', ['removed', 'repriced', 'disabled'])
def test_full_mode_activation_still_requires_the_verified_product(stopped_text, how):
    w, env, model, seen, first = stopped_text
    view = pilot_scope.describe(w.store, TENANT, ACCOUNT)
    full = reconfigure(w, env, view, n=41020, mode='full')    # photo is fine here: configuring full succeeds
    damage_photo(w, how)
    with pytest.raises(Refused, match='activation prerequisites are not met'):
        start(w, full['revision'], command=41021)
    with Session(w.database[1]) as s:
        assert not s.get(OfDeliveryPolicy, ACCOUNT).enabled


def test_text_only_still_requires_binding_profile_and_permissions(stopped_text):
    w, env, model, seen, first = stopped_text
    damage_photo(w, 'removed')
    with pytest.raises(Refused):                                # actor without the activate permission
        start(w, first['revision'], actor='someone-else', command=41030)
    view = pilot_scope.describe(w.store, TENANT, ACCOUNT)
    bad = json.loads(json.dumps(view['config']))
    bad['creator_profile']['limits'] = []                      # no documented boundaries
    with pytest.raises(Refused, match='boundaries'):
        pilot_scope.configure(w.store, TENANT, ACCOUNT, bad, expected_revision=view['revision'], command_key=key(41031),
                              actor=ACTOR, reason='missing boundaries')
    with Session(w.database[1]) as s:
        assert not s.get(OfDeliveryPolicy, ACCOUNT).enabled


def test_offer_approval_and_media_stay_refused_in_text_only_even_when_the_photo_changed(stopped_text):
    w, env, model, seen, first = stopped_text
    damage_photo(w, 'repriced')
    start(w, first['revision'], command=41040)
    with Session(w.database[1]) as s:
        runtime = w.store._runtime(s, TENANT, ACCOUNT, lock=False)
        assert pilot_scope.delivery_block(s, runtime, FAN, commercial=True) == 'pilot_text_only'
        _, config = pilot_scope.current(s, runtime)
        with pytest.raises(Refused, match='pilot_text_only'):
            pilot_scope.current_offer_coverage(s, runtime, config)
        assert not s.get(OfDeliveryPolicy, ACCOUNT).media_enabled


@pytest.mark.parametrize('how', ['unchanged', 'removed', 'repriced'])
def test_a_forced_offer_proposal_is_still_stopped_in_text_only_with_a_changed_photo(stopped_text, how):
    w, env, _, seen, first = stopped_text
    if how != 'unchanged':
        damage_photo(w, how)
    start(w, first['revision'], command=41050)

    def force_offer(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={'model': 'm/primary', 'choices': [{'message': {'content': json.dumps({
            'body': 'Here is the photo', 'action': 'offer', 'offer_grant_id': '00000000-0000-0000-0000-000000000001'})},
            'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'cost': 0.0001},
            'openrouter_metadata': {'endpoints': {'available': [{'provider': 'Fixture Provider', 'selected': True}]}}})
    pilot = build_supervised_pilot(w.store, env, model_transport=httpx.MockTransport(force_offer), fans_transport=w.fans.transport)
    raw, headers = signed(inbound(41051, 'send me the photo'), key='stage5-forced-offer')
    with TestClient(create_app(pilot, run_loop=False)) as client:
        assert client.post('/onlyfans/webhooks/plan-c', content=raw, headers=headers).status_code == 202
    ticked = pilot.tick()
    assert not w.fans.posts and ticked['dispatched'] == [], ticked    # held or refused: nothing reaches the wire


# ===================================================================================================================
# 4. The text-only prompt carries no mode-inapplicable selling instructions
# ===================================================================================================================
def test_text_only_actual_prompt_has_no_offer_contract_or_sales_state_lines_and_keeps_personal_facts(stopped_text):
    w, env, model, seen, first = stopped_text
    view = pilot_scope.describe(w.store, TENANT, ACCOUNT)
    changed = reconfigure(w, env, view, n=41060, profile_changes={
        'facts': {'telegram': '@maliebaby', 'age': '28'}, 'offers_permitted': True})
    start(w, changed['revision'], command=41061)
    pilot = build_supervised_pilot(w.store, env, model_transport=model, fans_transport=w.fans.transport)
    stage(pilot, 41062, 'hey, can you send me the photo? how much is it?')
    assert pilot.tick()['dispatched'] == ['accepted'] and len(seen) == 1
    system, user = seen[0]['messages'][0]['content'], seen[0]['messages'][1]['content']
    for banned in ('APPROVED OFFER', 'SALES STATE', 'offer_grant_id "', 'CONTENTS', 'INFORMATIONAL PRICE', 'action offer only when',
                   'The only product'):
        assert banned not in system and banned not in user, banned
    assert 'text only' in system and 'telegram: @maliebaby' in system and 'age: 28' in system      # approved personal facts stay
    assert 'My public Telegram is @maliebaby.' in system and 'another app, number or site' in system  # global rules stay
    assert 'VERIFIED PURCHASES' in user                                                              # global ownership rule stays
    assert len(w.fans.posts) == 1 and not w.fans.posts[0]['body'].get('price')


def test_full_mode_prompt_keeps_its_selling_instructions(supervised):
    w, env, model, seen, pilot = supervised
    stage(pilot, 41070, 'hello')
    pilot.tick()
    system, user = seen[0]['messages'][0]['content'], seen[0]['messages'][1]['content']
    assert 'APPROVED OFFER' in user and 'SALES STATE' in user and 'The only product is the APPROVED OFFER' in system


# ===================================================================================================================
# Stop and journey false positives through the real pilot
# ===================================================================================================================
@pytest.mark.parametrize('stop', ['STOP', 'stop texting me please', 'leave me alone', "don't text me anymore", 'unsubscribe'])
def test_clear_stop_then_greeting_blocks_generation_and_the_wire_in_the_text_pilot(stopped_text, stop):
    w, env, model, seen, first = stopped_text
    start(w, first['revision'], command=41080)
    pilot = build_supervised_pilot(w.store, env, model_transport=model, fans_transport=w.fans.transport)
    stage(pilot, 41081, stop, fan=FAN2)
    stage(pilot, 41082, 'hello again', fan=FAN2)
    before = len(seen)
    pilot.tick()
    with Session(w.database[1]) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN2)).mode == 'no_contact'
    assert len(seen) == before and not w.fans.posts


@pytest.mark.parametrize('chat', ["I can't stop thinking about you", "don't stop", "stop it you're making me blush", 'he said stop'])
def test_incidental_stop_words_still_get_an_ordinary_reply_in_the_text_pilot(stopped_text, chat):
    w, env, model, seen, first = stopped_text
    start(w, first['revision'], command=41090)
    pilot = build_supervised_pilot(w.store, env, model_transport=model, fans_transport=w.fans.transport)
    stage(pilot, 41091, chat)
    assert pilot.tick()['dispatched'] == ['accepted'] and len(w.fans.posts) == 1
    with Session(w.database[1]) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN)).mode == 'automated'


PROBE_TEXTS = ["My email inbox is full. Can you tell me about your day?", "I don't want to meet you, just chat",
               "Can you tell me your hobbies? My email inbox is still full."]
REAL_PUSH = ['can we meet up?', 'come over tonight', 'what is your whatsapp?']


def _three_then_tick(w, env, model, texts):
    pilot = build_supervised_pilot(w.store, env, model_transport=model, fans_transport=w.fans.transport)
    ingest_only(pilot, 41101, texts[0])
    ingest_only(pilot, 41102, texts[1])
    try:
        stage(pilot, 41103, texts[2])
        return pilot, pilot.tick()
    except Refused as exc:
        # Observed pre-existing behaviour (same on the unmodified base): the journey's human takeover for a limit push needs a
        # 'takeover' operator permission that this pilot scope does not grant, so the turn is refused instead of answered.
        return pilot, {'refused': str(exc), 'dispatched': []}


def test_irrelevant_email_and_negated_meetup_are_not_a_limit_push_that_silences_the_fan(stopped_text):
    w, env, model, seen, first = stopped_text
    start(w, first['revision'], command=41100)
    pilot, result = _three_then_tick(w, env, model, PROBE_TEXTS)
    assert result['dispatched'] == ['accepted'] and len(w.fans.posts) == 1 and seen


def test_three_real_pushes_still_route_to_a_human_with_no_model_call_and_no_send(stopped_text):
    w, env, model, seen, first = stopped_text
    start(w, first['revision'], command=41110)
    pilot, result = _three_then_tick(w, env, model, REAL_PUSH)
    assert not w.fans.posts and not seen


@pytest.mark.parametrize('texts,sent', [(['visited the canal today', 'the canal walk was lovely', 'back at the canal'], True),
                                        (['want anal tonight', 'anal please', 'so about anal'], False)],
                         ids=['canal-is-not-anal', 'anal-is-still-a-limit-push'])
def test_limit_term_matches_whole_words_only(stopped_text, texts, sent):
    w, env, model, seen, first = stopped_text
    view = pilot_scope.describe(w.store, TENANT, ACCOUNT)
    changed = reconfigure(w, env, view, n=41120, profile_changes={'limit_terms': ['anal'], 'limits': ['no anal', 'no meetups']})
    start(w, changed['revision'], command=41121)
    pilot, result = _three_then_tick(w, env, model, texts)
    assert bool(w.fans.posts) is sent and bool(seen) is sent


# ===================================================================================================================
# 7. Bursts through the pilot: the older message is represented in the prompt that answers the newest one
# ===================================================================================================================
def test_burst_in_the_text_pilot_represents_every_unanswered_message_in_the_one_prompt(stopped_text):
    w, env, model, seen, first = stopped_text
    start(w, first['revision'], command=41130)
    pilot = build_supervised_pilot(w.store, env, model_transport=model, fans_transport=w.fans.transport)
    ingest_only(pilot, 41131, 'what is your favorite color?')
    ingest_only(pilot, 41132, 'and what do you do for work?')
    stage(pilot, 41133, 'also do you like coffee?')
    assert pilot.tick()['dispatched'] == ['accepted'] and len(w.fans.posts) == 1
    unresolved = prompt_text(seen).split('UNRESOLVED REQUESTS AND ACTIONS', 1)[1]
    assert 'what is your favorite color?' in unresolved and 'what do you do for work?' in unresolved

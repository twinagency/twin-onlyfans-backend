"""W1 review F3 (G09): stored refusal evidence must never keep secret-shaped values or echoed prompt text.
Copied from 22fbbe79 (F3 cases only; F1/F2/F4/F5 belong to other fix groups). Real scheduler path for the ledger
case (SupervisedPilot.tick -> ModelRuntime.propose -> OpenRouterClient over httpx.MockTransport). Synthetic data.
"""
import json

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.onlyfans_engine_models import OfEngineModelCall
from test_engine_real_worker_pg import FAN2, inbound, world  # noqa: F401
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_plan_c_draft_app_pg import configured_draft, signed  # noqa: F401
from test_plan_c_model_retry_due_pg import make_world, post  # noqa: F401
from test_plan_c_text_activation_pg import stopped_text  # noqa: F401

# Built at runtime so no key-shaped literal sits in the diff (REVIEW #1).
FAKE_KEY = 'sk-or-v1-' + 'ab12' * 16


def _detail(world):
    with Session(world.w.database[1]) as s:
        return [r.failure_detail_json for r in s.scalars(select(OfEngineModelCall))]


def _resp(body, headers=None):
    return httpx.Response(429, content=json.dumps(body).encode(), headers=headers or {})


def _failure_detail(response):
    """Lazy: a fix may rename/move the private sanitizer; only these unit probes should then need updating."""
    from model_transport.openrouter import _failure_detail as sanitize
    return sanitize(response)


_SHAPES = [FAKE_KEY, 'sk-abc', 'Bearer x', 'bearer short', 'api_key=short', 'api_key=' + 'q' * 20,
           'eyJhbGciOi.eyJzdWIi.c2ln', 'A' * 20 + '\t' + 'B' * 20, 'hello, how was your day?',
           'hello how was your day']


def _probe(value):
    return _failure_detail(_resp({'error': {'code': value, 'type': value, 'message': value,
                                            'metadata': {'provider_name': value}}},
                                 {'x-request-id': value, 'x-ratelimit-reset': value, 'x-ratelimit-limit': value,
                                  'x-ratelimit-remaining': value, 'retry-after': value}))


def test_f3_failure_detail_redacts_every_field():
    # Every secret / prompt shape in every field (G09 Flash): only presence markers survive, never the text.
    # Safe messages only: a regression must not print the probe into the test log.
    for index, value in enumerate(_SHAPES):
        detail = _probe(value)
        kept = sorted(k for k, v in detail.items() if k != 'status' and v != '[redacted]')
        assert kept == [], f'shape {index}: fields kept verbatim {kept}'
        assert detail['errorCode'] == detail['errorType'] == detail['requestId'] == '[redacted]', f'shape {index}'


def test_f3_failure_detail_redaction_survives_character_stripping():
    split = 'A' * 20 + '\t' + 'B' * 20  # stripping the tab after redaction would rejoin a 40-char token
    text = json.dumps(_probe(split))
    assert 'A' * 20 not in text and 'B' * 20 not in text, 'split token half kept'


def test_f3_failure_detail_never_keeps_echoed_prompt_text():
    fan_text = 'hello, how was your day?'  # module docstring: "never the key, the prompt or a raw body"
    assert 'how was your day' not in json.dumps(_failure_detail(_resp({'error': f'Invalid input: {fan_text}'}))), \
        'string error kept'
    assert 'how was your day' not in json.dumps(_probe(fan_text)), 'prompt kept in a field'


def test_g09_real_shaped_ids_and_enums_are_kept():
    # G09 DA 1: a UUID / long provider request id is the operator's only handle on a refused call.
    for rid in ('3f2b1c9e-8a7d-4e6f-9b0a-1c2d3e4f5a6b', 'gen-1731234567-AbCdEfGhIjKlMnOpQrSt', 'a' * 32):
        assert _failure_detail(_resp({}, {'x-request-id': rid}))['requestId'] == rid
    detail = _failure_detail(_resp({'error': {'code': 429, 'type': 'rate_limit_exceeded', 'message': 'slow down',
                                              'metadata': {'provider_name': 'Google AI Studio'}}},
                                   {'retry-after': '7', 'x-ratelimit-reset': '1731234567000'}))
    assert detail == {'status': 429, 'retryAfter': '7', 'rateLimitReset': '1731234567000', 'errorCode': 429,
                      'errorType': 'rate_limit_exceeded', 'errorMessage': '[redacted]',
                      'providerName': 'Google AI Studio'}


def test_f3_header_secret_is_not_persisted_through_the_ledger(make_world):
    world = make_world([(429, {'x-request-id': FAKE_KEY})])
    post(world, 97003)
    world.ticks(1)
    details = _detail(world)
    assert len(details) == 1 and details[0] is not None, f'{len(details)} ledger rows'
    stored = details[0] if type(details[0]) is str else json.dumps(details[0])
    assert 'sk-or-v1' not in stored and json.loads(stored).get('requestId') == '[redacted]', 'key persisted'

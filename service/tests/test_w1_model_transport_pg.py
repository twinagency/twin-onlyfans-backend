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


def test_f3_failure_detail_redacts_every_field():
    detail = _failure_detail(_resp({'error': {'code': FAKE_KEY, 'type': FAKE_KEY, 'message': 'm',
                                              'metadata': {'provider_name': FAKE_KEY}}},
                                   {'x-request-id': FAKE_KEY, 'x-ratelimit-reset': FAKE_KEY}))
    assert 'sk-or-v1' not in json.dumps(detail), detail


def test_f3_failure_detail_redaction_survives_character_stripping():
    split = 'A' * 20 + '\t' + 'B' * 20  # stripping the tab after redaction rejoins a 40-char token
    detail = _failure_detail(_resp({'error': {'message': 'tok ' + split}}))
    assert 'A' * 20 + 'B' * 20 not in json.dumps(detail), detail


def test_f3_failure_detail_never_keeps_echoed_prompt_text():
    fan_text = 'hello, how was your day?'  # module docstring: "never the key, the prompt or a raw body"
    detail = _failure_detail(_resp({'error': {'message': f'Invalid input: {fan_text}'}}))
    assert fan_text not in json.dumps(detail), detail


def test_f3_header_secret_is_not_persisted_through_the_ledger(make_world):
    world = make_world([(429, {'x-request-id': FAKE_KEY})])
    post(world, 97003)
    world.ticks(1)
    assert all('sk-or-v1' not in json.dumps(d) for d in _detail(world)), _detail(world)

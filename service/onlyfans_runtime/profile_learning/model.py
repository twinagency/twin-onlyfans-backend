"""Budgeted offline analysis using only this account's saved model credential.

OnlyFans session expiry and reply activation are irrelevant to stored text.
No credential fallback, session renewal, provider reads or message sending.
"""
import asyncio
import json
import os
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import AccountAiConfig, GrokCall
from db.onlyfans_connection_models import OfProviderConnection
from native_pilot.budget import DailyBudget
from native_pilot.scope import AccountScope
from onlyfans_runtime.connections import _open
from onlyfans_runtime.store import Refused
from .evidence import PROMPT, FAN_FIELDS, CREATOR_FIELDS


async def extract(store, tenant, account, binding, fan, sources, *, creator=False):
    if os.environ.get('ONLYFANS_PROCESS_ROLE') != 'worker':
        raise Refused('profile-worker-role-required')
    from llm_models import MODELS
    from llm_providers import PROVIDERS
    from model_selection import explicit_model
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account, lock=False)
        row = s.get(OfProviderConnection, account)
        cfg = s.get(AccountAiConfig, account)
        if runtime.binding_id != binding or row is None or row.binding_id != binding:
            raise Refused('profile-connection-scope')
        try:
            credential = _open(row, tenant).get('model')
        except Refused:
            raise Refused('model-credential-unavailable') from None
        connection_revision, sealed_snapshot = row.revision, row.sealed_config
        if type(credential) is not dict or not credential.get('apiKey'):
            raise Refused('model-credential-missing')
        # Keys are scoped to the explicitly saved model, never another provider.
        try:
            selected = explicit_model(cfg, 'ai_chatter', MODELS)
        except ValueError:
            raise Refused('profile-model-config-missing') from None
        if credential.get('modelId') != selected:
            raise Refused('model-credential-mismatch')
        spec = MODELS[selected]
        provider = PROVIDERS[spec.provider]
        endpoint = provider.base_url.rstrip('/') + '/chat/completions'
        api_key = credential['apiKey']
    import httpx
    url = httpx.URL(endpoint)
    if url.scheme != 'https' or url.username or url.password or url.query or url.fragment:
        raise Refused('profile-model-route-invalid')
    fields = CREATOR_FIELDS if creator else FAN_FIELDS
    prompt = PROMPT.replace('SUBJECT', 'the creator account' if creator else 'the fan').replace(
        'FIELD_LIST', 'Permitted fields: ' + ', '.join(sorted(fields)))
    messages = [{'role': 'system', 'content': prompt},
                {'role': 'user', 'content': json.dumps(list(sources.values()), ensure_ascii=False)}]
    body = {'model': spec.wire_model(), 'messages': messages, 'stream': False,
            'max_tokens': 2400, 'temperature': 0,
            'response_format': {'type': 'json_object'}, **spec.reasoning_keys()}
    raw = json.dumps(body, ensure_ascii=False).encode()
    if len(raw) > 80000:
        raise Refused('profile-prompt-too-large')
    budget = DailyBudget(AccountScope.parse(account), selected, endpoint=endpoint)
    # The creator call is audited against a real source fan, not a fabricated id.
    audit_fan = fan or next(iter(sources.values()))['fan']
    reservation = await budget.reserve(fan_id=audit_fan, request_bytes=len(raw), max_output_tokens=2400)
    try:
        with Session(store.engine) as s, s.begin():
            runtime = store._runtime(s, tenant, account, lock=False)
            if runtime.binding_id != binding:
                raise Refused('profile-binding-changed')
            current = s.get(OfProviderConnection, account)
            cfg = s.get(AccountAiConfig, account)
            if (current is None or current.revision != connection_revision
                    or current.sealed_config != sealed_snapshot
                    or explicit_model(cfg, 'ai_chatter', MODELS) != selected):
                raise Refused('profile-credential-changed')
            call = s.get(GrokCall, reservation.call_id)
            call.purpose = 'gen_info'
            call.prompt_json = raw.decode()
        async with asyncio.timeout(45):
            async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
                async with client.stream('POST', endpoint, content=raw, headers={
                        'Authorization': 'Bearer ' + api_key, 'Content-Type': 'application/json',
                        'Accept-Encoding': 'identity'}) as response:
                    if response.status_code != 200:
                        raise Refused('profile-model-request-failed')
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 131072:
                            raise Refused('profile-model-response-too-large')
                        chunks.append(chunk)
        result = json.loads(b''.join(chunks))
        usage = result.get('usage', {})
        settled = await budget.settle(reservation, cost_usd=None,
            tokens_in=usage.get('prompt_tokens'), tokens_out=usage.get('completion_tokens'))
        if settled is None:
            raise Refused('profile-model-usage-unavailable')
        choice = result['choices'][0]
        if choice.get('finish_reason') != 'stop' or choice['message'].get('tool_calls'):
            raise Refused('profile-model-response-incomplete')
        content = choice['message']['content']
        with Session(store.engine) as s, s.begin():
            call = s.get(GrokCall, reservation.call_id)
            call.response_json = content
        return json.loads(content), reservation.call_id
    except BaseException:
        await budget.hold(reservation)
        raise

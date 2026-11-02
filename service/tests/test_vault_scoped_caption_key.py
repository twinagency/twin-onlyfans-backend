"""Credential selection only. No provider calls, real keys, or customer sends."""
import asyncio
from types import SimpleNamespace

import pytest
from native_pilot.tools.describe_imported_vault import scoped_caption_key
from onlyfans_runtime.store import Refused

ACCOUNT = 'of-312988353'
MODEL = {'modelId': 'openrouter/deepseek-v4.1-flash', 'apiKey': 'synthetic-test-key'}


def test_unselected_accounts_keep_existing_credential_path(monkeypatch):
    monkeypatch.delenv('ONLYFANS_VAULT_DESCRIPTION_SCOPED_KEYS', raising=False)
    assert scoped_caption_key(ACCOUNT, None) is None
    monkeypatch.setenv('ONLYFANS_VAULT_DESCRIPTION_SCOPED_KEYS', 'of-63665785')
    assert scoped_caption_key(ACCOUNT, None) is None


def test_selected_account_uses_only_its_verified_key(monkeypatch):
    monkeypatch.setenv('ONLYFANS_VAULT_DESCRIPTION_SCOPED_KEYS', ACCOUNT)
    resolver = scoped_caption_key(ACCOUNT, dict(MODEL))
    assert asyncio.run(resolver(ACCOUNT, SimpleNamespace(name='openrouter'), 'describe_media')) == MODEL['apiKey']


@pytest.mark.parametrize('account,provider,purpose', [
    ('of-63665785', 'openrouter', 'describe_media'),
    (ACCOUNT, 'deepinfra', 'describe_media'),
    (ACCOUNT, 'openrouter', 'ai_chatter'),
    (ACCOUNT, 'openrouter', ''),
])
def test_cross_account_provider_or_purpose_never_gets_key(monkeypatch, account, provider, purpose):
    monkeypatch.setenv('ONLYFANS_VAULT_DESCRIPTION_SCOPED_KEYS', ACCOUNT)
    resolver = scoped_caption_key(ACCOUNT, dict(MODEL))
    with pytest.raises(Refused, match='scope unavailable'):
        asyncio.run(resolver(account, SimpleNamespace(name=provider), purpose))


@pytest.mark.parametrize('model', [None, {}, {**MODEL, 'apiKey': ''},
    {**MODEL, 'apiKey': 'bad\nkey'}, {**MODEL, 'modelId': 'qwen3-vl-30b'},
    {**MODEL, 'modelId': 'unregistered'}, {**MODEL, 'extra': 'unsupported'},
])
def test_selected_account_missing_or_incompatible_key_fails_closed(monkeypatch, model):
    monkeypatch.setenv('ONLYFANS_VAULT_DESCRIPTION_SCOPED_KEYS', ACCOUNT)
    with pytest.raises(Refused, match='credential unavailable'):
        scoped_caption_key(ACCOUNT, model)


def test_invalid_account_selection_fails_closed(monkeypatch):
    monkeypatch.setenv('ONLYFANS_VAULT_DESCRIPTION_SCOPED_KEYS', ACCOUNT + ',not-an-account')
    with pytest.raises(Refused, match='account configuration'):
        scoped_caption_key(ACCOUNT, dict(MODEL))


def test_primary_caption_route_uses_the_account_credential_provider():
    from llm_client import _resolve
    from native_pilot.tools.caption_vault import DEFAULT_MODEL
    assert _resolve(DEFAULT_MODEL)[1].name == 'openrouter'

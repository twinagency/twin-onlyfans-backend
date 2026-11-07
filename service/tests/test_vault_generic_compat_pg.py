"""Exercise the retained generic vault variant with fixtures and no network."""
import importlib.util
import json
import sys
from pathlib import Path

from test_onlyfans_foundation_pg import database  # noqa: F401
from test_vault_description_coverage_pg import rig, ACCOUNT, MID, IMG  # noqa: F401
from moxy_replacement import media_descriptions as md


def compatibility_api():
    candidate = Path(__file__).resolve().parents[2] / 'compat/candidate/generic/vault_ai_api.py'
    sys.path.insert(0, str(candidate.parent))
    if 'vault_category_evidence' not in sys.modules:
        category_spec = importlib.util.spec_from_file_location('vault_category_evidence', candidate.parent / 'vault_category_evidence.py')
        category = importlib.util.module_from_spec(category_spec)
        sys.modules['vault_category_evidence'] = category
        category_spec.loader.exec_module(category)
    spec = importlib.util.spec_from_file_location('offline_generic_vault_ai_api', candidate)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def test_generic_refresh_retains_stale_human_binding_and_discards_old_flags(rig):
    api = compatibility_api()
    rig.update(description='Prior human text', describe_status='described',
               content_hash='v2', content_hash_kind='fixture',
               ai_fields_json=json.dumps({'_content_stamp': 'fixture:v1', '_flags_v': 1,
                                          'underwear_visible': True, '_auto_folders_v2': True}),
               operator_overrides_json=json.dumps({'description': 'Prior human text'}),
               locked_fields_json='["description"]')
    assert md.item_state(rig.row()) == 'pending'
    result = rig.arun(api._describe_one(ACCOUNT, MID))
    assert result['ok'] is True
    row = rig.row()
    fields = json.loads(row.ai_fields_json)
    assert fields['_content_stamp'] == 'fixture:v2'
    assert '_flags_v' not in fields and 'underwear_visible' not in fields
    assert fields['_auto_folders_v2'] is True
    assert md.item_state(row) == 'pending' and md.effective_description(row) == 'Prior human text'
    assert json.loads(row.operator_overrides_json)['_description_bindings_v1']['description']['content_stamp'] == 'fixture:v1'


def test_generic_refresh_refuses_content_changed_during_collection(rig, monkeypatch):
    api = compatibility_api()
    rig.update(content_hash='v1', content_hash_kind='fixture', description='Previous')
    async def collect(account, item, raw, want):
        rig.update(content_hash='v2')
        return rig.vf.Collected([IMG], 'ok', source='photo')
    monkeypatch.setattr(rig.vf, 'collect', collect)
    result = rig.arun(api._describe_one(ACCOUNT, MID))
    assert result['status'] == 'stale_content'
    assert rig.row().description == 'Previous'


def test_generic_refresh_honours_human_lock_added_during_collection(rig, monkeypatch):
    api = compatibility_api()
    rig.update(content_hash='v1', content_hash_kind='fixture', description='Previous')
    async def collect(account, item, raw, want):
        rig.update(description='Concurrent human edit',
                   operator_overrides_json=json.dumps({'description': 'Concurrent human edit'}),
                   locked_fields_json='["description"]')
        return rig.vf.Collected([IMG], 'ok', source='photo')
    monkeypatch.setattr(rig.vf, 'collect', collect)
    result = rig.arun(api._describe_one(ACCOUNT, MID))
    assert result['ok'] is True
    row = rig.row()
    assert row.description == md.effective_description(row) == 'Concurrent human edit'

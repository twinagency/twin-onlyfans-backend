"""Regression of the generic image's actual imported vault module."""
import json
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_vault_description_coverage_pg import rig, ACCOUNT, MID, IMG  # noqa: F401
from moxy_replacement import media_descriptions as md
import vault_ai_api as api


def test_packaged_generic_retains_stale_human_binding(rig):
    rig.update(description='Prior human text', describe_status='described', content_hash='v2', content_hash_kind='fixture',
        ai_fields_json=json.dumps({'_content_stamp':'fixture:v1','_flags_v':1,'underwear_visible':True}),
        operator_overrides_json=json.dumps({'description':'Prior human text'}), locked_fields_json='["description"]')
    assert md.item_state(rig.row()) == 'pending'
    assert rig.arun(api._describe_one(ACCOUNT, MID))['ok'] is True
    row = rig.row()
    assert md.item_state(row) == 'pending' and md.effective_description(row) == 'Prior human text'
    assert json.loads(row.operator_overrides_json)['_description_bindings_v1']['description']['content_stamp'] == 'fixture:v1'
    assert '_flags_v' not in json.loads(row.ai_fields_json)


def test_packaged_generic_refuses_changed_content(rig, monkeypatch):
    rig.update(content_hash='v1', content_hash_kind='fixture', description='Previous')
    async def collect(account, item, raw, want):
        rig.update(content_hash='v2')
        return rig.vf.Collected([IMG], 'ok', source='photo')
    monkeypatch.setattr(rig.vf, 'collect', collect)
    assert rig.arun(api._describe_one(ACCOUNT, MID))['status'] == 'stale_content'
    assert rig.row().description == 'Previous'

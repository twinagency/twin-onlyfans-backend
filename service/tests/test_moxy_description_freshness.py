"""Versioned human descriptions and honest fingerprint scope, offline only."""
import asyncio
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from moxy_replacement import media_descriptions as md
from vault_ai_effective import project_operator_text, preserve_stale_operator_descriptions


def item(**kw):
    row = dict(account_id='fixture-account', media_id=7101, kind='photo',
               description='Human corrected description', video_description=None,
               describe_status='described', content_hash='v1', content_hash_kind='fixture',
               ai_fields_json=json.dumps({'_content_stamp': 'fixture:v1'}),
               operator_overrides_json=json.dumps({'description': 'Human corrected description'}),
               locked_fields_json='["description"]', describe_model=None, describe_call_id=None,
               frames_sampled=None, describe_generated_at=None)
    row.update(kw)
    return SimpleNamespace(**row)


def bind(row, *, stamp='fixture:v1', **changes):
    overrides = json.loads(row.operator_overrides_json)
    evidence = {'account_id': row.account_id, 'media_id': row.media_id, 'kind': row.kind,
                'content_stamp': stamp,
                'text_sha256': hashlib.sha256(overrides['description'].strip().encode()).hexdigest()}
    evidence.update(changes)
    overrides['_description_bindings_v1'] = {'description': evidence}
    row.operator_overrides_json = json.dumps(overrides)
    return row


def test_legacy_operator_lock_does_not_override_known_content_change():
    row = item(content_hash='v2')
    before = deepcopy(row.__dict__)
    assert md.item_state(row) == 'pending'
    assert md.effective_description(row) == 'Human corrected description'
    assert row.__dict__ == before


def test_current_operator_review_is_independent_of_older_model_stamp():
    row = bind(item(content_hash='v2'), stamp='fixture:v2')
    assert md.item_state(row) == 'described'


def test_fresh_model_analysis_does_not_reapprove_old_operator_text():
    row = bind(item(content_hash='v2', ai_fields_json=json.dumps({'_content_stamp': 'fixture:v2'})))
    assert md.item_state(row) == 'pending'


def test_model_refresh_preserves_known_stale_legacy_correction_evidence():
    row = item(content_hash='v2')
    row.operator_overrides_json = json.dumps(preserve_stale_operator_descriptions(row))
    row.ai_fields_json = json.dumps({'_content_stamp': 'fixture:v2'})
    assert md.item_state(row) == 'pending'
    assert md.effective_description(row) == 'Human corrected description'


def test_same_content_or_unstamped_legacy_refresh_does_not_invent_version_evidence():
    for row in (item(), item(content_hash=None, ai_fields_json=None)):
        before = json.loads(row.operator_overrides_json)
        assert preserve_stale_operator_descriptions(row) == before
        assert md._view(row.media_id, row)['evidence']['description_freshness']['status'] == 'unknown'


@pytest.mark.parametrize('changes', [
    {'account_id': 'other-account'}, {'media_id': 8001}, {'kind': 'video'},
    {'text_sha256': '0' * 64},
])
def test_copied_or_changed_operator_binding_is_not_current(changes):
    row = bind(item(), **changes)
    assert md.item_state(row) == 'pending'


def test_unstamped_legacy_description_stays_available_with_unknown_evidence():
    row = item(content_hash=None, ai_fields_json=None, operator_overrides_json=None,
               locked_fields_json=None)
    view = md._view(row.media_id, row)
    assert view['state'] == 'described'
    assert view['evidence']['description_freshness']['status'] == 'unknown'


def test_matching_thumbnail_is_not_full_asset_verification():
    row = item(content_hash_kind='thumb/md5+dhash8+ahash8',
               ai_fields_json=json.dumps({'_content_stamp': 'thumb/md5+dhash8+ahash8:v1'}),
               operator_overrides_json=None, locked_fields_json=None)
    evidence = md._view(row.media_id, row)['evidence']['description_freshness']
    assert evidence['status'] == 'matching_fingerprint'
    assert evidence['fingerprint_scope'] == 'thumbnail'
    assert evidence['full_asset_verified'] is False


def test_writer_freshness_is_opt_in_and_does_not_invalidate_existing_asset_hash():
    from moxy_replacement.configuration import digest
    from moxy_replacement.pricing import asset_evidence
    row = item(operator_overrides_json=None, locked_fields_json=None,
               ai_fields_json=json.dumps({'_content_stamp': 'fixture:v1',
                                          '_frames': 1, '_frame_source': 'photo'}))
    historical_basis = {'frames': 1, 'frame_source': 'photo'}
    assert md.limitations(row) == historical_basis
    expected = digest({'media_id': row.media_id, 'kind': row.kind,
        'content_hash': row.content_hash, 'content_hash_kind': row.content_hash_kind,
        'description': md.effective_description(row), 'basis': historical_basis,
        'operator_corrected': False})
    assert asset_evidence(row) == expected
    assert md.limitations(row, include_freshness=True)['description_freshness']['full_asset_verified'] is False


def test_fingerprint_kind_change_is_a_change_even_if_hash_string_matches():
    row = bind(item(content_hash_kind='other-kind'))
    assert md.item_state(row) == 'pending'


@pytest.mark.parametrize('human', [True, False])
def test_lost_current_fingerprint_does_not_erase_recorded_version_requirement(human):
    row = bind(item()) if human else item(operator_overrides_json=None, locked_fields_json=None)
    row.content_hash = None
    assert md.item_state(row) == 'pending'
    assert md._view(row.media_id, row)['evidence']['description_freshness']['status'] == 'unverifiable'


def test_unrelated_tag_edit_does_not_rebind_stale_description():
    row = bind(item(content_hash='v2'))
    overrides = json.loads(row.operator_overrides_json)
    before = deepcopy(overrides['_description_bindings_v1'])
    project_operator_text('tags', ['New tag'], overrides, set(), {}, item=row)
    assert overrides['_description_bindings_v1'] == before


def test_new_operator_edit_records_current_binding_and_preserves_other_overrides():
    row = item(content_hash='v2')
    overrides = {'unrelated': 'kept'}
    locked, columns = set(), {}
    project_operator_text('description', 'New reviewed text', overrides, locked, columns, item=row)
    row.operator_overrides_json = json.dumps(overrides)
    row.locked_fields_json = json.dumps(sorted(locked))
    row.description = columns['description']
    assert overrides['unrelated'] == 'kept'
    assert overrides['_description_bindings_v1']['description']['content_stamp'] == 'fixture:v2'
    assert md.item_state(row) == 'described'


def test_empty_operator_edit_removes_only_its_binding():
    row = bind(item())
    overrides = json.loads(row.operator_overrides_json)
    overrides['_description_bindings_v1']['video_description'] = {'other': 'preserved'}
    project_operator_text('description', None, overrides, set(), {}, item=row)
    assert overrides['description'] is None
    assert 'description' not in overrides['_description_bindings_v1']
    assert overrides['_description_bindings_v1']['video_description'] == {'other': 'preserved'}


def test_edit_without_fingerprint_is_explicitly_unverified():
    row = item(content_hash=None, ai_fields_json=None)
    overrides, locked, columns = {}, set(), {}
    project_operator_text('description', 'Reviewed without a hash', overrides, locked, columns, item=row)
    row.operator_overrides_json = json.dumps(overrides)
    row.locked_fields_json = json.dumps(sorted(locked))
    row.description = columns['description']
    freshness = md._view(row.media_id, row)['evidence']['description_freshness']
    assert freshness['status'] == 'unknown'
    assert freshness['source'] == 'operator'
    assert freshness['full_asset_verified'] is False


def test_stale_operator_description_requires_review_not_automatic_paid_analysis(monkeypatch):
    from moxy_replacement import routing
    row = bind(item(content_hash='v2'))
    view = md._view(row.media_id, row)
    async def descriptions(account, ids):
        return [view]
    calls = []
    async def describe(account, media_id):
        calls.append((account, media_id))
    monkeypatch.setattr(md, 'descriptions', descriptions)
    monkeypatch.setattr(routing, 'owner_state', lambda account: routing.SELECTED)
    got = asyncio.run(md.ensure_described(row.account_id, [row.media_id], describe=describe))
    assert got[0]['state'] == 'pending'
    assert calls == []

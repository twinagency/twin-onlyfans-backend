"""Cross-lineage vault row compatibility, as read/written by the CANDIDATE Moxy code.

Old lineage = 0d9aab0fc (running vault workers) + Amelia overlays. Old modules are loaded
ISOLATED from `git show` output / the Amelia overlay file under private module names, with
sys.modules['vault_ai_effective'] swapped only while they execute. Nothing is imported
into the candidate process under its real name. Offline; the last section needs
ONLYFANS_TEST_PG_URL (disposable lab PG only).

Some tests pin CURRENT behaviour and say so in the name (`documents_`).
"""
import ast
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import types
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from moxy_replacement import media_descriptions as md
import vault_ai_effective as new_eff
import vault_describe_state as vds
from onlyfans_runtime import vault_editing as new_edit

OLD_REPO = Path('/home/ronan/twin/twin-onlyfans-backend')
OLD_SHA = '0d9aab0fc'
AMELIA = Path('/home/ronan/twin/moxy-collect-2026-11-06/pkg/MOXY-PROD-MANIFEST-2026-11-06/amelia-extra-files')
HAVE_OLD = (OLD_REPO / '.git').exists() or (OLD_REPO / 'service').exists()


def old_source(path):
    try:
        return subprocess.run(['git', '-C', str(OLD_REPO), 'show', f'{OLD_SHA}:{path}'],
                              check=True, capture_output=True, text=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip('old lineage 0d9aab0fc not available')


def load_old(name, path, package=None, swap=None):
    """Exec old source as a private module. `swap` = {real_module_name: module} visible
    only during exec (restored after) so `from vault_ai_effective import ...` binds old code."""
    src = old_source(path)
    mod = types.ModuleType(name)
    mod.__file__ = path
    if package:
        mod.__package__ = package
    saved = {k: sys.modules.get(k) for k in (swap or {})}
    try:
        sys.modules.update(swap or {})
        exec(compile(src, f'<{OLD_SHA}:{path}>', 'exec'), mod.__dict__)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return mod


@pytest.fixture(scope='module')
def old_eff():
    return load_old('old0d9_vault_ai_effective', 'service/vault_ai_effective.py')


@pytest.fixture(scope='module')
def old_edit(old_eff):
    return load_old('old0d9_vault_editing', 'service/onlyfans_runtime/vault_editing.py',
                    package='onlyfans_runtime', swap={'vault_ai_effective': old_eff})


def row(**kw):
    d = dict(account_id='acc', media_id=11, kind='photo', description='A woman on a beach',
             video_description=None, describe_status='described', content_hash='h1',
             content_hash_kind='thumb/v1', ai_fields_json='{}', operator_overrides_json=None,
             locked_fields_json=None, describe_model='qwen', describe_call_id='c1',
             frames_sampled=1, describe_generated_at=None, tags='[]', removed_at=None)
    d.update(kw)
    return SimpleNamespace(**d)


# ---------------------------------------------------------------- 1. old-lineage description rows
def old_lineage_described_row():
    """What 0d9 _describe_one_locked leaves: status described, text, NO _content_stamp,
    NO _description_bindings_v1, plus the v3 folder markers it carries."""
    return row(ai_fields_json=json.dumps({
        'description': 'A woman on a beach', 'tags': ['beach'], '_auto_folders_v3': True,
        '_folder_evidence_v1': {'status': 'ok'}, 'category_evidence_v1': {'status': 'ai_verified'}}),
        content_hash='h1')


def test_old_described_row_is_usable_with_unknown_freshness():
    r = old_lineage_described_row()
    assert md.item_state(r) == 'described'
    assert md.effective_description(r) == 'A woman on a beach'
    fr = md.description_freshness(r)
    assert fr['status'] == 'unknown' and fr['source'] == 'model_or_legacy'
    assert fr['recorded_stamp'] is None and fr['full_asset_verified'] is False
    assert vds.content_changed(r) is False


def test_old_described_row_view_does_not_crash_and_reports_unknown():
    r = old_lineage_described_row()
    v = md._view(11, r)
    assert v['state'] == 'described' and v['description'] == 'A woman on a beach'
    assert v['evidence']['description_freshness']['status'] == 'unknown'
    assert v['limitations']['description_freshness']['status'] == 'unknown'
    # unknown keys written by the old lineage are ignored, not choked on
    assert 'sampling_note' not in v['limitations']


def test_old_row_without_content_hash_and_odd_ai_fields_do_not_crash():
    for fields in (None, '', 'not json', '[]', '{"_content_stamp": 5}', '{"_content_stamp": ""}'):
        r = row(ai_fields_json=fields, content_hash=None, content_hash_kind=None)
        md.item_state(r); md.description_freshness(r); md._view(1, r)


def test_old_row_stamp_absent_but_content_hash_present_never_pending():
    # Candidate must not re-pay for every pre-stamp (old-lineage) asset.
    assert md.item_state(old_lineage_described_row()) == 'described'


def test_old_lineage_stripped_stamp_hides_real_content_change_documents_R3():
    """0d9 re-describe drops _content_stamp (whole-replaces ai_fields_json). After that a
    real content change is invisible to the candidate: still 'described' (safe, but stale)."""
    stamped = row(ai_fields_json=json.dumps({'_content_stamp': 'thumb/v1:h1'}), content_hash='h2')
    assert md.item_state(stamped) == 'pending'                     # candidate-lineage: detected
    stripped = row(ai_fields_json='{}', content_hash='h2')         # after old-lineage re-describe
    assert md.item_state(stripped) == 'described'                  # detection lost
    assert md.description_freshness(stripped)['status'] == 'unknown'


def test_old_lineage_operator_lock_without_binding_is_unknown_and_described():
    r = row(description='Human text', locked_fields_json='["description"]',
            operator_overrides_json='{"description": "Human text"}')
    assert md.operator_corrected(r) is True
    assert md.description_freshness(r)['status'] == 'unknown'
    assert md.item_state(r) == 'described'


def test_old_lineage_operator_lock_with_known_mismatch_goes_pending():
    r = row(description='Human text', locked_fields_json='["description"]',
            operator_overrides_json='{"description": "Human text"}',
            ai_fields_json='{"_content_stamp": "thumb/v1:h1"}', content_hash='h2')
    assert md.item_state(r) == 'pending'


# ---------------------------------------------------------------- 2. old edit leaves stale binding
def bound_row(text='Human text', stamp='thumb/v1:h1', **kw):
    o = {'description': text, '_description_bindings_v1': {'description': {
        'account_id': 'acc', 'media_id': 11, 'kind': 'photo', 'content_stamp': stamp,
        'text_sha256': hashlib.sha256(text.encode()).hexdigest()}}}
    return row(description=text, locked_fields_json='["description"]',
               operator_overrides_json=json.dumps(o),
               ai_fields_json=json.dumps({'_content_stamp': stamp}), **kw)


def old_edit_text(old_eff, r, new_text):
    """Apply the OLD project_operator_text exactly like old vault_editing.edit_asset (l.131)."""
    overrides = json.loads(r.operator_overrides_json)
    locked = set(json.loads(r.locked_fields_json))
    columns = {}
    old_eff.project_operator_text('description', new_text, overrides, locked, columns)
    for k, v in columns.items():
        setattr(r, k, v)
    r.operator_overrides_json = json.dumps(overrides)
    r.locked_fields_json = json.dumps(sorted(locked))
    return r


def test_old_project_operator_text_has_no_item_kwarg_and_no_binding_logic(old_eff):
    import inspect
    assert 'item' not in inspect.signature(old_eff.project_operator_text).parameters
    assert 'bindings' not in old_source('service/vault_ai_effective.py')


def test_old_edit_leaves_stale_binding_and_candidate_marks_invalid_R1(old_eff):
    r = bound_row()
    assert md.item_state(r) == 'described'
    old_edit_text(old_eff, r, 'Rewritten by old lineage')
    saved = json.loads(r.operator_overrides_json)
    assert '_description_bindings_v1' in saved                      # old code did not drop it
    assert saved['_description_bindings_v1']['description']['text_sha256'] != \
        hashlib.sha256(b'Rewritten by old lineage').hexdigest()   # ... and did not refresh it
    assert md.description_freshness(r)['status'] == 'invalid'
    # REAL INCOMPATIBILITY: a valid human correction vanishes from Moxy until re-saved
    assert md.item_state(r) == 'pending'
    assert md.effective_description(r) == 'Rewritten by old lineage'   # text itself intact


def test_stale_invalid_operator_row_is_never_redescribed_by_moxy(old_eff):
    r = old_edit_text(old_eff, bound_row(), 'Rewritten by old lineage')
    calls = []

    async def fake(account, media):
        calls.append(media)
    import asyncio
    view = asyncio.run(_ensure_with_rows([r], fake))
    assert view[0]['state'] == 'pending' and calls == []           # stuck pending, no model overwrite


async def _ensure_with_rows(rows, describe):
    by = {x.media_id: x for x in rows}

    async def fake_descriptions(account_id, media_ids):
        return [md._view(m, by.get(m)) for m in media_ids]
    orig = md.descriptions
    md.descriptions = fake_descriptions
    try:
        from moxy_replacement import routing
        orig_owner = routing.owner_state
        routing.owner_state = lambda a: routing.SELECTED
        try:
            return await md.ensure_described('acc', [r.media_id for r in rows], describe=describe)
        finally:
            routing.owner_state = orig_owner
    finally:
        md.descriptions = orig


def test_old_edit_to_same_text_keeps_binding_valid(old_eff):
    r = old_edit_text(old_eff, bound_row(), 'Human text')
    assert md.item_state(r) == 'described'


def test_old_edit_clearing_text_candidate_pending_not_crash(old_eff):
    r = old_edit_text(old_eff, bound_row(), None)
    r.description = None
    assert md.item_state(r) in {'pending', 'failed'}               # no crash, no description offered
    assert md.effective_description(r) == ''


def test_old_edit_on_row_with_no_binding_stays_unknown_and_usable(old_eff):
    r = row(description='Human text', locked_fields_json='["description"]',
            operator_overrides_json='{"description": "Human text"}')
    old_edit_text(old_eff, r, 'Edited by old')
    assert md.item_state(r) == 'described'
    assert md.description_freshness(r)['status'] == 'unknown'


def test_candidate_rebinding_fixes_a_stale_binding_after_old_edit(old_eff):
    r = old_edit_text(old_eff, bound_row(), 'Rewritten by old lineage')
    overrides = json.loads(r.operator_overrides_json); locked = set(json.loads(r.locked_fields_json))
    new_eff.project_operator_text('description', 'Rewritten by old lineage', overrides, locked, {}, item=r)
    r.operator_overrides_json = json.dumps(overrides)
    assert md.item_state(r) == 'described'                         # recovery path = save from candidate API


def test_old_edit_does_not_corrupt_binding_json(old_eff):
    r = old_edit_text(old_eff, bound_row(), 'x')
    saved = json.loads(r.operator_overrides_json)
    assert saved['_description_bindings_v1']['description']['account_id'] == 'acc'


# ---------------------------------------------------------------- 3. Amelia needs_review
def amelia_needs_review_row(**kw):
    """Amelia _describe_one_locked line ~2842: unverified description -> blanked, status
    needs_review, rejected_analysis kept in ai_fields_json."""
    d = dict(description='', video_description=None, describe_status='needs_review',
             ai_fields_json=json.dumps({'category_evidence_v1': {'status': 'needs_review',
                 'categories': [], 'rejected_analysis': {'description': 'Unverified claim'}}}))
    d.update(kw)
    return row(**d)


def test_amelia_overlay_really_writes_needs_review_for_blank_description():
    src = (AMELIA / 'amelia-vault_ai_api.py').read_text()
    assert 'description_status = "described" if desc else "needs_review"' in src


def test_needs_review_blank_row_is_failed_not_described_not_crash():
    r = amelia_needs_review_row()
    assert md.item_state(r) == 'failed'
    v = md._view(11, r)
    assert v['state'] == 'failed' and 'description' not in v
    assert v['evidence']['describe_status'] == 'needs_review'


def test_needs_review_rejected_analysis_never_leaks_to_moxy():
    r = amelia_needs_review_row()
    assert md.effective_description(r) == ''
    assert 'Unverified claim' not in json.dumps(md._view(11, r))


def test_needs_review_row_with_leftover_text_is_failed_not_trusted():
    r = amelia_needs_review_row(description='stale text')
    assert md.item_state(r) == 'failed'                            # status gate, text not offered


def test_needs_review_row_retried_only_when_retry_failed():
    import asyncio
    r = amelia_needs_review_row()
    calls = []

    async def fake(account, media):
        calls.append(media)
    asyncio.run(_ensure_with_rows([r], fake))
    assert calls == []                                             # default: not retried
    asyncio.run(_ensure_with_rows([r], fake))  # still nothing
    async def go():
        by = {r.media_id: r}
        orig = md.descriptions
        async def d(a, m): return [md._view(x, by.get(x)) for x in m]
        md.descriptions = d
        from moxy_replacement import routing
        o = routing.owner_state; routing.owner_state = lambda a: routing.SELECTED
        try:
            return await md.ensure_described('acc', [11], describe=fake, retry_failed=True)
        finally:
            md.descriptions = orig; routing.owner_state = o
    asyncio.run(go())
    assert calls == [11]   # Moxy WOULD pay to re-describe an Amelia needs_review row if retry_failed=True


def test_needs_review_with_operator_text_is_usable():
    r = amelia_needs_review_row(description='Human fix', locked_fields_json='["description"]',
                                operator_overrides_json='{"description": "Human fix"}')
    assert md.item_state(r) == 'described'


# ---------------------------------------------------------------- 4. asset_fingerprint mismatch
def test_old_and_new_fingerprint_field_sets_differ(old_edit):
    old_src = old_source('service/onlyfans_runtime/vault_editing.py')
    assert '"content_hash"' not in old_src.split('def asset_view')[0]
    r = row(operator_overrides_json='{}', locked_fields_json='[]')
    assert old_edit.asset_fingerprint(r) != new_edit.asset_fingerprint(r)


def test_fingerprints_differ_for_every_realistic_row_shape(old_edit):
    shapes = [row(), row(content_hash=None, content_hash_kind=None), old_lineage_described_row(),
              bound_row(), amelia_needs_review_row(), row(kind='video')]
    for r in shapes:
        assert old_edit.asset_fingerprint(r) != new_edit.asset_fingerprint(r)


def test_new_fingerprint_adds_thumbnail_hash_sensitivity_old_ignores_it(old_edit):
    a, b = row(content_hash='h1'), row(content_hash='h2')
    assert old_edit.asset_fingerprint(a) == old_edit.asset_fingerprint(b)
    assert new_edit.asset_fingerprint(a) != new_edit.asset_fingerprint(b)


def test_each_side_accepts_only_its_own_fingerprint(old_edit):
    r = row()
    old_fp, new_fp = old_edit.asset_view(r)['fingerprint'], new_edit.asset_view(r)['fingerprint']
    # edit_asset compares asset_view(row)['fingerprint'] to expected_fingerprint:
    assert new_edit.asset_view(r)['fingerprint'] != old_fp   # candidate edit would raise Refused
    assert old_edit.asset_view(r)['fingerprint'] != new_fp   # old edit would raise Refused
    assert len(old_fp) == len(new_fp) == 64                  # both pass _fingerprint() shape check


def test_asset_view_payload_other_than_fingerprint_is_identical(old_edit):
    r = row(operator_overrides_json='{}', locked_fields_json='[]')
    o, n = dict(old_edit.asset_view(r)), dict(new_edit.asset_view(r))
    o.pop('fingerprint'); n.pop('fingerprint')
    assert o == n


def test_vault_editing_is_the_only_cross_lineage_fingerprint_producer():
    """Ship rule: any module that mints/compares asset fingerprints is in vault_editing."""
    root = Path(__file__).resolve().parent.parent
    hits = [p.name for p in root.rglob('*.py') if 'tests' not in p.parts and
            'asset_fingerprint' in p.read_text(errors='ignore')]
    assert sorted(hits) == ['vault_editing.py']


def test_vault_editing_hash_is_what_must_match_between_api_and_worker():
    root = Path(__file__).resolve().parent.parent
    cand = hashlib.sha256((root / 'onlyfans_runtime/vault_editing.py').read_bytes()).hexdigest()
    old = hashlib.sha256(old_source('service/onlyfans_runtime/vault_editing.py').encode()).hexdigest()
    assert cand != old


# ---------------------------------------------------------------- 5. misc contract pins
def test_candidate_reader_columns_are_all_plain_attributes_old_rows_have():
    """Every attribute media_descriptions reads exists on the VaultItem model (no new column)."""
    from db.models import VaultItem
    for col in ('description', 'video_description', 'operator_overrides_json', 'locked_fields_json',
                'ai_fields_json', 'describe_status', 'describe_model', 'describe_call_id',
                'frames_sampled', 'describe_generated_at', 'content_hash', 'content_hash_kind'):
        assert hasattr(VaultItem, col)


def test_candidate_vault_ai_api_drops_v3_marker_documents_R3():
    src = Path(new_eff.__file__).parent.joinpath('vault_ai_api.py').read_text()
    assert '_auto_folders_v3' not in src
    assert '_auto_folders_v3' in old_source('service/vault_ai_api.py')


# ---------------------------------------------------------------- 6. DB-backed (disposable lab PG)
pg = pytest.mark.skipif(not os.environ.get('ONLYFANS_TEST_PG_URL'), reason='needs ONLYFANS_TEST_PG_URL')
if os.environ.get('ONLYFANS_TEST_PG_URL'):
    assert 'partner-sql-check' not in os.environ['ONLYFANS_TEST_PG_URL']
    from test_onlyfans_foundation_pg import database, seed  # noqa: F401


@pg
def test_pg_old_minted_fingerprint_is_refused_by_candidate_edit_asset(database, old_edit):
    from sqlalchemy.orm import Session
    from db.models import VaultItem
    from onlyfans_runtime.store import Refused, utcnow
    store = seed(*database)
    actor = 'op-session:' + 'a' * 64
    with Session(database[1]) as s, s.begin():
        s.add(VaultItem(account_id='acc-a', media_id=11, kind='photo', created_at=utcnow(),
                        description='Original', content_hash='h1', content_hash_kind='thumb/v1',
                        ai_fields_json='{}', operator_overrides_json='{}', locked_fields_json='[]'))
    with Session(database[1]) as s:
        r = s.get(VaultItem, ('acc-a', 11))
        old_fp = old_edit.asset_view(r)['fingerprint']
    good = new_edit.get_asset(store, 'tenant-a', 'acc-a', '11')['fingerprint']
    assert good != old_fp
    with pytest.raises(Refused, match='vault metadata changed'):
        new_edit.edit_asset(store, 'tenant-a', 'acc-a', '11', patch={'description': 'X'},
                            expected_fingerprint=old_fp, command_key='b' * 64, actor=actor)
    new_edit.edit_asset(store, 'tenant-a', 'acc-a', '11', patch={'description': 'X'},
                        expected_fingerprint=good, command_key='d' * 64, actor=actor)
    store.close()


@pg
def test_pg_needs_review_never_offered_by_candidate_worker_pending(database):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from db.models import VaultItem
    from onlyfans_runtime.store import utcnow
    from onlyfans_runtime import vault_description_worker as w
    store = seed(*database)
    now = utcnow()
    long_ago = now - timedelta(days=30)
    with Session(database[1]) as s, s.begin():
        for mid, status, desc in ((1, 'needs_review', ''), (2, 'described', ''), (3, None, ''),
                                  (4, 'failed', '')):
            s.add(VaultItem(account_id='acc-a', media_id=mid, kind='photo', created_at=now,
                            description=desc, describe_status=status, describe_generated_at=long_ago,
                            ai_fields_json='{}', locked_fields_json='[]'))
    with Session(database[1]) as s:
        got = {r.media_id for r in s.execute(select(VaultItem).where(w.pending(now))).scalars()}
    assert got == {2, 3, 4}          # needs_review (1) is parked: neither retried nor surfaced
    store.close()

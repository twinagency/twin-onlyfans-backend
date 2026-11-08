"""Versioned pilot coverage preserves the separate general catalogue contract."""
import copy
import json
import uuid
from dataclasses import asdict
from datetime import timedelta
import pytest
from sqlalchemy.orm import Session
from conversation_engine import pilot_scope
from conversation_engine.catalogue import vault_coverage, aliases_for, content_hash
from onlyfans_runtime.media_delivery import catalog_terms
from db.models import CatalogItem, VaultItem
from db.onlyfans_engine_models import OfEngineCatalogueAlias
from onlyfans_runtime.store import Refused, utcnow
from test_onlyfans_foundation_pg import database
from test_engine_real_worker_pg import world
from test_plan_c_draft_app_pg import configured_draft
from test_plan_c_supervised_pg import supervised
from test_plan_c_sales_journey_pg import install_coverage
from test_plan_c_scope_pg import ACTOR
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT


def test_pilot_product_snapshot_does_not_reinterpret_general_coverage(supervised):
    world, *_ = supervised
    install_coverage(world)
    cfg = pilot_scope.describe(world.store, TENANT, ACCOUNT)['config']
    assert cfg['offer_coverage']['snapshot_kind'] == 'pilot-product.v1'
    legacy = asdict(vault_coverage(world.store, TENANT, ACCOUNT, complete=True,
        evidence_ref='vault:legacy-snapshot', valid_until=utcnow() + timedelta(minutes=30)))
    legacy['valid_until'] = legacy['valid_until'].isoformat()
    world.second_item()
    with Session(world.database[1]) as s:
        runtime = world.store._runtime(s, TENANT, ACCOUNT, lock=False)
        assert pilot_scope.current_offer_coverage(s, runtime, cfg)
        with pytest.raises(Refused, match='offer_coverage_stale'):
            pilot_scope.current_offer_coverage(s, runtime, {**cfg, 'offer_coverage': legacy})
    for kind in ['pilot-product.v2', 'catalogue.v1', None]:
        bad = copy.deepcopy(cfg);bad['offer_coverage']['snapshot_kind'] = kind
        with pytest.raises(Refused):pilot_scope.validate(bad)


def test_every_relevant_product_dependency_invalidates_pilot_permission(supervised):
    world, *_ = supervised
    install_coverage(world)
    cfg = pilot_scope.describe(world.store, TENANT, ACCOUNT)['config']
    with Session(world.database[1]) as s, s.begin():
        runtime = world.store._runtime(s, TENANT, ACCOUNT, lock=False)
        item = s.get(CatalogItem, world.item)
        media = s.get(VaultItem, (ACCOUNT, 11))
        changes = [(item, 'label', 'Different approved label'),
            (item, 'description_for_ai', 'A different picture'), (item, 'price_cents', 400),
            (item, 'enabled', False), (item, 'kind', 'video'), (item, 'media_ids', '[12]'),
            (item, 'preview_media_ids', '[11]'), (item, 'tags', '["changed dependency"]'),
            (item, 'pricing_mode', 'fixed' if item.pricing_mode != 'fixed' else 'dynamic'),
            (item, 'updated_at', item.updated_at + timedelta(seconds=1)),
            (media, 'kind', 'video'), (media, 'content_hash', 'c' * 64),
            (media, 'content_hash_kind', 'synthetic-file-v1'), (media, 'removed_at', utcnow()),
            (media, 'updated_at', (media.updated_at or utcnow()) + timedelta(seconds=1))]
        for row, key, value in changes:
            savepoint = s.begin_nested()
            setattr(row, key, value);s.flush()
            with pytest.raises(Refused):pilot_scope.current_offer_coverage(s, runtime, cfg)
            savepoint.rollback()
            assert pilot_scope.current_offer_coverage(s, runtime, cfg), key
        savepoint = s.begin_nested()
        s.add(OfEngineCatalogueAlias(account_id=ACCOUNT, alias_id=str(uuid.uuid4()), binding_id=runtime.binding_id,
            item_id=world.item, alias='new meaningful alias', normalized='new meaningful alias',
            content_hash=content_hash(s,runtime,catalog_terms(s,ACCOUNT,world.item,lock=False)), actor=ACTOR, created_at=utcnow()))
        s.flush()
        with pytest.raises(Refused, match='offer_coverage_stale'):pilot_scope.current_offer_coverage(s, runtime, cfg)
        savepoint.rollback()
        for field in ['account_id', 'binding_id']:
            bad = copy.deepcopy(cfg);bad['offer_coverage'][field] = 'another-scope'
            with pytest.raises(Refused, match='offer_coverage_scope_changed'):pilot_scope.current_offer_coverage(s, runtime, bad)


def test_alias_that_existing_retrieval_excludes_does_not_change_pilot_permission(supervised):
    world,*_=supervised
    install_coverage(world)
    cfg=pilot_scope.describe(world.store,TENANT,ACCOUNT)['config']
    with Session(world.database[1]) as s,s.begin():
        runtime=world.store._runtime(s,TENANT,ACCOUNT,lock=False)
        terms=catalog_terms(s,ACCOUNT,world.item,lock=False)
        stale='e'*64
        assert stale!=content_hash(s,runtime,terms)
        s.add(OfEngineCatalogueAlias(account_id=ACCOUNT,alias_id=str(uuid.uuid4()),binding_id=runtime.binding_id,
            item_id=world.item,alias='obsolete description',normalized='obsolete description',
            content_hash=stale,actor=ACTOR,created_at=utcnow()))
        s.flush()
        assert aliases_for(s,runtime,world.item,terms)==[], 'the existing owner already excludes this stale alias'
        assert pilot_scope.current_offer_coverage(s,runtime,cfg)

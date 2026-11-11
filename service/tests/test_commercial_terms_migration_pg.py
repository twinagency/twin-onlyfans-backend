"""Step 3 schema: 0135_commercial_catalogue_terms (additive, evidence-preserving, guarded)."""
import uuid
from datetime import timedelta

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from commercial_terms_fixture import ACTOR, add_item, add_vault, runtime_of
from db.schema import HEAD, SchemaMismatch, _config, check_schema, upgrade
from onlyfans_runtime.store import utcnow
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, configured

NEW = '0135_commercial_catalogue_terms'


def terms_row(binding, item, **over):
    row = dict(account_id=ACCOUNT, item_id=item, revision=1, status='approved', product_kind='single_photo',
               standard_price_cents=1200, discount_price_cents=None, price_source='agency_category',
               category_folder_id=7, category_price_revision=1, media_count=1, paid_photo_count=1, paid_video_count=0,
               preview_photo_count=0, preview_video_count=0, approved_by=ACTOR, approved_at=utcnow(), binding_id=binding,
               command_key='c' * 64, request_hash='d' * 64, snapshot_json='{}', snapshot_sha256='e' * 64)
    row.update(over)
    return row


def insert(c, row):
    keys = list(row)
    c.execute(text('INSERT INTO of_catalogue_terms (' + ','.join(keys) + ') VALUES (' + ','.join(':' + k for k in keys) + ')'), row)


def member(c, item, revision=1, media=11, role='paid', kind='photo'):
    c.execute(text('INSERT INTO of_catalogue_terms_member(account_id,item_id,revision,media_id,role,kind) VALUES (:a,:i,:r,:m,:ro,:k)'),
              {'a': ACCOUNT, 'i': item, 'r': revision, 'm': media, 'ro': role, 'k': kind})


@pytest.fixture
def world(database):
    store, authority = configured(database, mode='automated', limit=20)
    add_vault(database[1], {11: 'image', 12: 'image', 13: 'video'})
    item = add_item(database[1], label='One', media=[11])
    yield store, database[1], item, runtime_of(store)
    store.close()


def test_head_is_the_new_revision_and_the_schema_check_pins_its_guards(world):
    store, engine, item, binding = world
    assert HEAD == NEW
    with engine.begin() as c:
        check_schema(c)
        assert c.scalar(text('SELECT version_num FROM alembic_version')) == NEW
        for table in ('of_catalogue_terms', 'of_catalogue_terms_member'):
            assert c.scalar(text('SELECT to_regclass(:t) IS NOT NULL'), {'t': table})
        with c.begin_nested():
            c.execute(text('DROP TRIGGER of_catalogue_terms_guard ON of_catalogue_terms'))
            with pytest.raises(SchemaMismatch, match='catalogue terms'):
                check_schema(c)


def test_constraints_reject_unapproved_shapes_and_two_live_revisions(world):
    store, engine, item, binding = world
    with engine.begin() as c:
        insert(c, terms_row(binding, item))
        member(c, item)
        bad = [dict(standard_price_cents=299), dict(standard_price_cents=20001),
               dict(revision=2, discount_price_cents=1200), dict(revision=2, discount_price_cents=1300),
               dict(revision=2, discount_price_cents=299), dict(revision=2, status='bogus'),
               dict(revision=2, product_kind='bundle_photo', price_source='agency_category'),
               dict(revision=2, product_kind='single_photo', price_source='agency_bundle_total'),
               dict(revision=2, paid_photo_count=2, media_count=2),                 # a single has exactly one paid asset
               dict(revision=2, product_kind='single_video'),                       # kind disagrees with counts
               dict(revision=2, media_count=3),                                     # counts do not add up
               dict(revision=2, category_folder_id=None)]                           # single without its category
        for index, patch in enumerate(bad):
            with pytest.raises(DBAPIError):
                with c.begin_nested():
                    insert(c, terms_row(binding, item, command_key=f'{index:064d}', **patch))
        # a second approved revision for the same item is refused until the first is superseded
        with pytest.raises(DBAPIError):
            with c.begin_nested():
                insert(c, terms_row(binding, item, revision=2, command_key='9' * 64))
        # idempotency key is unique per account
        with pytest.raises(DBAPIError):
            with c.begin_nested():
                insert(c, terms_row(binding, item, revision=2, status='superseded'))
        with pytest.raises(DBAPIError):   # member of an unknown revision
            with c.begin_nested():
                member(c, item, revision=9)


def test_rows_are_immutable_evidence(world):
    store, engine, item, binding = world
    with engine.begin() as c:
        insert(c, terms_row(binding, item))
        member(c, item)
        for sql in ("UPDATE of_catalogue_terms SET standard_price_cents=1300",
                    "UPDATE of_catalogue_terms SET discount_price_cents=900",
                    "UPDATE of_catalogue_terms SET snapshot_sha256=repeat('0',64)",
                    "UPDATE of_catalogue_terms SET status='bogus'",
                    "DELETE FROM of_catalogue_terms",
                    "UPDATE of_catalogue_terms_member SET kind='video'",
                    "DELETE FROM of_catalogue_terms_member"):
            with pytest.raises(DBAPIError):
                with c.begin_nested():
                    c.execute(text(sql))
        c.execute(text("UPDATE of_catalogue_terms SET status='superseded'"))      # the one legal change
        with pytest.raises(DBAPIError):                                            # and it never goes back
            with c.begin_nested():
                c.execute(text("UPDATE of_catalogue_terms SET status='approved'"))
        with pytest.raises(DBAPIError):
            with c.begin_nested():
                c.execute(text("UPDATE of_catalogue_terms SET status='withdrawn'"))   # a closed revision stays closed
        assert c.scalar(text('SELECT status FROM of_catalogue_terms')) == 'superseded'


def test_membership_is_indexed_for_reverse_ownership_lookup(world):
    store, engine, item, binding = world
    with engine.begin() as c:
        names = {r[0] for r in c.execute(text("SELECT indexname FROM pg_indexes WHERE tablename IN "
            "('of_catalogue_terms','of_catalogue_terms_member')"))}
        assert {'ix_terms_member_media', 'uq_of_catalogue_terms_live', 'ix_of_catalogue_terms_kind'} <= names
        columns = c.scalar(text("SELECT array_to_string(array_agg(a.attname ORDER BY k.ord),',') FROM pg_index i "
            "JOIN pg_class ic ON ic.oid=i.indexrelid CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum,ord) "
            "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum WHERE ic.relname='ix_terms_member_media'"))
        assert columns == 'account_id,media_id'


def grant(binding, **over):
    base = dict(account_id=ACCOUNT, grant_id=str(uuid.uuid4()), fan_id=FAN, binding_id=binding, item_id=0,
                terms_hash='a' * 64, coverage_ref='vault:fixture', expires_at=utcnow() + timedelta(minutes=30),
                used_intent_id=None, created_at=utcnow(), issuance=0, issuance_reason='initial',
                authorization_json='{"kind":"fixture"}')
    base.update(over)
    return base


def put(c, row):
    keys = list(row)
    c.execute(text('INSERT INTO of_engine_offer_grant (' + ','.join(keys) + ') VALUES (' + ','.join(':' + k for k in keys) + ')'), row)


def test_grant_lineage_admits_a_terms_successor_only_with_a_new_hash(world):
    store, engine, item, binding = world
    from conversation_engine.engine import EngineService, SyntheticModel
    EngineService(store, SyntheticModel({})).select_simulated(TENANT, ACCOUNT, FAN)
    with engine.begin() as c:
        first = grant(binding, item_id=item)
        put(c, first)
        for reason, digest in (('terms_revision', 'b' * 64), ('discount_variant', 'c' * 64)):
            savepoint = c.begin_nested()
            put(c, grant(binding, item_id=item, issuance=1, predecessor_grant_id=first['grant_id'],
                         issuance_reason=reason, terms_hash=digest))
            savepoint.rollback()
        # same hash with a terms reason is refused, as is a new hash with an old reason
        for patch in (dict(issuance_reason='terms_revision', terms_hash=first['terms_hash']),
                      dict(issuance_reason='cancelled_unsent', terms_hash='9' * 64),
                      dict(issuance_reason='expired_renewal', terms_hash='9' * 64),
                      dict(issuance_reason='bogus', terms_hash='9' * 64),
                      dict(issuance_reason='discount_variant', terms_hash='9' * 64, binding_id='other'),
                      dict(issuance_reason='discount_variant', terms_hash='9' * 64, issuance=2)):
            with pytest.raises(DBAPIError):
                with c.begin_nested():
                    put(c, grant(binding, item_id=item, issuance=patch.pop('issuance', 1),
                                 predecessor_grant_id=first['grant_id'], **patch))
        # the old same-hash renewal lineage still works exactly as in 0127
        with c.begin_nested():
            put(c, grant(binding, item_id=item, issuance=1, predecessor_grant_id=first['grant_id'],
                         issuance_reason='cancelled_unsent', terms_hash=first['terms_hash']))


def test_downgrade_refuses_to_destroy_terms_or_terms_lineage_then_round_trips_when_empty(world):
    store, engine, item, binding = world
    with engine.begin() as c:
        insert(c, terms_row(binding, item))
        member(c, item)
        with pytest.raises(RuntimeError, match='terms'):
            with c.begin_nested():
                command.downgrade(_config(c), '0133_preparation_retry_state')
        c.execute(text('ALTER TABLE of_catalogue_terms DISABLE TRIGGER of_catalogue_terms_guard'))
        c.execute(text('ALTER TABLE of_catalogue_terms_member DISABLE TRIGGER of_catalogue_terms_member_guard'))
        c.execute(text('DELETE FROM of_catalogue_terms_member'))
        c.execute(text('DELETE FROM of_catalogue_terms'))
        c.execute(text('ALTER TABLE of_catalogue_terms ENABLE TRIGGER of_catalogue_terms_guard'))
        c.execute(text('ALTER TABLE of_catalogue_terms_member ENABLE TRIGGER of_catalogue_terms_member_guard'))
    with engine.begin() as c:
        command.downgrade(_config(c), '0133_preparation_retry_state')
        check_schema(c, expected='0133_preparation_retry_state')
        assert c.scalar(text("SELECT to_regclass('of_catalogue_terms')")) is None
    with engine.begin() as c:
        upgrade(c)
        check_schema(c)

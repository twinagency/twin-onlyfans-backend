"""Bounded creator-scoped catalogue lookup for the offline engine."""
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
import re
import uuid
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from db.models import CatalogItem, VaultItem, Message
from db.onlyfans_delivery_models import ENGINE_TRANSPORT_KINDS, OfDeliveryPolicy
from db.onlyfans_engine_models import OfEngineCatalogueAlias, OfEngineOwner
from onlyfans_read.agency_scope import actor_allowed
from onlyfans_runtime.media_delivery import catalog_terms
from onlyfans_runtime.store import Refused, _id, utcnow

_STOP = frozenset('a an and are can could do for from i id like me of please show some the to want with you your'.split())
_MAX_ITEMS = 200
_MAX_ALIASES = 1000


def normalize(value):
    if type(value) is not str:
        return ''
    return ' '.join(re.findall(r'[^\W_]+', value.casefold(), flags=re.UNICODE))


def content_hash(s, runtime, terms):
    # Aliases describe content, not its mutable price. The full sale terms are
    # separately frozen and rechecked at the existing delivery authority.
    fingerprints = []
    for saved in terms['files']:
        current = s.get(VaultItem, (runtime.account_id, saved['media_id']))
        if (current is None or not current.content_hash or not current.content_hash_kind
                or current.content_hash != saved['content_hash'] or current.kind != saved['kind']
                or current.removed_at is not None):
            return None
        fingerprints.append({'media_id':current.media_id, 'hash':current.content_hash,
                             'hash_kind':current.content_hash_kind})
    content = {'id':terms['item']['id'], 'account_id':terms['item']['account_id'],
        'kind':terms['item']['kind'], 'description':terms['item']['description_for_ai'],
        'media_ids':terms['media_ids'], 'files':terms['files'], 'fingerprints':fingerprints}
    return sha256(json.dumps(content, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str).encode()).hexdigest()


@dataclass(frozen=True)
class SyntheticCatalogueCoverage:
    account_id: str
    binding_id: str
    complete: bool
    evidence_ref: str
    valid_until: datetime
    snapshot_hash: str


@dataclass(frozen=True)
class VaultCatalogueCoverage:
    """Real-provider counterpart: the catalogue was complete as of a synced vault pass (evidence_ref 'vault:...')."""
    account_id: str
    binding_id: str
    complete: bool
    evidence_ref: str
    valid_until: datetime
    snapshot_hash: str


# policy kind -> (coverage type, evidence prefix); one kind can never use the other's coverage.
_COVERAGE = {'synthetic': (SyntheticCatalogueCoverage, 'synthetic:'), 'fansapi': (VaultCatalogueCoverage, 'vault:')}


def _catalogue_version_v2(s, runtime):
    """Coverage hash for a commercial account: aggregated in the database, so no row is transferred and no size makes it None.

    Covers every catalogue row's identity/update/enable state with its live terms revision, the alias set bound to this binding
    and the vault state of every approved member. Old (v1) coverage objects hash differently, so they read as stale (safe).
    """
    items = s.execute(text("""SELECT count(*), md5(coalesce(string_agg(c.id::text || '|' || c.updated_at::text || '|' || c.enabled::text
        || '|' || coalesce(t.revision::text, '-') || '|' || coalesce(t.snapshot_sha256, '-'), ',' ORDER BY c.id), ''))
        FROM catalog_items c LEFT JOIN of_catalogue_terms t ON t.account_id = c.account_id AND t.item_id = c.id AND t.status = 'approved'
        WHERE c.account_id = :a AND c.script_id IS NULL"""), {'a': runtime.account_id}).one()
    aliases = s.execute(text("""SELECT count(*), md5(coalesce(string_agg(alias_id || '|' || item_id::text || '|' || normalized || '|' || content_hash,
        ',' ORDER BY alias_id), '')) FROM of_engine_catalogue_alias WHERE account_id = :a AND binding_id = :b"""),
        {'a': runtime.account_id, 'b': runtime.binding_id}).one()
    files = s.execute(text("""SELECT count(*), md5(coalesce(string_agg(v.media_id::text || '|' || coalesce(v.content_hash, '') || '|' || coalesce(v.content_hash_kind, '')
        || '|' || v.kind || '|' || coalesce(v.removed_at::text, ''), ',' ORDER BY v.media_id), ''))
        FROM vault_items v WHERE v.account_id = :a AND v.media_id IN (SELECT m.media_id FROM of_catalogue_terms_member m
        JOIN of_catalogue_terms t ON t.account_id = m.account_id AND t.item_id = m.item_id AND t.revision = m.revision
        WHERE t.account_id = :a AND t.status = 'approved')"""), {'a': runtime.account_id}).one()
    value = {'v': 'commercial-catalogue.v2', 'binding': runtime.binding_id, 'items': list(items), 'aliases': list(aliases), 'files': list(files)}
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str).encode()).hexdigest()


def _catalogue_version(s, runtime):
    from .commercial_scope import commercial_scope
    if commercial_scope(s, runtime):
        return _catalogue_version_v2(s, runtime)
    rows = s.execute(select(CatalogItem.id, CatalogItem.label, CatalogItem.description_for_ai,
        CatalogItem.kind, CatalogItem.media_ids, CatalogItem.preview_media_ids, CatalogItem.price_cents,
        CatalogItem.enabled, CatalogItem.script_id, CatalogItem.updated_at, CatalogItem.tags)
        .where(CatalogItem.account_id == runtime.account_id).order_by(CatalogItem.id).limit(_MAX_ITEMS + 1)).all()
    alias_rows = s.execute(select(OfEngineCatalogueAlias.item_id, OfEngineCatalogueAlias.normalized,
        OfEngineCatalogueAlias.content_hash).where(OfEngineCatalogueAlias.account_id == runtime.account_id,
        OfEngineCatalogueAlias.binding_id == runtime.binding_id).order_by(OfEngineCatalogueAlias.alias_id)
        .limit(_MAX_ALIASES + 1)).all()
    if len(rows) > _MAX_ITEMS or len(alias_rows) > _MAX_ALIASES:
        return None
    ids = set()
    for row in rows:
        try:
            values = json.loads(row.media_ids or '[]')
            if type(values) is not list or any(type(value) is not int for value in values):
                return None
            ids.update(values)
        except (ValueError, TypeError):
            return None
    if len(ids) > _MAX_ITEMS * 35:
        return None
    files = s.execute(select(VaultItem.media_id, VaultItem.kind, VaultItem.content_hash,
        VaultItem.content_hash_kind, VaultItem.removed_at, VaultItem.updated_at)
        .where(VaultItem.account_id == runtime.account_id, VaultItem.media_id.in_(ids))
        .order_by(VaultItem.media_id)).all() if ids else []
    value = {'binding':runtime.binding_id, 'items':[list(row) for row in rows],
             'aliases':[list(row) for row in alias_rows], 'files':[list(row) for row in files]}
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str).encode()).hexdigest()


def synthetic_coverage(store, tenant, account, *, complete, evidence_ref, valid_until):
    if (type(complete) is not bool or type(evidence_ref) is not str or not evidence_ref.startswith('synthetic:')
            or len(evidence_ref) > 128 or type(valid_until) is not datetime or valid_until.tzinfo is not None
            or valid_until <= utcnow()):
        raise Refused('invalid synthetic catalogue coverage')
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        policy = s.get(OfDeliveryPolicy, account)
        version = _catalogue_version(s, runtime)
        if policy is None or policy.transport_kind != 'synthetic' or version is None:
            raise Refused('synthetic catalogue coverage is unavailable')
        return SyntheticCatalogueCoverage(account, runtime.binding_id, complete, evidence_ref, valid_until, version)


def vault_coverage(store, tenant, account, *, complete, evidence_ref, valid_until):
    if (type(complete) is not bool or type(evidence_ref) is not str or not evidence_ref.startswith('vault:')
            or len(evidence_ref) > 128 or type(valid_until) is not datetime or valid_until.tzinfo is not None
            or valid_until <= utcnow()):
        raise Refused('invalid vault catalogue coverage')
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        policy = s.get(OfDeliveryPolicy, account)
        version = _catalogue_version(s, runtime)
        if policy is None or policy.transport_kind != 'fansapi' or version is None:
            raise Refused('vault catalogue coverage is unavailable')
        return VaultCatalogueCoverage(account, runtime.binding_id, complete, evidence_ref, valid_until, version)


def approve_alias(store, tenant, account, item_id, alias, *, actor):
    _id(item_id)
    normalized = normalize(alias)
    if (type(alias) is not str or not alias.strip() or len(alias.encode()) > 128 or not normalized
            or len(normalized.encode()) > 128 or not actor_allowed(actor, capability='manage')):
        raise Refused('invalid catalogue alias approval')
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        policy = s.get(OfDeliveryPolicy, account)
        if policy is None or policy.transport_kind != 'synthetic':
            raise Refused('catalogue alias approval is limited to the synthetic lab')
        terms = catalog_terms(s, account, item_id)
        digest = content_hash(s, runtime, terms)
        if digest is None:
            raise Refused('catalogue content version is unavailable')
        prior = s.scalar(select(OfEngineCatalogueAlias).where(OfEngineCatalogueAlias.account_id == account,
            OfEngineCatalogueAlias.binding_id == runtime.binding_id, OfEngineCatalogueAlias.item_id == item_id,
            OfEngineCatalogueAlias.normalized == normalized, OfEngineCatalogueAlias.content_hash == digest))
        if prior is not None:
            return prior.alias_id
        alias_id = str(uuid.uuid4())
        s.add(OfEngineCatalogueAlias(account_id=account, alias_id=alias_id, binding_id=runtime.binding_id,
            item_id=item_id, alias=alias.strip(), normalized=normalized, content_hash=digest,
            actor=actor, created_at=utcnow()))
        return alias_id


def aliases_for(s, runtime, item_id, terms):
    digest = content_hash(s, runtime, terms)
    if digest is None:
        return []
    return list(s.scalars(select(OfEngineCatalogueAlias.normalized).where(
        OfEngineCatalogueAlias.account_id == runtime.account_id,
        OfEngineCatalogueAlias.binding_id == runtime.binding_id,
        OfEngineCatalogueAlias.item_id == item_id, OfEngineCatalogueAlias.content_hash == digest)
        .order_by(OfEngineCatalogueAlias.normalized).limit(_MAX_ALIASES + 1)))


def search(store, tenant, account, fan_id, query, coverage=None):
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        return {**search_scoped(s, runtime, fan_id, query, coverage), 'scope':'approved_available_catalogue'}


def search_scoped(s, runtime, fan_id, query, coverage=None):
    account = runtime.account_id
    _id(fan_id)
    if type(query) is not str or len(query.encode()) > 2000:
        raise Refused('invalid catalogue query')
    tokens = list(dict.fromkeys(token for token in normalize(query).split() if len(token) > 1 and token not in _STOP))
    owner = s.get(OfEngineOwner, (account, fan_id))
    policy = s.get(OfDeliveryPolicy, account)
    if (owner is None or owner.binding_id != runtime.binding_id or policy is None
            or policy.transport_kind not in ENGINE_TRANSPORT_KINDS):
        raise Refused('catalogue search scope is unavailable')
    coverage_type, coverage_prefix = _COVERAGE[policy.transport_kind]
    if not tokens:
        return {'state':'unclear', 'items':[], 'coverage':'unknown'}
    coverage_state = 'incomplete'
    if coverage is not None:
        if (type(coverage) is not coverage_type or coverage.account_id != account
                or coverage.binding_id != runtime.binding_id or type(coverage.complete) is not bool
                or type(coverage.evidence_ref) is not str or not coverage.evidence_ref.startswith(coverage_prefix)
                or type(coverage.valid_until) is not datetime or coverage.valid_until.tzinfo is not None
                or type(coverage.snapshot_hash) is not str or len(coverage.snapshot_hash) != 64):
            raise Refused('catalogue coverage scope is invalid')
        if coverage.valid_until <= utcnow() or _catalogue_version(s, runtime) != coverage.snapshot_hash:
            return {'state':'stale', 'items':[], 'coverage':'stale'}
        coverage_state = 'complete' if coverage.complete else 'incomplete'
    from .commercial_scope import commercial_scope
    if commercial_scope(s, runtime):
        # Commercial accounts: scoped, indexed, windowed eligibility instead of the legacy 200-row whole-catalogue view.
        from .catalogue_select import shortlist
        return shortlist(s, runtime, fan_id, tokens, coverage_state)
    rows = list(s.scalars(select(CatalogItem).where(CatalogItem.account_id == account,
        CatalogItem.enabled.is_(True), CatalogItem.script_id.is_(None))
        .order_by(CatalogItem.id).limit(_MAX_ITEMS + 1)))
    if len(rows) > _MAX_ITEMS:
        coverage_state = 'incomplete'
    matches, blocked = [], False
    for item in rows[:_MAX_ITEMS]:
        try:
            tags = json.loads(item.tags or '[]')
            tags = [v for v in tags if type(v) is str] if type(tags) is list else []
        except (TypeError, ValueError):
            tags = []
            coverage_state = 'incomplete'
        base = normalize(' '.join([item.label or '', item.description_for_ai or '', item.kind or '', *tags]))
        try:
            terms = catalog_terms(s, account, item.id, lock=False)
            digest = content_hash(s, runtime, terms)
            alias_rows = s.execute(select(OfEngineCatalogueAlias.normalized, OfEngineCatalogueAlias.content_hash).where(
                OfEngineCatalogueAlias.account_id == account, OfEngineCatalogueAlias.binding_id == runtime.binding_id,
                OfEngineCatalogueAlias.item_id == item.id).limit(_MAX_ALIASES + 1)).all()
            aliases = [name for name, bound in alias_rows if digest is not None and bound == digest]
            stale_aliases = [name for name, bound in alias_rows if (digest is None or bound != digest) and name not in aliases]
            if stale_aliases:
                coverage_state = 'incomplete'
                if any(token in name.split() for name in stale_aliases for token in tokens):
                    blocked = True
            if len(alias_rows) > _MAX_ALIASES:
                coverage_state = 'incomplete'
        except Refused:
            coverage_state = 'incomplete'
            if any(token in base.split() for token in tokens):
                blocked = True
            continue
        if len(aliases) > _MAX_ALIASES:
            coverage_state = 'incomplete'
            aliases = aliases[:_MAX_ALIASES]
        words = set((base + ' ' + ' '.join(aliases)).split())
        score = sum(token in words for token in tokens)
        if score:
            matches.append((score, {'itemId':str(item.id), 'label':item.label or item.description_for_ai,
                'description':item.description_for_ai, 'mediaIds':terms['media_ids'],
                'priceCents':terms['price_cents'], 'currency':'USD'}))
    if matches:
        best = max(score for score, _ in matches)
        result = [item for score, item in sorted(matches, key=lambda pair:(-pair[0], int(pair[1]['itemId']))) if score == best]
        return {'state':'matches', 'items':result[:20], 'coverage':coverage_state,
                'matchQuality':'all_terms' if best == len(tokens) else 'broadened',
                'truncated':len(result) > 20}
    if blocked:
        return {'state':'unavailable', 'items':[], 'coverage':coverage_state}
    return {'state':'confirmed_none' if coverage_state == 'complete' else 'incomplete',
            'items':[], 'coverage':coverage_state}


_CONTEXT_REFERENCES = frozenset({'that one', 'those', 'those ones', 'what about that one', 'what about those', 'what about those ones'})


def search_context(s, runtime, fan_id, source):
    # Use recent *bound inbound* context only for an explicit anaphoric request;
    # do not silently treat arbitrary old messages as a new purchase instruction.
    query, source_id, used = source.body, source.message_id, False
    if normalize(source.body) in _CONTEXT_REFERENCES:
        from sqlalchemy import and_, or_
        from .history_scope import current_binding_message
        earlier = list(s.scalars(select(Message).where(Message.account_id == runtime.account_id,
            Message.fan_id == fan_id, Message.direction == 'in', Message.is_unsent.is_(False),
            current_binding_message(runtime.binding_id), or_(Message.created_at < source.created_at,
                and_(Message.created_at == source.created_at, Message.message_id < source.message_id)))
            .order_by(Message.created_at.desc(), Message.message_id.desc()).limit(5)))
        prior = next((message for message in earlier if normalize(message.body) not in _CONTEXT_REFERENCES), None)
        if prior is not None:
            query, source_id, used = prior.body, prior.message_id, True
    # The catalogue lookup only needs the words of a request. A long message used to make this raise 'invalid catalogue
    # query' and stall the whole turn without a handover; search the first 2000 bytes and say so. The writer still gets
    # the full message (head and tail) and the stop/control checks still read the full raw text.
    truncated = len(query.encode()) > 2000
    if truncated:
        query = query.encode()[:2000].decode('utf-8', 'ignore')
    return {**search_scoped(s, runtime, fan_id, query), 'scope':'approved_available_catalogue',
            'contextUsed':used, 'querySourceMessageId':str(source_id), **({'queryTruncated':True} if truncated else {})}

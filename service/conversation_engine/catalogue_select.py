"""Scoped, indexed, windowed product selection for commercial accounts (Step 3).

The legacy `search_scoped` loads at most 200 catalogue rows and quietly calls everything beyond them "incomplete". For a commercial
account that bound is replaced here: eligibility is one indexed query over live approved terms (kind, enabled, whole-bundle ownership
exclusion, text/alias match), ordered by (rank desc, item id), examined in verification pages of 25 up to a hard window of 200
candidates; the first K=5 that pass the same provenance checks as sending become the shortlist. The model never sees the vault.
If the window is exhausted before K verified products exist the answer is `incomplete` and `truncated`, never `confirmed_none`.
"""
import re

from sqlalchemy import select, text

from db.onlyfans_engine_models import OfEngineCatalogueAlias
from onlyfans_runtime.store import Refused

PAGE, MAX_PAGES, SHORTLIST = 25, 8, 5
WINDOW = PAGE * MAX_PAGES
_VIDEO = frozenset({'video', 'videos', 'vid', 'vids', 'clip', 'clips', 'movie', 'movies'})
_PHOTO = frozenset({'photo', 'photos', 'picture', 'pictures', 'pic', 'pics', 'image', 'images', 'selfie', 'selfies', 'nude', 'nudes'})
_BUNDLE = frozenset({'bundle', 'bundles', 'pack', 'packs', 'collection', 'collections', 'combo', 'set', 'sets'})
_KINDS = {'video': ('single_video', 'bundle_video', 'bundle_mixed'), 'photo': ('single_photo', 'bundle_photo', 'bundle_mixed'),
          'bundle': ('bundle_photo', 'bundle_video', 'bundle_mixed')}


def interpret(tokens):
    """Deterministic request reading from normalised tokens: (search tokens, allowed kinds or None)."""
    words = set(tokens)
    wants = [name for name, group in (('video', _VIDEO), ('photo', _PHOTO), ('bundle', _BUNDLE)) if words & group]
    kinds = None
    if wants:
        sets = [set(_KINDS[w]) for w in wants]
        # photo AND video words together ask for anything; a bundle word narrows to bundles
        if 'video' in wants and 'photo' in wants:
            kinds = set(_KINDS['video']) | set(_KINDS['photo'])
            if 'bundle' in wants:
                kinds &= set(_KINDS['bundle'])
        else:
            kinds = set.intersection(*sets)
    rest = [t for t in tokens if t not in _VIDEO | _PHOTO | _BUNDLE]
    return rest, (sorted(kinds) if kinds else None)


def _tsquery(tokens):
    clean = [re.sub(r'[^\w]', '', t) for t in tokens]
    return ' | '.join(f"'{t}'" for t in clean if t)


_DOC = "to_tsvector('simple', coalesce(c.label,'') || ' ' || coalesce(c.description_for_ai,'') || ' ' || coalesce(c.tags,''))"


def _candidates(s, runtime, tokens, kinds, grams, owned):
    """One indexed query: (item_id, via_alias_only) in (rank desc, id) order, at most WINDOW+1 rows."""
    params = {'a': runtime.account_id, 'b': runtime.binding_id, 'kinds': kinds, 'owned': sorted(owned), 'grams': grams,
              'q': _tsquery(tokens), 'n': WINDOW + 1}
    where_text = f"{_DOC} @@ to_tsquery('simple', :q)" if params['q'] else 'false'
    sql = f"""
    SELECT c.id, ({where_text}) AS by_text, {('ts_rank(' + _DOC + ", to_tsquery('simple', :q))") if params['q'] else '0.0'} AS rank
    FROM of_catalogue_terms t
    JOIN catalog_items c ON c.id = t.item_id AND c.account_id = t.account_id
    WHERE t.account_id = :a AND t.status = 'approved' AND c.enabled AND c.script_id IS NULL AND NOT c.is_free_teaser
      AND (CAST(:kinds AS text[]) IS NULL OR t.product_kind = ANY(CAST(:kinds AS text[])))
      AND NOT EXISTS (SELECT 1 FROM of_catalogue_terms_member m WHERE m.account_id = t.account_id AND m.item_id = t.item_id
                      AND m.revision = t.revision AND m.media_id = ANY(CAST(:owned AS bigint[])))
      AND ({where_text} OR c.id IN (SELECT al.item_id FROM of_engine_catalogue_alias al WHERE al.account_id = :a
                                    AND al.binding_id = :b AND al.normalized = ANY(CAST(:grams AS text[]))))
    ORDER BY rank DESC, c.id
    LIMIT :n"""
    return [(r[0], bool(r[1])) for r in s.execute(text(sql), params).all()]


def _grams(tokens):
    out = []
    for size in (1, 2, 3):
        out += [' '.join(tokens[i:i + size]) for i in range(len(tokens) - size + 1)]
    return out[:64]


def shortlist(s, runtime, fan_id, tokens, coverage_state):
    """The commercial counterpart of search_scoped for already-normalised tokens."""
    from .catalogue import aliases_for
    from .ownership_gate import owned_state
    from .commerce import _real_catalogue_terms
    from onlyfans_runtime.media_delivery import catalog_terms
    from onlyfans_runtime.catalogue_terms import commercial_facts
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    account = runtime.account_id
    owned, unknown = owned_state(s, account, fan_id)
    if unknown:
        return {'state': 'unavailable', 'items': [], 'coverage': coverage_state, 'reason': 'ownership_unknown',
                'productCount': 0, 'mediaCount': 0, 'truncated': False}
    search_tokens, kinds = interpret(tokens)
    if not search_tokens and kinds is None:
        return {'state': 'unclear', 'items': [], 'coverage': 'unknown'}
    grams = set(_grams(search_tokens))
    candidates = _candidates(s, runtime, search_tokens, kinds, sorted(grams), owned) if search_tokens else []
    if not search_tokens and kinds is not None:
        # generic interest in a kind: browse approved products of that kind in a stable order
        candidates = _browse(s, runtime, kinds, owned)
    more_than_window = len(candidates) > WINDOW
    candidates = candidates[:WINDOW]
    policy = s.get(OfDeliveryPolicy, account)
    resolve = _real_catalogue_terms if policy is not None and policy.transport_kind == 'fansapi' else catalog_terms
    verified, blocked, examined = [], False, 0
    for start in range(0, len(candidates), PAGE):
        for item_id, by_text in candidates[start:start + PAGE]:
            examined += 1
            try:
                terms = resolve(s, account, item_id, lock=False)
                facts = commercial_facts(terms)
                if facts is None:
                    continue
                if not by_text and search_tokens:
                    # matched only through an approved alias: it must still be bound to this exact content version
                    if not set(aliases_for(s, runtime, item_id, terms)) & grams:
                        blocked = True
                        continue
                verified.append((item_id, terms, facts))
            except Refused:
                continue
            if len(verified) >= SHORTLIST:
                break
        if len(verified) >= SHORTLIST:
            break
    exhausted = examined >= len(candidates) and not more_than_window
    if len(verified) < SHORTLIST and not exhausted:
        coverage_state = 'incomplete'           # the window ended before we could prove the shortlist or its absence
    truncated = not exhausted
    if not verified:
        state = 'unavailable' if blocked else ('confirmed_none' if coverage_state == 'complete' and exhausted else 'incomplete')
        return {'state': state, 'items': [], 'coverage': coverage_state, 'productCount': 0, 'mediaCount': 0,
                'truncated': truncated}
    scored = []
    for item_id, terms, facts in verified:
        words = set(_words(terms['item'].get('label'), terms['item'].get('description_for_ai'), terms['item'].get('kind')))
        score = sum(token in words for token in search_tokens)
        scored.append((score, item_id, terms, facts))
    best = max(score for score, *_ in scored)
    chosen = [row for row in sorted(scored, key=lambda r: (-r[0], r[1])) if row[0] == best][:SHORTLIST]
    items = [{'itemId': str(item_id), 'label': terms['item'].get('label') or terms['item'].get('description_for_ai'),
              'description': terms['item'].get('description_for_ai'), 'mediaIds': terms['media_ids'],
              'priceCents': terms['price_cents'], 'currency': 'USD', 'productKind': facts['kind'],
              'variant': facts['variant'], 'standardCents': facts['standardCents'], 'counts': facts['counts'],
              'productCount': 1} for _, item_id, terms, facts in chosen]
    return {'state': 'matches', 'items': items, 'coverage': coverage_state,
            'matchQuality': 'all_terms' if best == len(search_tokens) else 'broadened', 'truncated': truncated,
            'productCount': len(items), 'mediaCount': sum(i['counts']['media'] for i in items)}


def _words(*values):
    from .catalogue import normalize
    return ' '.join(normalize(v or '') for v in values).split()


def _browse(s, runtime, kinds, owned):
    sql = """SELECT c.id, true FROM of_catalogue_terms t JOIN catalog_items c ON c.id = t.item_id AND c.account_id = t.account_id
        WHERE t.account_id = :a AND t.status = 'approved' AND c.enabled AND c.script_id IS NULL AND NOT c.is_free_teaser
          AND t.product_kind = ANY(CAST(:kinds AS text[]))
          AND NOT EXISTS (SELECT 1 FROM of_catalogue_terms_member m WHERE m.account_id = t.account_id AND m.item_id = t.item_id
                          AND m.revision = t.revision AND m.media_id = ANY(CAST(:owned AS bigint[])))
        ORDER BY c.id LIMIT :n"""
    return [(r[0], True) for r in s.execute(text(sql), {'a': runtime.account_id, 'kinds': kinds, 'owned': sorted(owned), 'n': WINDOW + 1}).all()]


def price_information(catalogue_view, source_body):
    """Informational prices for a price question, from the fan-scoped shortlist only (max 3). Standard amounts only: a discount is
    never volunteered, it exists only when the fan explicitly asks for a cheaper option."""
    from .writer_context import is_price_question
    if not is_price_question(source_body):
        return []
    items = [i for i in (catalogue_view or {}).get('items', []) if isinstance(i, dict) and i.get('productKind')][:3]
    return [{'itemId': i['itemId'], 'label': i['label'], 'priceCents': i['standardCents'], 'currency': 'USD',
             'kind': i['productKind'], 'counts': i['counts']} for i in items]

"""Pure action authority over explicit products and verified fan state.

No clocks, turn-count ladders, keyword gates, model-selected prices or sender.
The caller must freeze/revalidate the returned fingerprint at the actual wire.
"""
from copy import deepcopy
from .configuration import ActionBlocked, InvalidConfiguration, digest


def _positive_int(value):
    return type(value) is int and value > 0


class ProductAuthority:
    def __init__(self, configuration, products, sequences):
        self.configuration = configuration
        self.products = {}
        for original in products:
            p = deepcopy(original)
            required = {'id', 'revision', 'category', 'price_cents', 'currency', 'assets',
                        'source', 'approval_ref', 'available', 'requires', 'caption_instructions'}
            if (type(p) is not dict or set(p) != required or type(p['id']) is not str or not p['id']
                    or p['id'] in self.products or not _positive_int(p['revision'])
                    or not _positive_int(p['price_cents']) or p['currency'] != 'USD'
                    or type(p['category']) is not str or not p['category']
                    or type(p['available']) is not bool or type(p['requires']) is not dict
                    or any(type(k) is not str or not k or type(v) not in (bool, str, int) for k, v in p['requires'].items())
                    or type(p['approval_ref']) is not str or not p['approval_ref'].strip()
                    or type(p['caption_instructions']) is not str):
                raise InvalidConfiguration('invalid authorised product')
            from .configuration import _source
            _source(p['source'], configuration.foundation['documents'])
            if type(p['assets']) is not list or not p['assets']:
                raise InvalidConfiguration('product assets required')
            seen = set()
            for asset in p['assets']:
                if (type(asset) is not dict or set(asset) != {'id', 'revision', 'facts', 'evidence_ref'}
                        or not _positive_int(asset['id']) or asset['id'] in seen
                        or not _positive_int(asset['revision']) or type(asset['facts']) is not list
                        or not asset['facts'] or any(type(f) is not str or not f.strip() for f in asset['facts'])
                        or type(asset['evidence_ref']) is not str or not asset['evidence_ref'].strip()):
                    raise InvalidConfiguration('verified internal asset facts required')
                seen.add(asset['id'])
            self.products[p['id']] = p
        if type(sequences) is not dict:
            raise InvalidConfiguration('sequence mapping must be an object')
        self.sequences = deepcopy(sequences)
        for name, sequence in self.sequences.items():
            if (type(sequence) is not list or not sequence or len(set(sequence)) != len(sequence)
                    or any(item not in self.products for item in sequence)):
                raise InvalidConfiguration('invalid ordered sequence:' + name)
        self.fingerprint = digest({'config': configuration.fingerprint, 'products': products, 'sequences': sequences})

    def next_step(self, sequence, state):
        if sequence not in self.sequences:
            raise ActionBlocked('sequence_mapping_missing')
        if state.get('purchase_links_verified') is not True:
            raise ActionBlocked('exact_purchase_links_missing')
        purchased = state.get('purchased_products', [])
        for product in self.sequences[sequence]:
            if product not in purchased:
                return product
        return None

    def terms(self, product_id, state, *, purpose='offer', sequence=None):
        if purpose not in {'offer', 'resend', 'alternative', 'continuation'}:
            raise ActionBlocked('unsupported_commercial_action')
        p = self.products.get(product_id)
        if p is None:
            raise ActionBlocked('product_mapping_missing')
        if not p['available']:
            raise ActionBlocked('product_unavailable')
        if state.get('ownership_verified') is not True:
            raise ActionBlocked('ownership_evidence_missing')
        if state.get('commerce_uncertain') is not False:
            raise ActionBlocked('commerce_needs_reconciliation')
        for flag in ('human_owned', 'safety_hold', 'financial_hardship', 'ordinary_chat_only', 'sales_refused'):
            if state.get(flag) is not False:
                raise ActionBlocked('sales_state_held:' + flag)
        if state.get('no_push') is not False and purpose != 'resend':
            # A no-push exception permits an explicit direct request, never an
            # unsolicited suggestion manufactured by a timer or quota.
            if state.get('direct_request_product') != product_id:
                raise ActionBlocked('no_push')
        banned = self.configuration.resolve('prohibited_categories', state).value
        if p['category'] in banned:
            raise ActionBlocked('creator_content_prohibited')
        for key, expected in p['requires'].items():
            if state.get(key) is None:
                raise ActionBlocked('condition_unknown:' + key)
            if type(state[key]) is not type(expected) or state[key] != expected:
                raise ActionBlocked('condition_not_met:' + key)
        permission = self.configuration.resolve('product:' + p['category'], state).value
        from .pricing import validate_price
        validate_price(permission, p['price_cents'])
        for key, expected in permission.get('requires', {}).items():
            if state.get(key) is None:
                raise ActionBlocked('condition_unknown:' + key)
            if type(state[key]) is not type(expected) or state[key] != expected:
                raise ActionBlocked('condition_not_met:' + key)
        assets = {a['id'] for a in p['assets']}
        if assets & set(state.get('owned_assets', [])) or product_id in state.get('purchased_products', []):
            raise ActionBlocked('already_owned_use_existing_access')
        if product_id in state.get('rejected_products', []):
            raise ActionBlocked('product_previously_rejected')
        if purpose == 'resend' and state.get('requested_resend_product') != product_id:
            raise ActionBlocked('resend_not_requested')
        # Prior exposure is distinct from ownership. Only selected paid attachments
        # are checked here: reusing a free preview does not sell that preview.
        if purpose != 'resend' and state.get('exposure_history_verified') is False:
            raise ActionBlocked('media_exposure_history_unreadable')
        paid_assets=set(state.get('paid_assets_by_product', {}).get(product_id, assets))
        if purpose != 'resend' and paid_assets & set(state.get('previously_sent_assets', [])):
            raise ActionBlocked('media_previously_sent_requires_explicit_resend')
        if purpose == 'continuation' and sequence is None:
            raise ActionBlocked('continuation_sequence_missing')
        memberships = [name for name, steps in self.sequences.items() if product_id in steps]
        if memberships and sequence not in memberships:
            raise ActionBlocked('sequence_context_missing')
        if sequence is not None and self.next_step(sequence, state) != product_id:
            raise ActionBlocked('sequence_step_not_current')
        budget = state.get('budget_cents')
        if budget is not None and (type(budget) is not int or budget < p['price_cents']):
            raise ActionBlocked('price_exceeds_budget')
        if product_id in state.get('pending_products', []) and purpose != 'resend':
            raise ActionBlocked('offer_already_pending')
        return {'product': deepcopy(p), 'authority_fingerprint': self.fingerprint,
                'purpose': purpose, 'sequence': sequence}

    def accepted_invitation(self, invitation, state):
        if (type(invitation) is not dict or set(invitation) != {'product_id', 'fingerprint', 'sequence'}
                or invitation['fingerprint'] != self.fingerprint):
            raise ActionBlocked('invitation_stale_or_ambiguous')
        # Acceptance authorises a paid card only. No payment/charge capability
        # exists in this authority.
        return self.terms(invitation['product_id'], state, sequence=invitation['sequence'])


def readiness(configuration, *, account_bound, products, sequences, operational, coordination_verified, pilot=None):
    """No account enablement is inferred from a green readiness result."""
    blockers = []
    if account_bound is not True:
        blockers.append('account_binding_missing')
    if not products:
        blockers.append('authorised_product_asset_mapping_missing')
    if not sequences and pilot is None:
        blockers.append('authorised_vault_sequence_missing')
    try:
        ProductAuthority(configuration, products, sequences)
    except (InvalidConfiguration, TypeError, KeyError):
        blockers.append('invalid_product_or_sequence_mapping')
    if coordination_verified is not True:
        blockers.append('external_automation_coordination_unverified')
    for field in configuration.creator.get('required_operational_fields', []):
        if field == 'vault_sequences' and pilot is not None and not sequences:
            continue  # Only standalone pilot products; sequence actions remain blocked.
        if operational.get(field) is None or operational.get(field) == '':
            blockers.append('operational_field_missing:' + field)
    return {'ready': not blockers, 'blockers': blockers, 'configuration_fingerprint': configuration.fingerprint,
            'scope': 'source_configuration_only_not_runtime_or_release_readiness', 'activates_account': False}

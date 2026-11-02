"""Moxy-initiated work, produced only from conditions the sources already define.

Producer inventory (nothing here is a timer, quota or sales strategy):
  fulfilment  an open obligation naming a product that has an approved complimentary
              remedy (complaints/past_promises instructions + settings.approved_remedies).
  purchase    already produced by commerce.py from exact receipts; the writer then
              continues the source sequence, so no second continuation producer exists.
Everything else the engine can enqueue (unattended invitation, scheduled engagement,
welcome offer, promotion) is NOT defined by the common PDFs or either creator sheet.
Those stay unresolved, produce nothing, and are reported with the exact missing source.
"""
import json
from sqlalchemy import select
from db.onlyfans_moxy_models import MoxyFan
from .configuration import ActionBlocked

UNRESOLVED = {
    'invitation': 'No source defines an unattended item invitation; invitations arise from the fan conversation only.',
    'scheduled': 'No source defines scheduled engagement for a creator; none is produced.',
    'welcome_offer': 'No source defines a welcome offer or welcome card; none is produced.',
    'promotion': 'No source defines promotions, free previews, rewards or discounts; none is produced.',
}


def unresolved():
    return dict(UNRESOLVED)


def _remedy(settings, product):
    approval = settings.get('approved_remedies', {}).get(product)
    return type(approval) is dict and approval.get('complimentary') is True


def produce(s, account, settings):
    """Enqueue owed fulfilment events. Returns {'enqueued': [...], 'unresolved': [...]}.

    The event key carries the fan's state revision, so a held or stale attempt is
    retried only after the fan's recorded state actually changes, never on a clock.
    """
    from .pilot import scope
    from .runtime import enqueue
    result = {'enqueued': [], 'unresolved': []}
    if scope(settings) is not None:
        result['unresolved'].append({'producer': 'fulfilment', 'reason': 'pilot_automation_deferred'})
        return result
    product_ids = {p['id'] for p in settings['products']}
    rows = s.scalars(select(MoxyFan).where(MoxyFan.account_id == account,
        MoxyFan.state_json.like('%"obligations"%')).order_by(MoxyFan.fan_id)).all()
    for row in rows:
        for o in json.loads(row.state_json).get('obligations', []):
            product = o.get('product_id')
            if o.get('status') != 'open' or o.get('owner') != 'moxy' or not product:
                continue
            if product not in product_ids:
                result['unresolved'].append({'producer': 'fulfilment', 'fan_id': row.fan_id,
                    'obligation': o['id'], 'reason': 'product_mapping_missing'})
            elif not _remedy(settings, product):
                result['unresolved'].append({'producer': 'fulfilment', 'fan_id': row.fan_id,
                    'obligation': o['id'], 'reason': 'remedy_authority_missing'})
            else:
                identity = enqueue(s, account, row.fan_id, 'fulfilment',
                    f"fulfilment:{o['id']}:{row.revision}",
                    {'obligation_id': o['id'], 'product_id': product, 'source_ref': o.get('source_ref')})
                result['enqueued'].append(identity)
    return result

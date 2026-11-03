"""Source-backed commercial terms, independent of model wording."""
from .configuration import ActionBlocked, digest

def price_bounds(permission):
    if type(permission) is not dict:
        raise ActionBlocked('product_terms_unresolved')
    exact = permission.get('exact_cents')
    lower = permission.get('minimum_cents')
    upper = permission.get('maximum_cents')
    if exact is not None:
        if type(exact) is not int or exact <= 0 or lower is not None or upper is not None:
            raise ActionBlocked('product_terms_unresolved')
        return exact, exact
    if type(lower) is not int or lower <= 0 or (upper is not None and
            (type(upper) is not int or upper < lower)):
        raise ActionBlocked('product_terms_unresolved')
    return lower, upper

def validate_price(permission, price):
    lower, upper = price_bounds(permission)
    if type(price) is not int or price < lower:
        raise ActionBlocked('price_below_authorised_minimum')
    if upper is not None and price > upper:
        raise ActionBlocked('price_differs_from_authorised_fixed' if lower == upper else
                            'price_above_authorised_maximum')

def validate_format(permission, paid):
    """Only paid assets count toward the sheet's product quantity."""
    kinds = ['photo' if v.kind == 'image' else v.kind for v in paid]
    allowed = permission.get('media_kinds')
    if allowed is not None and any(k not in allowed for k in kinds):
        raise ActionBlocked('product_media_format_mismatch')
    required = permission.get('required_kinds', [])
    if any(k not in kinds for k in required):
        raise ActionBlocked('product_media_format_mismatch')
    count = permission.get('photo_count')
    if count and not count[0] <= kinds.count('photo') <= count[1]:
        raise ActionBlocked('product_photo_count_mismatch')

def asset_evidence(item):
    """A URL/metadata refresh does not change product truth; content changes do."""
    from .media_descriptions import effective_description, limitations, operator_corrected
    return digest({'media_id':item.media_id, 'kind':item.kind,
        'content_hash':item.content_hash, 'content_hash_kind':item.content_hash_kind,
        'description':effective_description(item), 'basis':limitations(item),
        'operator_corrected':operator_corrected(item)})

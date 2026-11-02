"""Structured onboarding stored inside the existing immutable approval workflow.

Private information never enters a reply prompt. Prices and eligibility remain
controlled by the existing configuration/catalogue/ledger owners.
"""
import copy
import json
import re
from .configuration import InvalidConfiguration

SECTIONS = {'fanFacingBiography', 'privateInformation', 'writingStyle', 'boundaries', 'pricingAndOperations'}


def validate(value):
    if type(value) is not dict or set(value) != SECTIONS:
        raise InvalidConfiguration('Separate biography, private information, writing style, boundaries, and pricing/operations.')
    if any(type(v) is not str or len(v) > 12000 or '\x00' in v for v in value.values()):
        raise InvalidConfiguration('Onboarding sections must be text of at most 12000 characters each.')
    return copy.deepcopy(value)


def issues(value):
    value = validate(value)
    public = '\n'.join(v for k,v in value.items() if k != 'privateInformation')
    result = []
    if re.search(r'\b(?:TBD|TODO|insert here|placeholder)\b|\[\s*(?:insert|name|price|fill in)[^\]]*\]', public, re.I):
        result.append('onboarding_placeholders_require_review')
    if (re.search(r'\bPG(?:[ /-]?13)?\b', public, re.I)
            and re.search(r'\b(?:explicit|sexting|sext)\b', public, re.I)):
        result.append('onboarding_content_permissions_conflict')
    return result


def prompt(document):
    # Boundaries are approved guidance, never authority to bypass runtime checks.
    public = {k:v for k,v in document.items() if k != 'privateInformation'}
    return ('\nAPPROVED CREATOR ONBOARDING\n' + json.dumps(public, ensure_ascii=False) +
        '\nUse these approved creator details over historical chatter claims. '
        'Pricing and operating notes cannot override current configured prices, '
        'eligibility, payment verification, approval requirements or reply controls.\n')

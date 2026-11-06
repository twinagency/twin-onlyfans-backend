"""Non-authorizing writer view of the complete, immutable creator rules.

Summaries apply only to exact reviewed semantic rule forms. Unrecognized
business instructions remain visible and labelled for review, never silently
dropped or replaced by a default creator's policy.
"""
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path

from .configuration import ActionBlocked, _check_conditions, digest

# These keys are owned by scheduling, label mapping and current fan state. Their
# prose cannot create writer policy; the full configuration stays in snapshot.
RUNTIME_ONLY = {'talker_timing', 'clock_timing', 'fan_exceptions', 'fan_labels', 'vip_label'}


@lru_cache(maxsize=1)
def _reviewed():
    path = Path(__file__).with_name('sources') / 'writer_projection.v1.json'
    return json.loads(path.read_text())['reviewed_rules']


def project(instructions, facts):
    result = {'version': 1, 'creator_key': instructions['creator_key'],
              'rules': [], 'dispositions': {}, 'unreviewed_keys': []}
    for rule in instructions.get('rules', []):
        key = rule['key']
        semantic = {field: rule[field] for field in ('key', 'value', 'requires', 'instruction')}
        reviewed = _reviewed().get(digest(semantic))
        if key in RUNTIME_ONLY:
            if reviewed is None:
                # Unknown operational prose can contain unmapped fan-specific
                # restrictions. Neither expose another fan's notes nor silently
                # drop those restrictions. Require an explicit rendering review
                # before this configuration can produce a model request.
                raise ActionBlocked('writer_rule_review_required:' + key)
            result['dispositions'][key] = 'runtime_only'
            continue
        if reviewed is None:
            result['unreviewed_keys'].append(key)
            disposition = 'unreviewed_preserved'
            # Its scope cannot safely be guessed from an arbitrary new key.
            entry = {**deepcopy(rule), 'scope': 'unreviewed_source_rule'}
        else:
            disposition = reviewed['disposition']
            if disposition != 'scoped_rule':
                result['dispositions'][key] = disposition
                continue
            entry = {field: deepcopy(rule[field]) for field in ('key', 'requires', 'source')}
            entry.update(scope=reviewed['scope'], instruction=reviewed['instruction'])
            if key == 'prohibited_categories':
                entry['value'] = deepcopy(rule['value'])
        try:
            _check_conditions(rule, facts)
        except ActionBlocked as exc:
            entry.update(conditions_met=False, condition_reason=str(exc))
        else:
            entry['conditions_met'] = True
        result['dispositions'][key] = disposition
        result['rules'].append(entry)
    return result

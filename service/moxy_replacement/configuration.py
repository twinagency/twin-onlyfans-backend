"""Versioned source compilation; creator precedence preserves its conditions.

Historical text is never accepted as configuration authority. Private creator
facts and unresolved placeholders cannot enter the writing context.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path


class InvalidConfiguration(ValueError):
    pass


class ActionBlocked(ValueError):
    """An affected action is unavailable; unrelated chat remains possible."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _source(source, documents):
    if type(source) is not dict or set(source) != {'document', 'pages'}:
        raise InvalidConfiguration('source document and pages required')
    if type(source['document']) is not str:
        raise InvalidConfiguration('invalid source document')
    document = documents.get(source['document'])
    pages = source['pages']
    if (document is None or type(pages) is not list or not pages
            or any(type(p) is not int or not 1 <= p <= document['pages'] for p in pages)
            or len(set(pages)) != len(pages)):
        raise InvalidConfiguration('invalid source page')


def _rules(rules, documents):
    if type(rules) is not list:
        raise InvalidConfiguration('rules must be a list')
    result = {}
    for rule in rules:
        if (type(rule) is not dict or set(rule) != {'key', 'value', 'requires', 'source', 'instruction'}
                or type(rule['key']) is not str or not rule['key']
                or type(rule['instruction']) is not str or not rule['instruction'].strip()
                or type(rule['requires']) is not dict
                or any(type(k) is not str or not k or type(v) not in (bool, str, int) for k, v in rule['requires'].items())):
            raise InvalidConfiguration('invalid rule')
        if rule['key'] in result:
            raise InvalidConfiguration('same-source ambiguity: ' + rule['key'])
        _source(rule['source'], documents)
        result[rule['key']] = deepcopy(rule)
    return result


@dataclass(frozen=True)
class Resolution:
    key: str
    value: object
    source: dict
    authority: str


class CreatorConfiguration:
    def __init__(self, foundation, creator):
        if (type(foundation) is not dict or foundation.get('version') != 1
                or type(creator) is not dict or creator.get('version') != 1):
            raise InvalidConfiguration('unsupported configuration version')
        self.foundation, self.creator = deepcopy(foundation), deepcopy(creator)
        documents = self.foundation.get('documents', {})
        if type(documents) is not dict or not documents:
            raise InvalidConfiguration('source manifest required')
        for doc in documents.values():
            if (type(doc) is not dict or type(doc.get('pages')) is not int or doc['pages'] < 1
                    or type(doc.get('sha256')) is not str or len(doc['sha256']) != 64):
                raise InvalidConfiguration('invalid source manifest')
        self.common = _rules(self.foundation.get('rules'), documents)
        self.overrides = _rules(self.creator.get('rules'), documents)
        if not self.creator.get('creator_key') or type(self.creator.get('unresolved')) is not list:
            raise InvalidConfiguration('creator identity and unresolved register required')
        self.fingerprint = digest({'foundation': self.foundation, 'creator': self.creator})

    def resolve(self, key, facts):
        if key in self.overrides:
            rule, authority = self.overrides[key], 'creator'
        elif key in self.common:
            rule, authority = self.common[key], 'foundation'
        else:
            raise ActionBlocked('instruction_missing:' + key)
        for field, expected in rule['requires'].items():
            if field not in facts or facts[field] is None:
                raise ActionBlocked('condition_unknown:' + field)
            if type(facts[field]) is not type(expected) or facts[field] != expected:
                # Conditional overrides do not fall back to a less restrictive
                # common instruction when the creator's condition is unmet.
                raise ActionBlocked('condition_not_met:' + field)
        return Resolution(key, deepcopy(rule['value']), deepcopy(rule['source']), authority)

    def public_context(self):
        # Only rule text deliberately mapped as instructions is exposed. The
        # creator sheet's internal biography and fan notes stay outside prompts.
        effective = {**self.common, **self.overrides}
        return {'version': 1, 'creator_key': self.creator['creator_key'],
                'fingerprint': self.fingerprint,
                'rules': list(deepcopy(effective).values()),
                'unresolved': deepcopy(self.creator['unresolved'])}


def load(creator_key, directory=None):
    root = Path(directory) if directory else Path(__file__).with_name('sources')
    if type(creator_key) is not str or not creator_key.replace('_', '').isalnum():
        raise InvalidConfiguration('invalid creator key')
    return CreatorConfiguration(json.loads((root / 'foundation.v1.json').read_text()),
                                json.loads((root / (creator_key + '.v1.json')).read_text()))

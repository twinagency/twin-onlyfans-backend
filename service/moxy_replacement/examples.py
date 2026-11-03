"""Labelled writing references, never authority for historical product claims."""
import json
from pathlib import Path


def references(creator):
    data=json.loads((Path(__file__).with_name('sources')/'conversation_examples.v1.json').read_text())
    return {'scope':data['scope'],'examples':[e for e in data['examples'] if e['creator']==creator]}

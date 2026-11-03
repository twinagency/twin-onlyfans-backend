"""Labelled writing references, never authority for historical product claims."""
import json
from pathlib import Path


def references(creator):
    """Model-facing projection: lessons and message lines only.

    Source message ids, archive paths and dates stay in the file as provenance;
    they are not sent to the model, keeping the context budget for inventory.
    """
    data=json.loads((Path(__file__).with_name('sources')/'conversation_examples.v1.json').read_text())
    return {'scope':data['scope'],'examples':[
        {'kind':e.get('kind','positive'),'lesson':e['lesson'],'lines':[[m['direction'],m['text']] for m in e['context']]}
        for e in data['examples'] if e['creator']==creator]}

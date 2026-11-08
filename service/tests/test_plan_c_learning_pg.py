"""Stored, scoped learner evidence must reach the real rendered writer prompt."""
from sqlalchemy.orm import Session
from test_profile_learning_pg import pg_store, run as learn_step
from test_onlyfans_foundation_pg import database
from test_profile_learning_queue import add_message, fan_due
from onlyfans_runtime.profile_learning.storage import reply_memory
from conversation_engine.writer_context import CreatorProfile, build_context
from conversation_engine.prompt_provider_a_original import SpecPromptProvider


def test_stored_learning_is_rendered_and_does_not_cross_fans(pg_store, monkeypatch):
    monkeypatch.setenv('ONLYFANS_PROFILE_LEARNING_ENABLED','1')
    add_message(pg_store,body='I enjoy tennis')
    fan_due(pg_store)
    for _ in range(3):
        learn_step(pg_store)
    profile=CreatorProfile(name='Fixture',limits=('No meetups',))
    def rendered(fan):
        with Session(pg_store.engine) as session:
            learned=reply_memory(session,'of-1',fan,query='tennis')
        snapshot={'current_message':{'id':99,'body':'What sport did I tell you about?'},'history':[],
            'sales_mode':'open','memory':{'state':'current','memory':{'learnedProfile':learned}}}
        return SpecPromptProvider().render(build_context(snapshot,profile)).messages[1]['content']
    assert 'tennis' in rendered(1).lower()
    assert 'tennis' not in rendered(2).lower()

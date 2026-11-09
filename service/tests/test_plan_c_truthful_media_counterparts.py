import json
import pytest
from conversation_engine.model_output import judge,JudgeContext
from conversation_engine.writer_context import CreatorProfile
@pytest.mark.parametrize('body',[
 'I cannot discuss that video because no video is available.',
 'I cannot discuss it because no video is available.',
 'Since no video is available, I cannot discuss that video.',
 'I do not have a video, so we can talk about gardening.',
 'I cannot discuss private details, but since I have no photos, we can talk about plants.',
])
def test_truthful_media_analysis_and_unrelated_clauses_are_not_conversation_bans(body):
 profile=CreatorProfile(name='Fixture',limits=('No meetups',))
 snapshot={'current_message':{'id':1,'body':'What does that video show?'},'history':[],'sales_mode':'open','offer_reference':{'offers':[]}}
 verdict=judge(json.dumps({'body':body}),JudgeContext(snapshot,profile))
 assert verdict.outcome=='ok',verdict.reasons

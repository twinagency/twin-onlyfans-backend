import pytest
from conversation_engine.writer_context import _history_rows,PromptBudget
@pytest.mark.parametrize('access,send,review,card_message,wording',[
 ('confirmed','accepted',False,'700','access/unlock confirmed'),
 ('revoked','accepted',False,'700','access/unlock revoked'),
 ('confirmed','unknown',False,'700','access/unlock unconfirmed'),
 ('confirmed','accepted',True,'700','access/unlock requires review'),
 ('confirmed','accepted',False,'701','access/unlock unconfirmed'),
])
def test_history_annotation_agrees_with_exact_scoped_card_facts(access,send,review,card_message,wording):
 snap={'current_message':{'id':999},'history':[{'id':700,'direction':'out','body':'Garden photo','price_cents':300,'media_count':1,'paid':False}],
       'offer_reference':{'cards':[{'messageId':card_message,'sendState':send,'accessState':access,'financialReview':review}]}}
 rendered=_history_rows(snap,PromptBudget())
 assert wording in rendered[0][1]
 assert 'financial settlement not established' in rendered[0][1] if access in ('confirmed','revoked') and send=='accepted' and not review and card_message=='700' else True

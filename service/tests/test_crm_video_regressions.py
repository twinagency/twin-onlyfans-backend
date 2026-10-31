from types import SimpleNamespace as N
from dataclasses import replace
import pytest
from onlyfans_runtime.store import Event
from onlyfans_runtime.history_repair import equivalent
from onlyfans_commands.refusals import public_refusal
from onlyfans_read.search import normalize_search
from onlyfans_read.contract import encode_cursor,decode_cursor
from onlyfans_read.errors import BadRequest

def observed(**kw):
    return N(**dict(account_id='creator-a',fan_id=101,message_id=501,direction='out',
        body='<p>Hello &amp; welcome<br />back</p>',price_cents=0,media_ids='[]',**kw))

def test_representation_repair_preserves_content_and_scope():
    message=observed();event=Event(101,501,'2026-10-28T00:00:00Z','out','Hello & welcome\nback')
    assert equivalent(message,event)
    assert message.body=='<p>Hello &amp; welcome<br />back</p>'
    for changed in (replace(event,fan_id=102),replace(event,message_id=502),replace(event,direction='in'),
            replace(event,body='Hello & welcome back'),replace(event,price_cents=300),replace(event,media_ids=(901,))):
        assert not equivalent(message,changed)

def test_only_exact_retained_pack_ids_can_fill_missing_legacy_media():
    message=observed();event=Event(101,501,'2026-10-28T00:00:00Z','out','Hello & welcome\nback',(901,902))
    assert not equivalent(message,event)
    assert equivalent(message,event,saved_media=(901,902))
    assert not equivalent(message,event,saved_media=(902,901))
    assert not equivalent(message,event,saved_media=(901,903))
    message.media_ids='[903]'
    assert not equivalent(message,event,saved_media=(901,902))

@pytest.mark.parametrize('query',['@u318022380','u318022380','318022380','  @U318022380  '])
def test_copied_numeric_handles(query):assert normalize_search(query)=='318022380'

def test_search_is_bounded_and_cursor_binds_account_and_query():
    assert normalize_search(' @Fan.Name ' )=='fan.name'
    with pytest.raises(BadRequest):normalize_search('a'*129)
    with pytest.raises(BadRequest):normalize_search('fan\x00')
    cursor=encode_cursor(account_id='creator-a',last_activity_at=None,fan_id=101,query_key='318022380')
    assert decode_cursor(cursor,account_id='creator-a',query_key='318022380')==(None,101)
    for account,query in [('creator-b','318022380'),('creator-a','someone-else'),('creator-a','')]:
        with pytest.raises(ValueError):decode_cursor(cursor,account_id=account,query_key=query)

def test_conflicts_are_allowlisted_and_never_echo_an_exception():
    assert public_refusal('history_quarantined')[0]=='history_repair_required'
    assert public_refusal('control revision conflict')[0]=='revision_changed'
    assert public_refusal('unresolved_delivery')[0]=='delivery_unresolved'
    assert public_refusal('private provider cookies')[0]=='control_conflict'
    assert 'cookies' not in public_refusal('private provider cookies')[1]

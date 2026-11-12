"""Compressed 429 -> due retry -> compressed success through actual scheduler and ledger."""
import gzip
import zlib
import httpx
import pytest
from test_plan_c_model_retry_due_pg import make_world, database, world, configured_draft, stopped_text, post

@pytest.mark.parametrize('encoding',['gzip','deflate'])
def test_compressed_429_retries_without_new_fan_input_and_sends_once(make_world,encoding):
    w=make_world([(429,{'Retry-After':'30'}),'ok'])
    original=w.provider.handle
    def compressed(request):
        response=original(request)
        raw=(gzip.compress if encoding=='gzip' else zlib.compress)(response.content)
        headers=dict(response.headers);headers['content-encoding']=encoding;headers['content-length']=str(len(raw))
        return httpx.Response(response.status_code,headers=headers,stream=httpx.ByteStream(raw))
    w.provider.transport=httpx.MockTransport(compressed)
    pilot=w.new_pilot()
    post(w,93001)
    w.ticks(1,pilot)
    assert [(c.status,c.outcome) for c in w.calls()]==[('released','http_429')]
    assert not w.w.fans.posts
    w.advance(15);w.ticks(2,pilot)
    assert w.provider.calls==[429]
    w.advance(31);w.ticks(3,w.new_pilot())
    assert w.provider.calls==[429,'ok'] and w.request(93001)[0]=='completed'
    assert [c.outcome for c in w.calls()]==['http_429','verified_ok']
    assert len(w.w.fans.posts)==1 and w.out_messages()==1
    w.advance(600);w.ticks(3,w.new_pilot())
    assert w.provider.calls==[429,'ok'] and len(w.w.fans.posts)==1

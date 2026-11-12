"""Real HTTPX streamed decoding; synthetic responses, no network or credentials."""
import gzip
import json
import zlib
import httpx
import pytest
from conversation_engine.model_client import ChatRequest, ModelHTTPStatus, ModelOutcomeUnknown
from model_transport.openrouter import OpenRouterClient, _MAX_ERROR_BODY

KEY = 'sk-offline-fixture-secret'
REQUEST = ChatRequest(model='fixture/model', messages=({'role':'user','content':'private fixture prompt'},))

class Chunks(httpx.SyncByteStream):
    def __init__(self, raw, fail=False):
        self.raw, self.fail, self.closed, self.read = raw, fail, False, 0
    def __iter__(self):
        for i in range(0, len(self.raw), 127):
            part=self.raw[i:i+127]; self.read += len(part); yield part
        if self.fail:
            raise httpx.ReadError('untrusted response prose '+KEY)
    def close(self):
        self.closed=True

def client(status, raw, encoding='identity', fail=False):
    compressed={'identity':lambda x:x,'gzip':gzip.compress,'deflate':zlib.compress}[encoding](raw)
    stream=Chunks(compressed,fail)
    headers={'content-type':'application/json','content-length':str(len(compressed)),
             'retry-after':'120','x-request-id':'req-decoding-fixture'}
    if encoding!='identity': headers['content-encoding']=encoding
    calls=[]
    def handle(request):
        calls.append(request)
        return httpx.Response(status,headers=headers,stream=stream)
    c=OpenRouterClient(key_env='KEY',environ={'KEY':KEY},transport=httpx.MockTransport(handle))
    return c,stream,calls

@pytest.mark.parametrize('encoding',['identity','gzip','deflate'])
@pytest.mark.parametrize('status',[200,429,503,401])
def test_decoded_once_preserves_result_or_known_http_status(encoding,status):
    data=({'id':'gen-offline-123','model':'fixture/model','provider':'Fixture',
           'choices':[{'message':{'content':'hello'},'finish_reason':'stop'}],
           'usage':{'prompt_tokens':10,'completion_tokens':2,'cost':0.0001}}
          if status==200 else {'error':{'code':status,'metadata':{'provider_name':'Fixture'},'message':KEY}})
    c,stream,calls=client(status,json.dumps(data).encode(),encoding)
    if status==200:
        result=c.chat(REQUEST)
        assert (result.text,result.model,result.provider,result.finish_reason)==('hello','fixture/model','Fixture','stop')
        assert result.usage.cost_microusd==100 and result.generation_id=='gen-offline-123'
    else:
        with pytest.raises(ModelHTTPStatus) as e:c.chat(REQUEST)
        assert e.value.status==status and e.value.retry_after==120
        assert e.value.detail['requestId']=='req-decoding-fixture'
        assert e.value.detail['errorCode']==status
        assert KEY not in json.dumps(e.value.detail)
    assert stream.closed and len(calls)==1

@pytest.mark.parametrize('status',[429,503,401])
def test_malformed_compressed_error_preserves_status_and_delay(status,caplog):
    c,stream,calls=client(status,b'placeholder')
    def handle(request):return httpx.Response(status,headers={'content-encoding':'gzip','retry-after':'120'},stream=Chunks(b'not-gzip '+KEY.encode()))
    c._transport=httpx.MockTransport(handle)
    with pytest.raises(ModelHTTPStatus) as e:c.chat(REQUEST)
    assert e.value.status==status and e.value.retry_after==120
    assert 'DecodingError' in caplog.text and KEY not in caplog.text

@pytest.mark.parametrize('status',[200,429])
def test_interrupted_body_unknown_only_without_known_refusal(status,caplog):
    c,stream,calls=client(status,b'{',fail=True)
    with pytest.raises(ModelOutcomeUnknown if status==200 else ModelHTTPStatus) as e:c.chat(REQUEST)
    if status==429:assert e.value.status==429 and e.value.retry_after==120
    assert stream.closed and len(calls)==1 and 'ReadError' in caplog.text and KEY not in caplog.text

def test_bad_compressed_success_stays_unknown(caplog):
    c,_,_=client(200,b'placeholder')
    c._transport=httpx.MockTransport(lambda r:httpx.Response(200,headers={'content-encoding':'gzip'},stream=Chunks(b'bad '+KEY.encode())))
    with pytest.raises(ModelOutcomeUnknown):c.chat(REQUEST)
    assert 'DecodingError' in caplog.text and KEY not in caplog.text

@pytest.mark.parametrize('encoding',['identity','gzip'])
def test_oversized_error_remains_bounded_and_keeps_headers(encoding):
    c,stream,calls=client(429,b'x'*(_MAX_ERROR_BODY*4),encoding)
    with pytest.raises(ModelHTTPStatus) as e:c.chat(REQUEST)
    assert e.value.status==429 and e.value.retry_after==120
    assert len(json.dumps(e.value.detail))<2000 and len(calls)==1 and stream.closed
    if encoding=='identity':assert stream.read<=_MAX_ERROR_BODY+128

"""Ordinary prompt scope and lossless use of the existing bubble splitter."""
import asyncio
import random
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from automations import ai_chatter as chat
from automations.welcome_chatter_for_info import split_for_bubbles
from db.models import Fan
from native_pilot.engine import NativeEngine


def prompt(**kwargs):
    fan=Fan(account_id='900001',fan_id=900002,real_name='Alex',his_age='28',home_city='Brisbane')
    candidate=chat._Cand(900002)
    candidate.messages=[('in','Can you explain what you meant?')]
    candidate.last_body='Can you explain what you meant?'
    candidate.last_in_at=datetime(2026,10,30,4)
    random.seed(41)
    return chat._build_messages('Approved creator voice. Clear and friendly.',fan,candidate,set(),**kwargs)


def test_off_is_exact_baseline_prompt():
    assert prompt()==prompt(reply_format=False)


def test_format_changes_only_ordinary_scope():
    messages,_=prompt(reply_format=True)
    system=messages[0]['content']
    assert chat.REPLY_FORMAT_INSTRUCTION in system
    assert 'No paragraphs.' not in system
    assert 'YOUR GOAL THIS MESSAGE: find out ONE' not in system
    assert 'Approved creator voice.' in system
    assert 'Ask for clarification only if it is needed' in system
    # Transactional/required special-turn directions retain the original prompt.
    for kind in (chat._TURN_CONTENT_ASK,chat._TURN_PIC_OFFER,chat._TURN_HOOK_UPSELL):
        assert prompt(kind=kind)==prompt(kind=kind,reply_format=True)


@pytest.mark.parametrize('reply',[
    'Thanks!',
    'The first step is to choose a date.\n\nThen confirm the time that suits you.',
    'Sorry, I meant Friday, not Thursday.',
    'Did you mean the morning or the evening?',
    'The price is $12.50. Details: https://example.test/item?id=12&price=12.50',
    'A complete explanation. '*220,
    'First paragraph.\n\nSecond paragraph.\n\nThird paragraph.',
])
def test_complete_reply_preserves_words_prices_links(reply):
    parts=split_for_bubbles(reply,2,preserve_complete=True)
    assert 1<=len(parts)<=2
    assert ''.join(' '.join(parts).split())==''.join(reply.split())


def test_native_format_is_independent_and_excludes_jobs():
    async def run():
        async def chat_stub(**kwargs):raise AssertionError('no inference')
        engine=NativeEngine('900001',900002,chat=chat_stub,send=lambda *a:None,reply_format=True)
        with patch.object(engine,'_fan_unlinked_paid',return_value=False),patch.object(engine,'_resumed_after_last_human',return_value=False):
            cfg=await engine.load_config('900001',{})
            assert cfg['_native_reply_format'] is True
            assert cfg['rhythm_enabled'] is False
            engine._job='post_buy'
            cfg=await engine.load_config('900001',{})
            assert '_native_reply_format' not in cfg
        engine=NativeEngine('900001',900002,chat=chat_stub,send=lambda *a:None)
        with patch.object(engine,'_fan_unlinked_paid',return_value=False),patch.object(engine,'_resumed_after_last_human',return_value=False):
            assert '_native_reply_format' not in await engine.load_config('900001',{'_native_reply_format':True})
    asyncio.run(run())

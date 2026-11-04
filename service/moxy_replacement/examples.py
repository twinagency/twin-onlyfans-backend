"""Approved writing references, chosen by the recent exchange and the creator.

The bank is Jesse's eight approved verbatim excerpts (sources/conversation_examples.v1.json). They
are references for voice, rhythm, expression and natural bubble breaks only: historical names,
prices, availability and promises are never facts about the current fan or catalogue.
"""
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

SOURCE=Path(__file__).with_name('sources')/'conversation_examples.v1.json'
LIMIT=3
# Production shows all eight excerpts. The two held_back records are the generalisation test set: tests flip this
# (or pass exclude_held_back=True) so the model never sees them there.
EXCLUDE_HELD_BACK=False

# Situation signals read from the fan's recent messages. Each key is also a label used by the bank.
SIGNALS={
 'hardship':r"can'?t afford|cannot afford|can not afford|\bbroke\b|behind (on|with|in) (my )?(finances|bills|rent|money)|no money|out of money|"
            r"money('s| is)? tight|tight on money|\bbills\b|\brent\b|lost my job|struggling|paycheck|finances|low on funds|balance is low",
 'spending_limit':r"\bcheaper\b|\bbudget\b|too (much|expensive|pricey)|sticking to|over my limit|\bonly have \$?\d|i can'?t (get|buy|do) (that|it|this)|shouldn'?t be spending|can'?t spend",
 'declined_offer':r"not (really |quite )?(in the mood|feeling (it|frisky|like|the)|today|now)|\bno thanks?\b|\bmaybe later\b|something came up|"
            r"\bi'?ll pass\b|\bnot interested\b|don'?t (want|wanna) (to )?(buy|open|see)|get it (ltr|later)|\bnot right now\b",
 'owned_complaint':r"(already|alr) (have|own|bought|got|paid)|\bi have (this|these|that|it|the)\b|bought (this|that|it)\b|\brepeat(s|ed)?\b|seen (this|that|it) before|"
            r"\bduplicate|not what i (paid|expected)|\brefund|didn'?t (show|get|receive|work)|only (about )?\d+ ?(pics|photos|videos|min)|\bdisappoint",
 'availability_question':r"\bdo (you|u) (do|make|offer|sell|have|ever|take|show|send|film|use)\b|\bcan (you|u) (do|make|show|send)\b|\bcustoms?\b|"
            r"\bwould (you|u) (do|make|show|send)\b|\bany (videos?|pics?|photos?|audio)\b|\bpanties\b|\bare there any\b",
 'content_request':r"\bshow me\b|\bsend (me|it)\b|wanna see|want to see|let me see|what do you have|\bany (new|more)\b|\bmore of (that|this)\b",
 'sexting_scene':r"\b(cock|dick|pussy|suck(ing)?|lick(ing)?|fuck(ing)?|cum(ming)?|horny|naughty|stroke|stroking|throat|tits|boobs?|nude|naked|booty|"
            r"spank|slut|daddy|on your knees|turn(ed)? on|so hot|hard for you)\b|😈|🍆|🥵",
 'compliment':r"beautiful|gorgeous|\bpretty\b|\bsexy\b|stunning|\bcute\b|love (you|your|seeing)|look(s|ing)? (so |really )?(good|great|amazing|beautiful|hot)",
 'personal_chat':r"\b(work|job|gym|workout|lift(ing)?|squats?|leg day|birthday|weekend|plans?|tonight|tomorrow|dinner|lunch|breakfast|ate|eat|cook(ed|ing)?|grill(ed)?|"
            r"game|football|soccer|trip|vacation|kids|family|friends|day off|tired|sleep|morning|how'?s your|how are you|what'?s up|wyd|up to|how was your)\b",
 'clarification':r"\bi (meant|mean)\b|what i (meant|said|mean)|\bno,? (no|i)\b|not what i (meant|said|asked)|\bi was (asking|talking|saying)\b|\bi asked\b|"
            r"answer my question|didn'?t answer|that'?s not what|\bwhat do you mean\b|\bwdym\b|\bnot that\b|\bi said\b",
}
SIGNALS={k:re.compile(v,re.I) for k,v in SIGNALS.items()}
# A detected signal can stand for more than one bank label.
IMPLIES={
 'hardship':('money_trouble_rapport','hardship','spending_limit'),
 'spending_limit':('spending_limit','money_trouble_rapport'),
 'declined_offer':('declined_offer','delayed_purchase','not_in_mood'),
 'owned_complaint':('complaint_recovery','owned_complaint'),
 'availability_question':('availability_question','honest_unavailable_alternative','content_request'),
 'content_request':('content_request','honest_unavailable_alternative'),
 'sexting_scene':('sexting_scene','sales_flow','content_request'),
 'compliment':('compliment',),
 'personal_chat':('personal_chat',),
 'clarification':('clarification',),
}
QUESTION=re.compile(r"\?|^\s*(what|why|how|when|where|who|which|do|does|did|can|could|would|will|is|are)\b",re.I)


@lru_cache(maxsize=1)
def _bank():
    return json.loads(SOURCE.read_text(encoding='utf-8'))


def verbatim(example):
    """The excerpt lines exactly as exported, rebuilt from the stored fields (integrity check)."""
    lines=[]
    for t in example['turns']:
        if t['who']=='marker':lines.append('[%s] $$$ %s'%(t['at'],t['marker']))
        else:lines.append('[%s] %s: %s%s'%(t['at'],'FAN' if t['who']=='fan' else 'CHATTER',t['text'],t.get('media_marker','')))
    return '\n'.join(lines)


def verified(example):
    return hashlib.sha256(verbatim(example).encode()).hexdigest()==example['verbatim_sha256']


def held_back():
    return sorted(e['record'] for e in _bank()['examples'] if e['held_back'])


def situations(history=None,fan_state=None):
    """Weighted situation labels from the recent exchange: the fan's newest message counts most."""
    history=history or []
    burst=[]
    for m in reversed(history):
        if m.get('direction')=='in':burst.append(m)
        elif burst:break
        if len(burst)==3:break
    weights={}
    def add(label,w):weights[label]=max(weights.get(label,0),w)
    for rank,m in enumerate(burst):
        text=m.get('text') or ''
        w=3 if rank==0 else 1
        for name,pattern in SIGNALS.items():
            if pattern.search(text):
                for label in IMPLIES[name]:add(label,w)
        if rank==0 and QUESTION.search(text):
            for label in ('question','honest_unavailable_alternative','personal_chat'):add(label,w if label=='question' else 1)
    fan_state=fan_state or {}
    if fan_state.get('financial_hardship'):
        for label in ('money_trouble_rapport','hardship'):add(label,2)
    if type(fan_state.get('budget_cents')) is int:add('spending_limit',2)
    if fan_state.get('ordinary_chat_only'):add('personal_chat',2)
    return weights


def _allowed(example,creator):
    allowed=example['allowed_creators']
    return '*' in allowed or creator in allowed


def select(creator,history=None,fan_state=None,*,exclude_held_back=False,limit=LIMIT):
    """2 to 3 bank excerpts for the latest exchange and creator.

    Creator fit is a hard filter: an explicit excerpt is never offered to a creator it is not listed
    for (Amelia is a PG account with sexting for spenders only). Same-source creator is a tie-break.
    """
    pool=[e for e in _bank()['examples'] if _allowed(e,creator) and not (exclude_held_back and e['held_back'])]
    weights=situations(history,fan_state)
    def score(e):
        s=sum(weights.get(label,0) for label in e['matches'])
        if weights.get(e['situation']):s+=1
        return s
    def fit(e):return 0.5 if e['creator_in_source'].lower().startswith(creator.split('_')[0].lower()) else 0.0
    ranked=sorted(pool,key=lambda e:(-(score(e)+fit(e)),e['record']))
    chosen=[];seen=set()
    for e in ranked:
        if score(e)<=0 or len(chosen)==limit:break
        # Prefer different situations; a second excerpt of one situation only fills a gap.
        if e['situation'] in seen:continue
        chosen.append(e);seen.add(e['situation'])
    for e in ranked:
        if len(chosen)>=min(limit,2):break
        if e not in chosen:chosen.append(e)
    return chosen,weights


def _lines(example):
    result=[]
    for t in example['turns']:
        if t['who']=='marker':
            result.append(['note','fan payment pending (historical marker)' if '[pending]' in t['marker']
                else 'fan unlocked the previous card (historical marker)'])
        elif t['who']=='fan':result.append(['fan',t['text']])
        else:
            marker=t.get('media_marker','')
            result.append(['chatter_card' if 'PPV' in marker else 'chatter_media' if marker else 'chatter',t['text']])
    return result


def references(creator,history=None,fan_state=None,*,exclude_held_back=None,limit=LIMIT):
    """Model-facing projection: situation, one-line lesson and the message lines, one bubble per line.

    Record numbers, source line ranges, hashes and creator-of-origin stay in the file as provenance.
    """
    data=_bank()
    chosen,weights=select(creator,history,fan_state,exclude_held_back=EXCLUDE_HELD_BACK if exclude_held_back is None else exclude_held_back,limit=limit)
    # A clarification or a question is what the writer must answer first, so those labels always lead.
    labels=sorted(weights,key=lambda k:(-weights[k],k not in ('clarification','question'),k))
    return {'scope':data['scope'],
        'latest_exchange_reads_as':labels[:5],
        'examples':[{'situation':e['situation'],'lesson':e['lesson'],'lines':_lines(e)} for e in chosen]}

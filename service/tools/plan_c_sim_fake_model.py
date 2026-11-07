"""DRY-RUN ONLY: a fake model PROVIDER used with --dry-run.

It is an httpx.MockTransport behind the real OpenRouterClient, so the real client, the real ModelRuntime, the real
ledger and the real validator all run; only the provider is replaced. No network, no key, no cost. It reads the
rendered prompt and returns canned, varied replies in the writer's strict JSON contract. It can inject faults
(rate limit, refusal, empty completion, unknown outcome, model substitution, an invalid draft, a draft that breaks the
operator rubric) on scripted fan turns so every recovery path of the runtime is exercised.

SIMULATED: the replies here are not model output and say nothing about any model's writing quality.
"""
import hashlib
import json
import re

import httpx

OPENERS = 2  # a canned reply never opens like one of her last two


def _h(*parts):
    return int(hashlib.sha256('|'.join(str(p) for p in parts).encode()).hexdigest()[:8], 16)


GREET = ["hey you, good to hear from you", "hi there, glad you wrote", "well hello, you caught me at a good time",
         "hey, nice of you to message me", "hi, I was hoping someone fun would text"]
CHAT = ["that made me smile", "ha, I like how you put that", "honestly that sounds like a good time",
        "okay you have my attention", "talking to you is easy, I like that", "that's sweet of you to say",
        "you're good at this, you know", "I'm enjoying this chat more than I expected", "same here, honestly",
        "that sounds really nice", "mine was pretty relaxed, coffee and music", "I like that about you"]
SEXY = ["mm you're making me blush, keep going", "I like where your head is tonight", "you know exactly what to say",
        "that's getting me warm, not going to lie", "I can picture it and I like it", "slow down, you're winding me up",
        "I'd be lying if I said that didn't turn me on", "you're really good with words, I'm enjoying this",
        "oh that's naughty, and I'm into it", "you're going to get me in trouble with talk like that"]
OFFER = ["since you asked, I have a little set ready, take a look", "I do have something, have a look",
         "okay, here's a peek at what I have", "you asked so nicely, so here it is"]
NO_OFFER = ["no pressure on anything, I'm happy just chatting", "let's just talk for a bit",
            "nothing to push right now, I'm enjoying this", "we can just chat, that's fine by me"]
DECLINE = ["totally fine, no pressure at all", "that's okay, no worries", "of course, we can just chat",
           "all good, I'm not going anywhere", "understood, let's keep chatting"]
STOP = ["got it, no more offers from me, we'll just chat", "okay, I'll leave that alone and just talk with you"]
BOUNDARY = ["I keep things here on this platform, no meeting up and no other apps, and no customs, but I'm happy chatting",
            "that's not something I do, I stay here and keep it to chatting",
            "I don't do meetups or other apps or customs, but I'm enjoying talking with you",
            "that's a no from me, I keep everything on here, still happy to talk"]
THANKS = ["I'm glad you like it, that made my day", "aw that's really sweet, I love hearing that",
          "you're welcome, I'm happy you enjoyed it", "that means a lot to me"]
BYE = ["good night, sleep well", "sleep tight, it was fun", "night, talk soon in here", "goodnight, sweet dreams"]
QUESTIONS = ["what's your favorite way to spend a night off", "what do you do when you can't sleep",
             "what kind of music do you put on after a long day", "what's the last thing that made you laugh",
             "do you cook or just order in", "what's your idea of a perfect lazy morning"]

REFUSAL_TEXT = "I'm sorry, but I can't help with that request."
INVALID_TEXT = {"body": "I made this set just for you, there are six pics and a video", "parts": [], "action": "reply",
                "offer_grant_id": None, "sales_signal": "none", "item_scope": [], "pitch": False,
                "price_claims": [], "purchase_claim": False}
RUBRIC_TEXT = "good night, we'll talk tomorrow"


class FakeModelProvider:
    def __init__(self, *, primary, fallback='', faults=(), prices=(0, 0), fallback_prices=(0, 0), item_id=None):
        self.primary, self.fallback = primary, fallback
        self.faults_by_turn = {}
        for f in faults:
            self.faults_by_turn.setdefault((f['persona'], f['turn']), []).append(list(f['kinds']))
        self.prices, self.fallback_prices = prices, fallback_prices
        self.item_id = item_id          # dry-run oracle: lets the fake model name the item on a clear 'reopen'
        self.pending, self.turn, self.turn_calls = [], (None, 0), 0
        self.calls, self.log = [], []
        self._opened, self._asked, self._counter = {}, {}, 0
        self.transport = httpx.MockTransport(self.handle)

    def set_turn(self, persona, n):
        self.turn, self.turn_calls = (persona, n), 0
        self.pending = [k for kinds in self.faults_by_turn.get((persona, n), []) for k in kinds]

    # ------------------------------------------------------------------ http
    def handle(self, request):
        body = json.loads(request.content)
        model, messages = body['model'], body['messages']
        user = messages[-1]['content']
        self.turn_calls += 1
        call = {'persona': self.turn[0], 'turn': self.turn[1], 'model': model, 'n': self.turn_calls,
                'fault': None, 'prompt_sha256': hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()}
        self.calls.append(call)
        fault = self._fault(model)
        call['fault'] = fault
        if fault == 'rate_limit_primary':
            return httpx.Response(429, json={'error': {'message': 'rate limited (dry run)', 'code': 429}})
        if fault == 'timeout_unknown':
            raise httpx.ReadTimeout('dry run: no answer', request=request)
        returned = model
        if fault == 'model_substitution':
            returned = model + '-routed-dry-run'
        if fault == 'refusal':
            text = REFUSAL_TEXT
        elif fault == 'empty':
            text = ''
        elif fault == 'invalid_then_ok':
            text = json.dumps(INVALID_TEXT)
        elif fault == 'rubric_violation':
            text = json.dumps(self._reply(RUBRIC_TEXT))
        else:
            text = json.dumps(self.canned(user))
        pt = max(1, sum(len(m['content']) for m in messages) // 4)
        ct = max(1, len(text) // 4)
        price_in, price_out = self.fallback_prices if (self.fallback and model == self.fallback) else self.prices
        cost = (pt * price_in + ct * price_out) / 1_000_000 / 1_000_000   # micro-USD per Mtok -> USD
        self.log.append(call)
        return httpx.Response(200, json={
            'model': returned, 'provider': 'DryRunProvider',
            'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': pt, 'completion_tokens': ct, 'cost': cost}})

    def _fault(self, model):
        for kind in list(self.pending):
            if kind == 'rate_limit_primary' and model != self.primary:
                continue
            self.pending.remove(kind)
            return kind
        return None

    # ------------------------------------------------------------------ canned writer
    @staticmethod
    def _reply(text, **extra):
        out = {"body": text, "parts": [], "action": "reply", "offer_grant_id": None, "sales_signal": "none",
               "item_scope": [], "pitch": False, "price_claims": [], "purchase_claim": False}
        out.update(extra)
        return out

    def _pick(self, pool, user, persona):
        self._counter += 1
        recent = self._opened.setdefault(persona, [])
        start = _h(persona, user[-200:], self._counter)
        for i in range(len(pool)):
            line = pool[(start + i) % len(pool)]
            if line.split()[0].lower() not in recent[-OPENERS:]:
                break
        recent.append(line.split()[0].lower())
        return line

    def _question(self, persona):
        asked = self._asked.setdefault(persona, [])
        for q in QUESTIONS:
            if q not in asked:
                asked.append(q)
                return q + '?'
        return None

    def canned(self, user):
        m = re.search(r'LATEST:\nFAN: (.*?)\n\nTHIS TURN:', user, re.S)
        fan = (m.group(1) if m else '').lower()
        persona = self.turn[0] or 'x'
        offer = re.search(r'APPROVED OFFER: id ([0-9a-f-]{36})', user)
        paused = 'SALES STATE: PAUSED' in user
        card = re.search(r'HE IS ASKING ABOUT A CARD ALREADY SENT: (.*?), (\d+) cents', user)
        contents = re.search(r'CONTENTS \(.*?\): (\d+) items, (\d+) pictures, (\d+) videos', user)
        purchased = not re.search(r'VERIFIED PURCHASES: none on record', user) and 'VERIFIED PURCHASES: ' in user and 'unavailable' not in user.split('VERIFIED PURCHASES:')[1][:30]
        pick = lambda pool: self._pick(pool, user, persona)  # noqa: E731
        if re.search(r'stop offering|stop (trying to )?(sell|pitch)|no more offers', fan):
            return self._reply(pick(STOP), sales_signal='decline')
        if re.search(r'too expensive|too much|can.?t afford|not now|maybe later|not interested|still a lot|no thanks', fan):
            return self._reply(pick(DECLINE), sales_signal='decline')
        if re.search(r'meet up|instagram|whatsapp|number|custom|come over|one drink', fan):
            return self._reply(pick(BOUNDARY))
        if re.search(r'how many', fan):
            if contents and (offer or card):
                return self._reply(f"it's {contents.group(2)} pictures and {contents.group(3)} video")
            return self._reply("I'd rather not guess a number, just look at the card when you're ready")
        if card and re.search(r'how much|price|cost', fan):
            return self._reply(f"The {card.group(1)} is $6.00, one fixed price for the whole set", price_claims=[int(card.group(2))])
        if re.search(r'yes.*(send|buy)|i want to buy|want it|send me the set', fan) and paused and self.item_id:
            return self._reply("okay, since you're asking, let me set that up for you", sales_signal='reopen',
                               item_scope=[str(self.item_id)])
        wants = re.search(r'pic|video|vid |vids|sell|show me|send me|want to see|have any|buy|how much|price|want it|set\b|what else', fan)
        if wants and offer and not paused:
            return {**self._reply(pick(OFFER)), 'action': 'offer', 'offer_grant_id': offer.group(1), 'pitch': True}
        if wants and re.search(r'pic|video|vid|sell|buy|price|set\b|what else|show me|send me', fan):
            return self._reply(pick(NO_OFFER))
        if re.search(r'thank|wow|love|so hot|incredible|amazing', fan) and purchased:
            return self._reply(pick(THANKS))
        if re.search(r'good ?night|bye|sleep|goodnight', fan):
            return self._reply(pick(BYE))
        if re.search(r'hard|cock|wet|horny|fuck|naked|dirty|hands|body|touch|bed|close|cum|wearing|unreal|dirty', fan):
            return self._reply(pick(SEXY))
        if re.search(r'^(hi|hello|hey)\b', fan):
            return self._reply(pick(GREET))
        extra = None
        if self._counter % 4 == 0:
            extra = self._question(persona)
        out = self._reply(pick(CHAT))
        if extra and 'QUESTIONS ALREADY ASKED' not in user:
            out['parts'] = [extra]
        return out

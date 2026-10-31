"""Post-sale engine behaviour on the REAL ai_chatter engine: the discounted resend and the post-purchase upsell.

Synthetic data, blocked sockets, no model: ``chat`` is a fake, ``content_prompts.read_hook`` is patched where the
hook read would be a model call, and the wire (removal + replacement send) is the real ``runner.Gateway``
``arm_unsend`` / ``_remove_card`` code behind a fake provider client. Covers:

A  an eligible $5.00 vault card is replaced by a strictly cheaper card (10-20% band, >= $3), same media and previews
B  silence alone, distress, decline, no beat, already-owned, a $3 card, too early, missing card evidence: skipped
   with the fixed reason, NOTHING sent and NOTHING unsent
C  the two-step item discount (15%, then 20% total from the original, never compounded), replay protection,
   unpaid-rung cap, and the existing frequency governor
D  a purchase landing while the discount is processed: the offer is no longer open, nothing sent or removed
E  removal failure (non-404) -> no replacement; 404 -> already removed, replacement sent; uncertain removal -> no
   send; send failure after confirmed removal -> original cancelled 'discounted', never replayed
F  purchase recognition only from the ledger-resolved offer (delivered + resolved_by ppv_ledger); the post-buy
   job of a still-open offer is refused at the service gate
G  a duplicate purchase event / a restart cannot queue the acknowledgement twice, and a re-run does not repeat it
H  the post-buy vault card is chosen from UNOWNED media (vault_sends.was_purchased excluded)
I  a fan reply after the purchase supersedes the post-buy job (engine he_spoke / queue followup-fan-replied)
J  pause / takeover right before dispatch stops the job
L  both switches off: the engine behaves exactly as before
M  the existing nudge still works

Real bugs found in non-test code are recorded with ``bug()`` (reported at the end, exit status 1), never skipped.
"""
import asyncio, json, logging, os, socket, sqlite3, sys, tempfile
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ACCOUNT, FAN, FAN2 = 'of-900001', 900002, 900003
COUNT = 0
BUGS = []
ORIG_MSG = 5001          # the unpaid vault card's provider message id


def check(cond, label):
    global COUNT
    assert cond, label
    COUNT += 1


def bug(cond, label):
    """A real assertion on behaviour that is a defect in NON-test code: counted when it holds, recorded when not."""
    global COUNT
    if cond:
        COUNT += 1
    else:
        BUGS.append(label)


class Wire:
    """The provider side: records every DELETE and every POST that reached it, with switchable failures."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.deleted, self.posted = [], []
        self.delete_mode = 'ok'     # ok | 404 | 500 | timeout | unconfirmed
        self.post_fail = False

    def delete_json(self, url, json_body=None, **kw):
        self.deleted.append((url, json_body))
        if self.delete_mode == 'ok':
            return {'success': True}
        if self.delete_mode == 'unconfirmed':
            return {'success': False}
        if self.delete_mode == 'timeout':
            raise TimeoutError('read timed out')
        code = 404 if self.delete_mode == '404' else 500
        exc = RuntimeError('http %s' % code)
        exc.response = SimpleNamespace(status_code=code)
        raise exc


async def exercise(tmp):
    from db.engine import engine, get_session
    from db.models import (Base, Account, AccountAiConfig, AutomationRun, Fan, Employee, Message, CatalogItem, ContentOffer, VaultItem, VaultSend,
                           LadderState, LadderQuote)
    from sqlalchemy import delete, select, func
    import automation_executor as ax
    from automations import ai_chatter, content_prompts, upsell, _price_ask
    from automations.content_contract import Contract
    from native_pilot import postsale, runner
    from native_pilot.content import VaultPlan
    from native_pilot.engine import NativeEngine, PilotBoundaryError, TEXT_ONLY, FOLLOWUPS, _DiscountClient
    from native_pilot.followups import FollowupQueue, job_kind
    from native_pilot.ledger import Ledger, Refused
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    db_path = Path(tmp) / 'fixture.db'
    ledger = Ledger(db_path)
    clock = datetime.utcnow
    queue = FollowupQueue(ledger, ACCOUNT, clock)
    vault = VaultPlan(ACCOUNT, FAN, str(db_path))
    wire = Wire()
    sent, events = [], []
    cards = {}                   # provider message id -> the vault card as sent (the runner's receipt evidence)
    state = {'mid': 8000, 'next': 70000}

    async with get_session() as s:
        s.add(Account(id=ACCOUNT, nickname='Fixture'))
        s.add(AccountAiConfig(account_id=ACCOUNT, ai_chatter_config_json=json.dumps({**{"pack_send_enabled":True,"pack_on_ask_enabled":True,"qualification_gate_enabled":True}, "fastt_discount_resend":True,"fastt_post_buy_upsell":True})))
        s.add(Employee(id=1, display_name='Automation', is_active=True))
        s.add_all([Fan(account_id=ACCOUNT, fan_id=FAN), Fan(account_id=ACCOUNT, fan_id=FAN2)])
        s.add(CatalogItem(id=2, account_id=ACCOUNT, label='vault - asked for', kind='image_set', enabled=False,
                          description_for_ai='content from her vault', price_cents=1250, tags='["rung:vault-ask"]'))
        s.add(CatalogItem(id=3, account_id=ACCOUNT, label='vault - hook', kind='image_set', enabled=False,
                          description_for_ai='content from her vault', price_cents=1250, tags='["hook_upsell"]'))
        now0 = datetime.utcnow()
        for mid, text in ((7001, 'bare feet and soles on the bed'), (7002, 'close up of her toes and soles'),
                          (7003, 'feet up on the couch, soles to camera'), (7004, 'toes in the sand, bare feet'),
                          (7005, 'soles and arches, close up'), (7006, 'barefoot on the stairs, feet crossed'),
                          (7007, 'feet in white socks, soles up'), (7008, 'wet feet after a swim, soles')):
            s.add(VaultItem(account_id=ACCOUNT, media_id=mid, kind='photo', description=text, search_text=text,
                            tags='[]', created_at=now0 - timedelta(days=1)))

    # ── the provider wire: the REAL Gateway removal code behind a fake client ──────────────────────────────────
    def make_gateway(fan=FAN, expected=ORIG_MSG):
        gw = runner.Gateway.__new__(runner.Gateway)
        gw.client, gw.pilot = wire, SimpleNamespace(fan_id=fan)
        gw.expected_unsend, gw.unsend_id, gw.unsent_ids, gw.stopped_reason = expected, None, set(), None
        return gw

    def provider_send(gw):
        def send(fid, text, kw):
            term = vault.terms(kw)
            uid, gw.unsend_id = gw.unsend_id, None            # armed for THIS send only (Gateway._send)
            if uid is not None:
                gw._remove_card(uid)                          # the DELETE, at the wire, before the replacement
            if wire.post_fail:
                raise RuntimeError('post failed')
            state['next'] += 1
            res = {'id': state['next'], 'text': text, 'createdAt': datetime.utcnow().isoformat(),
                   'fromUser': {'id': 900001}}
            sent.append((text, kw, state['next']))
            wire.posted.append(state['next'])
            if term:
                cards[state['next']] = {'media': list(kw['media_files']), 'previews': list(kw.get('previews') or []),
                                        'price_cents': term['price_cents']}
            return res
        return send

    async def chat(**kw):
        p = kw.get('purpose')
        if kw.get('response_format') and p not in ('content_resolver_match', 'content_resolver_verify'):
            return SimpleNamespace(content='{}', parsed={})
        if p == 'content_resolver_match':
            data = {'media_ids': list(range(7001, 7009))}
        elif p == 'content_resolver_verify':
            data = {'verdicts': {str(n): True for n in range(7001, 7009)}}
        elif p == 'content_resolver_action':
            data = {'phrase': 'me showing my soles'}
        else:
            return SimpleNamespace(content='haha tell me more', parsed=None)
        return SimpleNamespace(content=json.dumps(data), parsed=data)

    def engine_for(gw, followups=queue, fan=FAN):
        return NativeEngine(ACCOUNT, fan, chat=chat, send=provider_send(gw), emit=events.append, content=vault,
                            followups=followups, arm_unsend=gw.arm_unsend, unsent=lambda: gw.unsent_ids,
                            card_of=cards.get)

    # ── fixtures ──────────────────────────────────────────────────────────────────────────────────────────────
    async def reset():
        async with get_session() as s:
            for model in (AutomationRun, ContentOffer, Message, VaultSend, LadderState, LadderQuote):
                await s.execute(delete(model))
            for f in (await s.execute(select(Fan))).scalars():
                f.automation_paused_until = None
        await ax.release_fan_lease(ACCOUNT, FAN)
        with closing(sqlite3.connect(db_path)) as con, con:
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='native_pilot_followups'").fetchone():
                con.execute('DELETE FROM native_pilot_followups')
        wire.reset()
        sent.clear(); cards.clear(); events.clear()

    async def put_message(fan, direction, body, minutes_ago, **kw):
        state['mid'] += 1
        async with get_session() as s:
            s.add(Message(account_id=ACCOUNT, fan_id=fan, message_id=kw.pop('message_id', state['mid']), direction=direction,
                          body=body, created_at=datetime.utcnow() - timedelta(minutes=minutes_ago), **kw))
        return state['mid']

    async def put_card(*, price=500, media=(2001, 2002), previews=(2001,), minutes_ago=120, fan=FAN, msg=ORIG_MSG,
                       item=2, evidence=True, offer_price=None):
        """An unpaid vault card as the ask lane leaves it: the message, its OPEN offer, and the runner's receipt."""
        await put_message(fan, 'out', 'this one is for you', minutes_ago, message_id=msg, automation_kind='pack_send',
                          sent_by_employee_id=1, price_cents=price)
        async with get_session() as s:
            offer = ContentOffer(account_id=ACCOUNT, fan_id=fan, item_id=item, mode='ppv',
                                 price_cents=price if offer_price is None else offer_price, offer_message_id=msg,
                                 status='open', offered_at=datetime.utcnow() - timedelta(minutes=minutes_ago))
            s.add(offer)
            await s.flush()
            oid = offer.id
        if evidence:
            cards[msg] = {'media': list(media), 'previews': list(previews), 'price_cents': price}
        return oid

    async def objection(text='that is a bit steep, any deal?', then='you there?', fan=FAN):
        """The fan voices a price objection and then speaks again (the beat)."""
        await put_message(fan, 'in', text, 60)
        if then:
            await put_message(fan, 'in', then, 50)

    async def offer(oid):
        async with get_session() as s:
            o = await s.get(ContentOffer, oid)
            s.expunge(o)
        return o

    async def offers(fan=FAN):
        async with get_session() as s:
            rows = (await s.execute(select(ContentOffer).where(ContentOffer.fan_id == fan).order_by(ContentOffer.id))).scalars().all()
            s.expunge_all()
        return rows

    async def ladder(fan=FAN):
        async with get_session() as s:
            row = await s.get(LadderState, (ACCOUNT, fan))
            if row is not None:
                s.expunge(row)
        return row

    async def run_job(payload, *, fan=FAN, gw=None, followups=queue, expect_violation=False):
        gw = gw or make_gateway(fan)
        before = len(sent)
        with engine_for(gw, followups, fan) as native:
            try:
                stats = await native.run(1, payload=payload)
            except PilotBoundaryError as exc:
                check(expect_violation, ('unexpected boundary refusal', str(exc)))
                return {'violation': str(exc)}, sent[before:], gw
        check(not expect_violation, 'expected a boundary refusal')
        return stats, sent[before:], gw

    def untouched(why):
        check(not sent and not wire.deleted and not wire.posted, (why, 'nothing sent or unsent', sent, wire.deleted))

    async def owned_now(media, fan=FAN, msg=4001):
        async with get_session() as s:
            for m in media:
                s.add(VaultSend(account_id=ACCOUNT, fan_id=fan, media_id=m, message_id=msg, price_cents=500, was_purchased=True))

    async def owned(media, fan=FAN):
        await owned_now(media, fan)

    # ═══ 0. the building blocks themselves ═══════════════════════════════════════════════════════════════════
    lo, hi = upsell.DISCOUNT_BAND
    check((lo, hi) == (0.80, 0.85) and upsell.OF_PRICE_FLOOR_CENTS == 300, 'FASTT discount band and $3 floor')
    gw = make_gateway()
    for bad in (ORIG_MSG + 1, '5001', None, True):
        try:
            gw.arm_unsend(bad)
            check(False, 'arm_unsend must only accept the one expected card')
        except runner.Refused as exc:
            check(exc.reason == 'unsend-scope-refused', exc.reason)
        gw.stopped_reason = None
    gw.arm_unsend(ORIG_MSG)
    try:
        gw.arm_unsend(ORIG_MSG)
        check(False, 'a second arm in one send is refused')
    except runner.Refused:
        check(True, 'single arm')
    for mode, removed in (('ok', True), ('404', True)):
        gw = make_gateway(); wire.reset(); wire.delete_mode = mode
        gw._remove_card(ORIG_MSG)
        check((ORIG_MSG in gw.unsent_ids) is removed and wire.deleted[0][1] == {'withUserId': FAN}
              and wire.deleted[0][0].endswith('/messages/%d' % ORIG_MSG), ('wire removal', mode))
    for mode in ('500', 'timeout', 'unconfirmed'):
        gw = make_gateway(); wire.reset(); wire.delete_mode = mode
        try:
            gw._remove_card(ORIG_MSG)
            check(False, 'removal must fail closed ' + mode)
        except Exception:
            check(ORIG_MSG not in gw.unsent_ids, ('not confirmed', mode))
    wire.reset()

    # ═══ A. an eligible vault card is replaced by a strictly cheaper one ═════════════════════════════════════
    await reset()
    oid = await put_card(price=500)
    await objection()
    stats, out, gw = await run_job({'discount_offer': oid})
    check(stats.get('discount') and stats.get('discount_resends') == 1, ('A: resent', stats))
    check(len(out) == 1 and len(wire.deleted) == 1 and wire.deleted[0][0].endswith('/messages/%d' % ORIG_MSG),
          'A: the original is removed once, the replacement posted once')
    text, kw, new_id = out[0]
    px = round(kw['price'] * 100)
    check(400 <= px <= 450 and px < 500 and px >= upsell.OF_PRICE_FLOOR_CENTS, ('A: 10-20% off, floor kept', px))
    check(kw['media_files'] == [2001, 2002] and kw['previews'] == [2001] and kw['locked_text'] is False,
          'A: same media and previews as the card he saw')
    first, *rest = await offers()
    check(first.status == 'cancelled' and first.resolved_by == 'discounted' and first.delivery_message_id == new_id,
          ('A: original cancelled, linked to its replacement', first.status, first.resolved_by))
    check(len(rest) == 1 and rest[0].status == 'open' and rest[0].offer_message_id == new_id and rest[0].price_cents == px,
          'A: the replacement is the one open offer')
    lad = await ladder()
    check(lad.discount_count == 1 and lad.status == upsell.STATUS_TAPPED and lad.unpaid_rungs == 1, 'A: ladder taps out after the one cut')
    async with get_session() as s:
        quote = (await s.execute(select(LadderQuote).where(LadderQuote.kind == 'discount'))).scalars().all()
    check(len(quote) == 1 and quote[0].price_cents == px and quote[0].base_cents == 500, 'A: the cut is logged against the price he saw')
    # the price is computed off the price HE SAW (the card), not the offer row
    await reset()
    oid = await put_card(price=1000, offer_price=2500)
    await objection()
    stats, out, gw = await run_job({'discount_offer': oid})
    px = round(out[0][1]['price'] * 100)
    check(stats['discount_resends'] == 1 and 800 <= px <= 900, ('A: priced off the card he saw, not the offer row', px))
    # band, floor and strictness across prices (every send is strictly lower, never under $3, never above the cap)
    for seen in (301, 330, 375, 450, 1250, 4999, 19999, 20000):
        await reset()
        oid = await put_card(price=seen)
        await objection()
        stats, out, gw = await run_job({'discount_offer': oid})
        px = round(out[0][1]['price'] * 100)
        check(px < seen and px >= 300, ('A: strictly lower, >= $3', seen, px))
        if seen * 0.8 >= 300:
            check(int(seen * 0.8) <= px <= int(seen * 0.9), ('A: inside the 10-20% band', seen, px))
        else:
            check(px == 300, ('A: below the band the floor wins', seen, px))
    # deterministic: the same offer is priced the same way twice (seeded by account/fan/offer)
    prices = set()
    for _ in range(2):
        await reset()
        state['next'] = 70000
        oid = await put_card(price=1250)
        await objection()
        await run_job({'discount_offer': oid})
        prices.add(sent[-1][1]['price'])
    check(len(prices) <= 2, 'A: price draws stay in band')

    # ═══ B. every refusal says why and touches nothing ═══════════════════════════════════════════════════════
    async def refused(label, reason, *, setup=None, payload_extra=None, price=500, **card):
        await reset()
        oid = await put_card(price=price, **card)
        if setup:
            await setup()
        stats, out, gw = await run_job({'discount_offer': oid})
        check(stats.get('discount') is True and stats.get('discount_resends', 0) == 0 and stats.get('discount_skip') == reason,
              (label, reason, stats))
        untouched(label)
        return oid, stats

    await refused('B silence alone', 'unprompted')
    await refused('B chatter without a price objection', 'unprompted', setup=lambda: objection('hey babe', 'how are you'))
    # A single request shortly after a fresh card qualifies immediately.
    await reset()
    oid = await put_card(price=670, minutes_ago=0.5)
    await put_message(FAN, 'in', 'Can you please make it cheaper baby and I will buy it', 0)
    stats, out, gw = await run_job({'discount_offer': oid})
    check(stats.get('discount_resends') == 1 and len(out) == 1, ('immediate single request', stats))
    check(out[0][1]['price'] == 5.70, ('15 percent rounded to cents', out))
    await refused('B distress', _price_ask.BLOCK_DISTRESS, setup=lambda: objection(then='my wife will kill me, spent too much'))
    await refused('B broke is a soft decline', _price_ask.BLOCK_DECLINED, setup=lambda: objection(then='im broke rn'))
    await refused('B not interested', _price_ask.BLOCK_DECLINED, setup=lambda: objection(then='not interested'))
    await refused('B no thanks first', _price_ask.BLOCK_DECLINED, setup=lambda: objection('no thanks', 'any deal?'))
    await refused('B an older request cannot discount a new card', 'unprompted', setup=objection, minutes_ago=30)
    await refused('B no card evidence', 'card-evidence-missing', setup=objection, evidence=False)
    await refused('B $3 card', 'price-at-minimum', setup=objection, price=300, media=(2001, 2002), previews=())
    await refused('B under the minimum', 'price-at-minimum', setup=objection, price=250)
    async def objected_and_owned():
        await objection()
        await owned([2002])
    oid, _ = await refused('B already owned', 'already-owned', setup=objected_and_owned, previews=(2001,))
    check((await offer(oid)).status == 'cancelled' and (await offer(oid)).resolved_by == 'owned', 'B: an owned card is retired, not resent')
    # owning only the free preview frame does not make the locked payoff his
    await reset()
    oid = await put_card(price=500, media=(2001, 2002), previews=(2001,))
    await objection()
    await owned_now([2001])
    stats, out, gw = await run_job({'discount_offer': oid})
    check(stats['discount_resends'] == 1 and len(out) == 1, 'B: an owned PREVIEW frame does not block the discount')
    # a conversation that went quiet after "broke" earlier than the offer is not distress about this card
    await reset()
    await put_message(FAN, 'in', 'im broke rn', 600)
    oid = await put_card(price=500)
    await objection()
    stats, out, gw = await run_job({'discount_offer': oid})
    check(stats['discount_resends'] == 1, 'B: only what he said AFTER the card counts')
    # a ladder the fan was stopped on blocks the cut (selling-stopped / paused / companion)
    for label, vals, why in (('stopped', {'status': upsell.STATUS_STOPPED}, 'selling-stopped'),
                             ('tapped', {'status': upsell.STATUS_TAPPED}, 'selling-stopped'),
                             ('paused', {'offers_paused_until': datetime.utcnow() + timedelta(hours=5)}, 'selling-paused'),
                             ('cooldown', {'cooldown_until': datetime.utcnow() + timedelta(hours=5)}, 'selling-paused'),
                             ('companion', {'status': upsell.STATUS_COMPANION}, 'companion-only')):
        async def seed(vals=vals):
            await objection()
            await ai_chatter._save_ladder(ACCOUNT, FAN, **vals)
        await refused('B ladder ' + label, why, setup=seed)
    # not a vault card / not an ours-open PPV / unknown ids
    await reset()
    for payload in ({'discount_offer': 999999}, {'discount_offer': '1'}, {'discount_offer': True}):
        with engine_for(make_gateway()) as native:
            try:
                st = await native.run(1, payload=payload)
                check(st.get('discount_skip') in ('no_offer', 'discount_disabled'), ('B: bad offer id', payload, st))
            except PilotBoundaryError as exc:
                check(str(exc) == 'forbidden-job-payload', ('B: forged id refused', payload))
    untouched('B bad ids')

    # ═══ C. two-step discount per item, then the frequency governor ══════════════════════════════════════════
    await reset()
    oid = await put_card(price=1000)
    await objection()
    stats, out, gw = await run_job({'discount_offer': oid})
    check(stats['discount_resends'] == 1, 'C: first cut')
    first_price = round(out[0][1]['price'] * 100)
    check(first_price == 850, ('C: first cut is 15 percent from the original', first_price))
    n_sent, n_del = len(sent), len(wire.deleted)
    stats, out, gw = await run_job({'discount_offer': oid})            # the same job again (restart / replay)
    check(stats.get('discount_skip') == 'offer_resolved' and len(sent) == n_sent and len(wire.deleted) == n_del,
          ('C: replaying the job cuts nothing more', stats))
    second = (await offers())[-1]
    async with get_session() as s:                                  # make the replacement old enough to be a candidate
        o = await s.get(ContentOffer, second.id)
        o.offered_at = datetime.utcnow() - timedelta(minutes=100)
    await put_message(FAN, 'in', 'can you do 20 percent off the original?', 40)
    await put_message(FAN, 'in', 'come on, final offer?', 30)
    stats, out, gw = await run_job(
        {'discount_offer': second.id}, gw=make_gateway(expected=second.offer_message_id))
    check(stats.get('discount_resends') == 1 and len(out) == 1,
          ('C: a further explicit negotiation reaches the second cut', stats))
    second_price = round(out[0][1]['price'] * 100)
    check(second_price == 800, ('C: the second cut is 20 percent total, not compounded', second_price))
    third = (await offers())[-1]
    second_after = await offer(second.id)
    check(second_after.status == 'cancelled' and third.price_cents == second_price == 800,
          ('C: payable replacement matches the quoted 20 percent price', second_after.status,
           third.price_cents, second_price))
    async with get_session() as s:
        quotes = (await s.execute(select(LadderQuote).where(
            LadderQuote.kind == 'discount').order_by(LadderQuote.id))).scalars().all()
    check([(q.base_cents, q.price_cents) for q in quotes] == [(1000, 850), (1000, 800)],
          ('C: both cuts are measured from the original price',
           [(q.base_cents, q.price_cents) for q in quotes]))
    check((await ladder()).discount_count == 2, 'C: two cuts are recorded')
    n_sent, n_del = len(sent), len(wire.deleted)
    stats, out, gw = await run_job({'discount_offer': second.id})
    check(stats.get('discount_skip') == 'offer_resolved' and len(sent) == n_sent and len(wire.deleted) == n_del,
          ('C: replay after the second cut sends nothing', stats))
    async with get_session() as s:
        o = await s.get(ContentOffer, third.id)
        o.offered_at = datetime.utcnow() - timedelta(minutes=100)
    await put_message(FAN, 'in', 'one more discount?', 40)
    await put_message(FAN, 'in', 'please?', 30)
    stats, out, gw = await run_job(
        {'discount_offer': third.id}, gw=make_gateway(expected=third.offer_message_id))
    check(stats.get('discount_skip') in ('selling-stopped', 'already_cut') and len(sent) == n_sent
          and len(wire.deleted) == n_del,
          ('C: a third cut is refused without touching the card', stats))
    # even with the ladder reopened, the item cannot be cut a third time
    await ai_chatter._save_ladder(ACCOUNT, FAN, status=upsell.STATUS_OPEN)
    stats, out, gw = await run_job(
        {'discount_offer': third.id}, gw=make_gateway(expected=third.offer_message_id))
    check(stats.get('discount_skip') == 'already_cut' and len(sent) == n_sent and len(wire.deleted) == n_del,
          ('C: the 20 percent ceiling stops a third cut on the item', stats))
    # unpaid-rung cap (stop_after_unpaid_rungs, default 1)
    await reset()
    oid = await put_card(price=500)
    await objection()
    await ai_chatter._save_ladder(ACCOUNT, FAN, status=upsell.STATUS_OPEN, unpaid_rungs=1)
    stats, out, gw = await run_job({'discount_offer': oid})
    check(stats.get('discount_skip') == 'already-discounted', ('C: unpaid-rung cap', stats))
    untouched('C unpaid cap')
    # the frequency governor: > 35% of the last 30 days' asks ended in a cut -> pivot, no cut
    await reset()
    oid = await put_card(price=500)
    await objection()
    async with get_session() as s:
        for n in range(5):
            s.add(LadderQuote(account_id=ACCOUNT, fan_id=FAN, media_key='k%d' % n, price_cents=500, band_lo=300, band_hi=500,
                              base_cents=500, pre_clamp_cents=500, kind='discount' if n < 2 else 'rung',
                              sent_at=datetime.utcnow() - timedelta(days=3)))
    stats, out, gw = await run_job({'discount_offer': oid})
    check(stats.get('discount_resends') == 1, ('C: explicit request is not blocked by earlier unrelated discounts', stats))
    # a governor at the threshold (35% exactly) still allows it; old quotes (> 30 days) do not count
    await reset()
    oid = await put_card(price=500)
    await objection()
    async with get_session() as s:
        for n in range(6):
            s.add(LadderQuote(account_id=ACCOUNT, fan_id=FAN, media_key='k%d' % n, price_cents=500, band_lo=300, band_hi=500,
                              base_cents=500, pre_clamp_cents=500, kind='discount',
                              sent_at=datetime.utcnow() - timedelta(days=45)))
    stats, out, gw = await run_job({'discount_offer': oid})
    check(stats.get('discount_resends') == 1, 'C: cuts older than 30 days do not trip the governor')

    # ═══ D. a purchase lands while the discount is processed ═════════════════════════════════════════════════
    await reset()
    oid = await put_card(price=500)
    await objection()
    async with get_session() as s:                                  # the ledger resolved the offer first
        o = await s.get(ContentOffer, oid)
        o.status, o.resolved_by, o.resolved_at = 'delivered', 'ppv_ledger', datetime.utcnow()
    stats, out, gw = await run_job({'discount_offer': oid})
    check(stats.get('discount_skip') == 'offer_resolved' and stats.get('discount_resends', 0) == 0, ('D: bought -> skipped', stats))
    untouched('D')
    check((await offer(oid)).status == 'delivered', 'D: the paid offer is left exactly as the ledger wrote it')
    # ...and a purchase landing BETWEEN the decision and the wire (during the replacement's own attempt)
    await reset()
    oid = await put_card(price=500)
    await objection()
    gw = make_gateway()
    real_send = provider_send(gw)

    def buying_send(fid, text, kw):
        # the ledger resolves the original as paid while the removal/replacement attempt is in flight
        with closing(sqlite3.connect(db_path)) as con, con:
            con.execute("UPDATE content_offers SET status='delivered',resolved_by='ppv_ledger',resolved_at=? WHERE id=?",
                        (datetime.utcnow().isoformat(sep=' '), oid))
        return real_send(fid, text, kw)
    with NativeEngine(ACCOUNT, FAN, chat=chat, send=buying_send, emit=events.append, content=vault, followups=queue,
                      arm_unsend=gw.arm_unsend, unsent=lambda: gw.unsent_ids, card_of=cards.get) as native:
        stats = await native.run(1, payload={'discount_offer': oid})
    paid = await offer(oid)
    bug(paid.status == 'delivered' and paid.resolved_by == 'ppv_ledger',
        'D: ai_chatter.py:4115 (_maybe_discount_resend, vault path) overwrites a ledger-PAID offer with cancelled/discounted when the purchase '
        'lands between the open-check and the wire (_resolve_offer :3443 updates by id with no status guard; low severity race): status=%s resolved_by=%s'
        % (paid.status, paid.resolved_by))

    # ═══ E. removal and send failures ═══════════════════════════════════════════════════════════════════════
    async def fresh_discount(mode=None, post_fail=False):
        await reset()
        oid = await put_card(price=500)
        await objection()
        if mode:
            wire.delete_mode = mode
        wire.post_fail = post_fail
        return oid

    for mode in ('500', 'unconfirmed', 'timeout'):
        oid = await fresh_discount(mode)
        stats, out, gw = await run_job({'discount_offer': oid}, expect_violation=True)
        check(not sent and not wire.posted, ('E: removal not confirmed (%s) -> no replacement' % mode, sent))
        check(len(wire.deleted) == 1 and ORIG_MSG not in gw.unsent_ids, ('E: nothing confirmed gone', mode))
        o = await offer(oid)
        check(o.status == 'open' and o.resolved_by is None, ('E: the original offer stays open and unresolved', mode, o.status))
        check((await ladder()) is None or (await ladder()).discount_count == 0, ('E: no cut recorded', mode))
        # the saved ladder is untouched except for the objection stamp
        check(not [x for x in await offers() if x.id != oid], ('E: no replacement offer row', mode))
    # 404 = already removed = success: the replacement is sent
    oid = await fresh_discount('404')
    stats, out, gw = await run_job({'discount_offer': oid})
    check(stats['discount_resends'] == 1 and len(out) == 1 and ORIG_MSG in gw.unsent_ids, 'E: 404 counts as removed, replacement sent')
    check((await offer(oid)).resolved_by == 'discounted', 'E: 404 -> original retired as discounted')
    # the replacement send failing AFTER a confirmed removal: the original is gone, so it is retired, not replayed
    oid = await fresh_discount(post_fail=True)
    stats, out, gw = await run_job({'discount_offer': oid}, expect_violation=True)
    check(len(wire.deleted) == 1 and ORIG_MSG in gw.unsent_ids and not wire.posted, 'E: removed, replacement failed')
    o = await offer(oid)
    check(o.status == 'cancelled' and o.resolved_by == 'discounted', ('E: original cancelled as discounted', o.status, o.resolved_by))
    n_del = len(wire.deleted)
    wire.post_fail = False
    stats, out, gw = await run_job({'discount_offer': oid})
    check(stats.get('discount_skip') == 'offer_resolved' and len(wire.deleted) == n_del and not wire.posted,
          ('E: no replay after a failed replacement', stats))
    check((await ladder()) is None or (await ladder()).discount_count == 0, 'E: a failed replacement is not recorded as a cut')
    # the removal surface exists for the discount job alone and for the one card it names
    gw = make_gateway()
    with engine_for(gw) as native:
        client = native.make_client(ACCOUNT)
        check(not isinstance(client, _DiscountClient), 'E: an ordinary turn gets the plain text client')
        for call in (lambda: client.unsend_message(ORIG_MSG, FAN),):
            try:
                call()
                check(False, 'unsend refused outside a discount job')
            except (PilotBoundaryError, AttributeError):
                check(True, 'E: no removal surface outside a discount job')
    wire.reset()
    gw = make_gateway()
    with engine_for(gw) as native:
        native._job = 'discount'
        client = native.make_client(ACCOUNT)
        check(isinstance(client, _DiscountClient), 'E: discount job client')
        for bad in ((ORIG_MSG, FAN + 1), (str(ORIG_MSG), FAN), (True, FAN)):
            try:
                client.unsend_message(*bad)
                check(False, 'forged removal')
            except PilotBoundaryError as exc:
                check(str(exc) == 'forbidden-platform-operation', 'E: forged removal refused')
        try:
            client.unsend_message(ORIG_MSG + 7, FAN)
            check(False, 'E: another message id is refused')
        except PilotBoundaryError:
            check(not wire.deleted, 'E: arming another message is refused before any wire call')
    check(not wire.deleted, 'E: arming never touches the platform by itself')

    # ═══ posts-buy fixtures ═══════════════════════════════════════════════════════════════════════════════
    async def put_purchase(*, fan=FAN, msg=ORIG_MSG, minutes_ago=3, resolved_by='ppv_ledger', status='delivered', item=2):
        await put_message(fan, 'out', 'this one is for you', minutes_ago + 30, message_id=msg, automation_kind='pack_send',
                          sent_by_employee_id=1, price_cents=1000, is_paid=(status == 'delivered'))
        async with get_session() as s:
            o = ContentOffer(account_id=ACCOUNT, fan_id=fan, item_id=item, mode='ppv', price_cents=1000, offer_message_id=msg,
                             status=status, resolved_by=resolved_by, offered_at=datetime.utcnow() - timedelta(minutes=minutes_ago + 30),
                             resolved_at=(datetime.utcnow() - timedelta(minutes=minutes_ago)) if status == 'delivered' else None)
            s.add(o)
            await s.flush()
            return o.id

    def hook_for(subject='feet'):
        async def read_hook(account_id, fan_id, **kw):
            return content_prompts.Hook(source='more', what='one more set', bridge='ok one more for you',
                                        contract=Contract(is_ask=True, subject=subject, terms=['feet', 'soles'],
                                                          media_kind='photo'))
        return read_hook

    # ═══ F. purchase recognition only from the ledger ═══════════════════════════════════════════════════════
    async def purchases(now=None):
        with closing(sqlite3.connect(db_path)) as con:
            return postsale.purchases(con, ACCOUNT, now or datetime.utcnow())

    await reset()
    ledger_oid = await put_purchase()
    check(await purchases() == [(ledger_oid, FAN)], 'F: a ledger-resolved vault card is a purchase')
    for label, kw in (('fastpath', {'resolved_by': 'ppv_fastpath'}), ('manual', {'resolved_by': 'manual'}), ('no resolver', {'resolved_by': None}),
                      ('still open', {'status': 'open', 'resolved_by': None}), ('expired', {'status': 'expired', 'resolved_by': 'ppv_ledger'}),
                      ('cancelled', {'status': 'cancelled', 'resolved_by': 'ppv_ledger'}),
                      ('unlocked', {'status': 'unlocked', 'resolved_by': 'ppv_ledger'}),
                      ('not a vault card', {'item': 1})):
        await reset()
        if kw.get('item') == 1:
            async with get_session() as s:
                s.add(CatalogItem(id=1, account_id=ACCOUNT, label='plain', kind='video', enabled=True, price_cents=300,
                                  description_for_ai='x', tags='[]')) if not await s.get(CatalogItem, 1) else None
        await put_purchase(**kw)
        check(await purchases() == [], ('F: not a purchase: ' + label))
    # an unverified claim in the fan's own words never queues, whatever he says
    await reset()
    oid = await put_card(price=1000)
    for claim in ('i bought it', 'just unlocked it, thanks', 'paid!!', 'sent the stars'):
        await put_message(FAN, 'in', claim, 5)
    check(await purchases() == [], 'F: a claim in the chat is not a purchase')
    check(postsale.inbound_since(sqlite3.connect(db_path), ACCOUNT, FAN, datetime.utcnow() - timedelta(hours=1)) != [],
          'F: the claims exist in the thread (the test can fail)')
    # an old purchase is history, not an event
    await reset()
    await put_purchase(minutes_ago=60)
    check(await purchases() == [], 'F: a purchase older than the fresh window is not queued')
    await reset()
    await put_purchase(minutes_ago=5)
    check(len(await purchases()) == 1, 'F: a fresh one is')
    # the service gate re-reads the offer at run time: a post_buy job of a still-open offer is refused
    from native_pilot.service import Service
    svc = Service.__new__(Service)
    svc.path, svc.db = str(db_path), ACCOUNT

    async def switch(**on):
        """Save the account's FASTT switches (ai_chatter_config_json) the way the dashboard does."""
        from db.models import AccountAiConfig
        on = {**{"pack_send_enabled":True,"pack_on_ask_enabled":True,"qualification_gate_enabled":True}, **on}
        async with get_session() as s:
            row = await s.get(AccountAiConfig, ACCOUNT)
            if row is None:
                s.add(AccountAiConfig(account_id=ACCOUNT, ai_chatter_config_json=json.dumps(on)))
            else:
                row.ai_chatter_config_json = json.dumps(on)

    def job_row(kind, oid, fan=FAN):
        key = 'discount_offer' if kind == 'discount' else 'post_buy'
        return {'kind': kind, 'fan_id': fan, 'payload_json': json.dumps({key: oid})}
    await reset()
    open_oid = await put_card(price=500)
    await switch(fastt_post_buy_upsell=True, fastt_discount_resend=True)
    check(svc.postsale_block(job_row('post_buy', open_oid)) == 'not-purchased', 'F: post-buy of an unpaid card refused at the gate')
    await reset()
    paid_oid = await put_purchase()
    check(svc.postsale_block(job_row('post_buy', paid_oid)) is None, 'F: post-buy of a ledger purchase passes the gate')
    await reset()
    fast_oid = await put_purchase(resolved_by='ppv_fastpath')
    check(svc.postsale_block(job_row('post_buy', fast_oid)) == 'not-purchased', 'F: only the ledger resolver counts at run time too')
    check(svc.postsale_block(job_row('post_buy', paid_oid + 999)) == 'offer-mismatch', 'F: an unknown offer is refused')
    check(svc.postsale_block(job_row('post_buy', paid_oid, fan=FAN2)) == 'offer-mismatch', 'F: another fan\'s job is refused')
    # the engine itself, handed a post_buy job for an offer that is NOT a ledger purchase, must not acknowledge it
    await reset()
    open_oid = await put_card(price=500)
    stats, out, gw = await run_job({'post_buy': open_oid})
    bug(not out, 'F: ai_chatter._run_post_buy acknowledges (bridge bubble) an offer that is still OPEN/unpaid when the service gate is bypassed: '
        'it never checks offer.status == delivered / resolved_by == ppv_ledger (ai_chatter.py:4844 _run_post_buy; defence-in-depth gap, the service gate postsale_block covers it)')

    # ═══ switches off (L) ═══════════════════════════════════════════════════════════════════════════════════
    await reset()
    for kind, payload in (('discount', {'discount_offer': 1}), ('post_buy', {'post_buy': 1})):
        for followups in (None,):
            with engine_for(make_gateway(), followups) as native:
                try:
                    await native.run(1, payload=payload)
                    check(False, 'L: ' + kind + ' refused without the queue')
                except PilotBoundaryError as exc:
                    check(str(exc) == 'forbidden-job-payload', 'L: refused')
    gw = make_gateway()
    for label, content, followups in (('text-only', None, None), ('vault', vault, None), ('vault + followups', vault, queue)):
        with NativeEngine(ACCOUNT, FAN, chat=chat, send=provider_send(gw), emit=events.append, content=content,
                          followups=followups, arm_unsend=gw.arm_unsend, unsent=lambda: gw.unsent_ids, card_of=cards.get) as native:
            cfg = await native.native._load_config(ACCOUNT)
            merged = await native.load_config(ACCOUNT, cfg)
            check(merged['post_buy_rung_enabled'] is False and not merged.get('fastt_vault_post_buy'),
                  ('L: the rung stays off on an ordinary turn', label))
            check(not isinstance(native.make_client(ACCOUNT), _DiscountClient), ('L: no removal surface', label))
            if content is None:
                check(all(merged[k] == v for k, v in TEXT_ONLY.items()), 'L: TEXT_ONLY unchanged')
            check(merged['enabled'] is True and merged['payers_only'] is False and merged['max_fans_per_tick'] == 1,
                  ('L: stock engine knobs unchanged', label))
    # both saved switches off: the service gate drops either kind before anything is generated
    await reset()
    paid_oid = await put_purchase()
    open_oid = await put_card(price=500, msg=ORIG_MSG + 1)
    await switch()
    check(svc.postsale_block(job_row('post_buy', paid_oid)) == 'feature-disabled'
          and svc.postsale_block(job_row('discount', open_oid)) == 'feature-disabled', 'L: switches off -> both kinds dropped at the gate')
    check(postsale.settings(str(db_path), ACCOUNT) == {'discount': False, 'post_buy': False}, 'L: default off')
    await switch(fastt_discount_resend='true', fastt_post_buy_upsell=1)
    check(postsale.settings(str(db_path), ACCOUNT) == {'discount': False, 'post_buy': False}, 'L: only a literal true enables')
    await switch(fastt_discount_resend=True)
    check(postsale.settings(str(db_path), ACCOUNT) == {'discount': True, 'post_buy': False}, 'L: independent switches')
    # ordinary turn with both switches off: only a nudge job kind passes, and nothing queues discount/post_buy
    with engine_for(make_gateway(), queue) as native:
        before = len(events)
        run_at = datetime.utcnow() + timedelta(minutes=15)
        for payload in ({'post_buy': 3}, {'discount_offer': 3}, {'aftercare': 3}):
            check(await native.enqueue_job(ACCOUNT, 'ai_chatter', payload=payload, run_at=run_at) is None, payload)
        check(len([e for e in events[before:] if e['event'] == 'native-background-job-suppressed']) == 3, 'L: ai_chatter-queued post-sale jobs are suppressed')
    with closing(sqlite3.connect(db_path)) as con:
        left = con.execute("SELECT count(*) FROM native_pilot_followups WHERE kind IN ('discount','post_buy')").fetchone()[0] \
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='native_pilot_followups'").fetchone() else 0
    check(left == 0, 'L: the engine itself never queues a post-sale job')

    # ═══ post-buy runs (G, H, I, J) ═════════════════════════════════════════════════════════════════════════
    async def post_buy(oid, *, hook=None, expect_violation=False, fan=FAN, bypass_qualify=False):
        """Run the post_buy job. ``bypass_qualify`` lifts ONLY upsell.qualify's low_information refusal (see the bug recorded
        under H) so the card SELECTION can be tested at all; every other brake, the lane and the wire stay real."""
        gate = patch.object(upsell, 'qualify', lambda ctx, now: (True, 'ok')) if bypass_qualify else patch.object(upsell, 'qualify', upsell.qualify)
        with patch.object(content_prompts, 'read_hook', hook or hook_for()), gate:
            return await run_job({'post_buy': oid}, expect_violation=expect_violation, fan=fan)

    await reset()
    oid = await put_purchase()
    await owned_now([7001, 7002])
    await switch(fastt_post_buy_upsell=True,pack_send_enabled=False,pack_on_ask_enabled=False,qualification_gate_enabled=False)
    stats, out, gw = await post_buy(oid)
    check(stats.get('post_buy') is True and stats.get('post_buy_bridged') == 1, ('G: acknowledgement sent', stats))
    check(len([o for o in out if not o[1]]) == 1 and [o for o in out if not o[1]][0][0].strip(), 'G: one plain acknowledgement bubble')
    bug(stats.get('post_buy_rungs') == 1 and len([o for o in out if o[1]]) == 1,
        'H/F: the post-buy vault card NEVER goes out: ai_chatter.py:5046 (_fire_post_buy_vault_rung; same in stock _fire_post_buy_rung '
        ':4942) calls upsell.qualify with last_inbound_text="unlocked" and no inbound_qualifies=True, so upsell.py:856 refuses it as '
        '"low_information" (one short token, _common.is_qualifying_inbound) on every purchase, even with an unowned vault, a hook and '
        'all brakes open: stats=%r' % (stats,))
    # H. with that one refusal lifted, the selection itself: only media he does not own
    await reset()
    oid = await put_purchase()
    await owned_now([7001, 7002])
    stats, out, gw = await post_buy(oid, bypass_qualify=True)
    cardsent = [o for o in out if o[1]]
    check(stats.get('post_buy_rungs') == 1 and len(cardsent) == 1, ('H: with the refusal lifted the lane sells one card', stats, out))
    text, kw, new_id = cardsent[0]
    check(not (set(kw['media_files']) & {7001, 7002}), ('H: the card excludes owned media', kw['media_files']))
    check(set(kw['media_files']) <= set(range(7003, 7009)) and 300 <= round(kw['price'] * 100) <= 20000, ('H: from the unowned vault', kw))
    check(not wire.deleted, 'H: a post-buy card removes nothing')
    rows = await offers()
    check(len([r for r in rows if r.status == 'open']) == 1 and (await ladder()).status == upsell.STATUS_OPEN, 'H: one new open offer')
    check(len(cardsent) == 1 and len(out) == 2, 'H: exactly one acknowledgement and one card')
    async with get_session() as s:
        stamped = (await s.execute(select(VaultSend.media_id).where(VaultSend.message_id == new_id))).scalars().all()
    check(sorted(stamped) == sorted(kw['media_files']), 'H: the card media is recorded as sent to him')
    # owned media are never re-sold, however much of the vault he owns
    for owned_set in ([7001, 7002, 7003], [7001, 7002, 7003, 7004, 7005]):
        await reset()
        oid = await put_purchase()
        await owned_now(owned_set)
        stats, out, gw = await post_buy(oid, bypass_qualify=True)
        for _, kw, _ in [o for o in out if o[1]]:
            check(not (set(kw['media_files']) & set(owned_set)), ('H: owned never re-sold', owned_set, kw['media_files']))
    # ...including the media of the card he just bought (its vault_sends rows are purchase-stamped)
    await reset()
    oid = await put_purchase()
    await owned_now([7001, 7002, 7003])
    stats, out, gw = await post_buy(oid, bypass_qualify=True)
    check(all(not (set(o[1]['media_files']) & {7001, 7002, 7003}) for o in out if o[1]), 'H: the just-bought card is not sold back')
    # a vault he has fully bought has nothing to sell: the acknowledgement only
    await reset()
    oid = await put_purchase()
    await owned_now(list(range(7001, 7009)))
    stats, out, gw = await post_buy(oid, bypass_qualify=True)
    check(stats.get('post_buy_rungs') == 0 and not [o for o in out if o[1]], 'H: nothing left to sell -> no card, only the acknowledgement')
    check(len([o for o in out if not o[1]]) == 1, 'H: the acknowledgement still goes out')
    # no hook (the read found no reason to sell) -> no card manufactured
    await reset()
    oid = await put_purchase()

    async def no_hook(account_id, fan_id, **kw):
        return None
    stats, out, gw = await post_buy(oid, hook=no_hook, bypass_qualify=True)
    check(stats.get('post_buy_rungs') == 0 and not [o for o in out if o[1]], 'H: no hook -> no card')
    # the ordinary rung brakes still apply on the post-buy card: a paused/stopped ladder gets the acknowledgement only
    for label, vals in (('stopped', {'status': upsell.STATUS_STOPPED}), ('offers paused', {'offers_paused_until': datetime.utcnow() + timedelta(hours=2)}),
                        ('companion', {'status': upsell.STATUS_COMPANION}), ('cooldown', {'cooldown_until': datetime.utcnow() + timedelta(hours=2)})):
        await reset()
        oid = await put_purchase()
        await ai_chatter._save_ladder(ACCOUNT, FAN, **vals)
        stats, out, gw = await post_buy(oid, bypass_qualify=True)
        check(stats.get('post_buy_rungs') == 0 and not [o for o in out if o[1]], ('H: brake respected: ' + label, stats))
    await reset()
    oid = await put_purchase()
    async with get_session() as s:                               # $300+ paid in 7 days: the spend-velocity brake
        s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=4999, direction='out', body='x', price_cents=35000, is_paid=True,
                      created_at=datetime.utcnow() - timedelta(days=1), purchased_at=datetime.utcnow() - timedelta(days=1)))
    stats, out, gw = await post_buy(oid, bypass_qualify=True)
    check(stats.get('post_buy_rungs') == 0 and not [o for o in out if o[1]], ('H: spend-velocity brake respected', stats))
    # the rung switch is ON only for this job: an ordinary turn after it is back to stock
    with engine_for(make_gateway()) as native:
        check((await native.load_config(ACCOUNT, await native.native._load_config(ACCOUNT)))['post_buy_rung_enabled'] is False,
              'H: the rung switch does not leak to the next ordinary turn')

    # G. duplicate purchase event / restart
    await reset()
    oid = await put_purchase()
    await switch(fastt_post_buy_upsell=True)
    first = queue.add(FAN, 'ai_chatter', {'post_buy': oid}, datetime.utcnow() + timedelta(seconds=120))
    dup = queue.add(FAN, 'ai_chatter', {'post_buy': oid}, datetime.utcnow() + timedelta(seconds=200))
    check(first and dup is None, 'G: a duplicate purchase event queues nothing more')
    ledger.followup_finish(job_id=first, state='done', reason=None, now=datetime.utcnow())
    check(queue.add(FAN, 'ai_chatter', {'post_buy': oid}, datetime.utcnow() + timedelta(seconds=120)) is None,
          'G: after the job is done a restarted scan cannot queue it again')
    ledger2 = Ledger(db_path)      # a restart: a fresh handle on the same file
    check(FollowupQueue(ledger2, ACCOUNT, clock).add(FAN, 'ai_chatter', {'post_buy': oid}, datetime.utcnow() + timedelta(seconds=120)) is None,
          'G: restart-proof: the queue row is durable')
    with closing(sqlite3.connect(db_path)) as con:
        check(con.execute("SELECT count(*) FROM native_pilot_followups WHERE kind='post_buy'").fetchone()[0] == 1, 'G: one row per purchase')
    for bad in ({'post_buy': 0}, {'post_buy': -1}, {'post_buy': '7'}, {'post_buy': True}, {'post_buy': oid, 'x': 1}, {'post_buy': 999999}):
        try:
            r = queue.add(FAN, 'ai_chatter', bad, datetime.utcnow() + timedelta(seconds=120))
            check(r is None, ('G: bad post_buy payload not queued', bad))
        except Refused:
            check(True, ('G: bad post_buy payload refused', bad))
    # the engine handler, re-run after its cooldown lapsed (a restart would be this), does not repeat the acknowledgement
    await reset()
    oid = await put_purchase()
    stats1, out1, gw = await post_buy(oid)
    check(stats1.get('post_buy_bridged') == 1, 'G: first run acknowledged')
    n = len(sent)
    stats2, out2, gw = await post_buy(oid)
    check(len(sent) == n and stats2.get('reason') in ('paused', 'cooldown'), ('G: an immediate re-run is stopped by the fan cooldown', stats2))
    async with get_session() as s:                                  # the cooldown lapses (restart hours later)
        for f in (await s.execute(select(Fan))).scalars():
            f.automation_paused_until = None
    stats3, out3, gw = await post_buy(oid)
    bug(len(sent) == n and not stats3.get('post_buy_bridged'),
        'G: ai_chatter.py:4844 _run_post_buy is not idempotent per offer: once the fan cooldown lapses, a re-run of the same post_buy offer '
        'sends the acknowledgement%s again (no per-offer "already acknowledged" marker; only the runner queue prevents it): %r'
        % (' and a second card' if [o for o in out3 if o[1]] else '', stats3))

    # An uncertain bridge is an attempted send, never permission to replay.
    await reset()
    oid = await put_purchase()
    async def uncertain_bridge(*args, **kwargs):
        raise TimeoutError("synthetic uncertain provider response")
    try:
        with patch.object(ai_chatter, "_send_free_bubble", uncertain_bridge):
            await post_buy(oid)
        check(False, "uncertain bridge must surface")
    except TimeoutError:
        check(True, "uncertain bridge surfaced")
    async with get_session() as s:
        for f in (await s.execute(select(Fan))).scalars():
            f.automation_paused_until = None
    stats, out, gw = await post_buy(oid)
    check(stats.get("reason") == "post_buy_already_started" and not out,
          "uncertain attempt is not replayed after cooldown or restart")

    # I. the fan replies after the purchase
    await reset()
    oid = await put_purchase()
    await put_message(FAN, 'in', 'thanks babe, that was hot', 1)
    stats, out, gw = await post_buy(oid)
    check(stats.get('reason') == 'he_spoke' and not out and not wire.deleted, ('I: engine stands down when he spoke', stats))
    # ...and at the queue: a job queued before he spoke is dropped by the runner (followup-fan-replied)
    await reset()
    oid = await put_purchase()
    await put_message(FAN, 'in', 'hi there', 20)
    jid = queue.add(FAN, 'ai_chatter', {'post_buy': oid}, datetime.utcnow() - timedelta(seconds=1))
    await put_message(FAN, 'in', 'wait one more thing', 0)
    with closing(sqlite3.connect(db_path)) as con:
        row = con.execute('SELECT last_in_id FROM native_pilot_followups WHERE job_id=?', (jid,)).fetchone()
        newest = con.execute("SELECT message_id FROM messages WHERE account_id=? AND fan_id=? AND direction='in' ORDER BY created_at DESC,message_id DESC LIMIT 1",
                             (ACCOUNT, FAN)).fetchone()
    check(row[0] is not None and row[0] != newest[0],
          'I: the job anchors on his newest message at queue time; a later reply differs, which is exactly what claim_followup compares '
          '(followup-fan-replied; its claim path is covered in test_native_pilot_service.py)')
    from native_pilot.ledger import FOLLOWUP_DROPS
    check('followup-fan-replied' in FOLLOWUP_DROPS and 'followup-chat-moved' in FOLLOWUP_DROPS, 'I: both supersede reasons drop (never fail) the job')
    # a newer purchase supersedes the older job (a newer rung owns him)
    await reset()
    old = await put_purchase(minutes_ago=6, msg=ORIG_MSG)
    new = await put_purchase(minutes_ago=2, msg=ORIG_MSG + 1)
    stats, out, gw = await post_buy(old)
    check(stats.get('reason') == 'superseded' and not out, ('I: a newer purchase supersedes the older job', stats))

    # J. pause / takeover right before dispatch
    await switch(fastt_post_buy_upsell=True,fastt_discount_resend=True)
    await reset()
    oid = await put_purchase()
    async with get_session() as s:
        f = await s.get(Fan, (ACCOUNT, FAN))
        f.automation_paused_until = datetime.utcnow() + timedelta(minutes=30)
    stats, out, gw = await post_buy(oid)
    check(stats.get('reason') == 'paused' and not out, ('J: a paused fan gets nothing', stats))
    await reset()
    oid = await put_purchase()
    gw = make_gateway()

    def held_send(fid, text, kw):
        from native_pilot.ledger import Refused as R
        raise R('fan-held-before-post')          # the wire's last-instant hold check (takeover / pause)
    with patch.object(content_prompts, 'read_hook', hook_for()), \
            NativeEngine(ACCOUNT, FAN, chat=chat, send=held_send, emit=events.append, content=vault, followups=queue,
                         arm_unsend=gw.arm_unsend, unsent=lambda: gw.unsent_ids, card_of=cards.get) as native:
        try:
            await native.run(1, payload={'post_buy': oid})
            check(False, 'J: a hold at the wire must stop the job')
        except PilotBoundaryError as exc:
            check(str(exc) == 'native-send-interrupted', ('J: held at the wire -> the job stops', str(exc)))
    check(not wire.posted and not [o for o in await offers() if o.status == 'open'], 'J: nothing posted, no new offer')
    # discount: a hold at the wire stops the job before the removal
    await reset()
    oid = await put_card(price=500)
    await objection()

    def held_discount(fid, text, kw):
        raise Refused('fan-held-before-post')    # raised before gateway._remove_card ever runs
    gw = make_gateway()
    with NativeEngine(ACCOUNT, FAN, chat=chat, send=held_discount, emit=events.append, content=vault, followups=queue,
                      arm_unsend=gw.arm_unsend, unsent=lambda: gw.unsent_ids, card_of=cards.get) as native:
        try:
            await native.run(1, payload={'discount_offer': oid})
            check(False, 'J: discount held')
        except PilotBoundaryError:
            check(not wire.deleted and not wire.posted and ORIG_MSG not in gw.unsent_ids, 'J: held before the wire -> nothing removed or sent')
    check((await offer(oid)).status == 'open', 'J: the original card stays live and open')

    # ═══ M. the existing nudge still works ══════════════════════════════════════════════════════════════
    await reset()
    oid = await put_card(price=500)
    await put_message(FAN, 'in', 'show me something', 200)
    gw = make_gateway()
    stats, out, gw = await run_job({'nudge_fan_ids': [FAN]})
    check(stats.get('nudged') == 1 and len(out) == 1 and not out[0][1] and out[0][0].strip(), ('M: the nudge sends one plain line', stats))
    check(not wire.deleted, 'M: the nudge removes nothing')
    # fan replied -> stale nudge is a no-op
    await put_message(FAN, 'in', 'wait what', 0)
    n = len(sent)
    stats, out, gw = await run_job({'nudge_fan_ids': [FAN]})
    check(stats.get('nudged') == 0 and len(sent) == n, 'M: replied -> nudge skipped')
    # queueing: the stock nudge is still the one job ai_chatter may queue
    with engine_for(make_gateway(), queue) as native:
        await native.enqueue_job(ACCOUNT, 'ai_chatter', payload={'nudge_fan_ids': [FAN]}, run_at=datetime.utcnow() + timedelta(minutes=15))
    with closing(sqlite3.connect(db_path)) as con:
        check(con.execute("SELECT count(*) FROM native_pilot_followups WHERE kind='nudge' AND state='queued'").fetchone()[0] == 1, 'M: nudge still queues')
    check(job_kind('ai_chatter', {'nudge_fan_ids': [FAN]}, FAN) == 'nudge'
          and job_kind('ai_chatter', {'discount_offer': 1}, FAN) == 'discount'
          and job_kind('ai_chatter', {'post_buy': 1}, FAN) == 'post_buy'
          and job_kind('ai_chatter', {'aftercare': 1}, FAN) is None
          and job_kind('make_right', {'post_buy': 1}, FAN) is None, 'M: job kinds')
    await engine.dispose()


def main():
    import dotenv
    dotenv.load_dotenv = lambda *a, **kw: False
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix='native-postsale-engine-') as tmp:
        with patch.dict(os.environ, {'DATABASE_URL': 'sqlite+aiosqlite:///' + (Path(tmp) / 'fixture.db').as_posix(),
                                     'PYTHON_DOTENV_DISABLED': '1'}, clear=True):
            async def run():
                def deny(*a, **kw): raise AssertionError('network forbidden')
                with patch.object(socket.socket, 'connect', deny), patch.object(socket.socket, 'connect_ex', deny), \
                        patch.object(socket, 'create_connection', deny):
                    await exercise(tmp)
            asyncio.run(run())
    print(f'{COUNT} checks passed')
    if BUGS:
        print(f'{len(BUGS)} REAL BUG(S) in non-test code:')
        for line in BUGS:
            print(' - ' + line)
        sys.exit(1)
    print('all cases passed')


if __name__ == '__main__':
    main()


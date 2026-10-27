"""Offer caps, repeats and stop-after-card on the REAL native catalogue path (synthetic data, blocked sockets).

Proves, against FASTT's own ai_chatter rules (no selling rule is changed here):
* two unpaid cards on the table: the next turn still gets an ordinary text reply, never a third card,
  and the declined offer leaves a visible ``offer-declined`` runner event with its reason;
* a third distinct card needs explicit ``fastt_max_open_offers`` / ``fastt_max_offers_per_day`` = 4;
* the daily cap counts expired/cancelled offers of the last 24 hours and rolls over;
* an item the fan bought, or whose card is still open, is never offered again.
"""
import asyncio, json, logging, os, socket, sys, tempfile
from pathlib import Path
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ACCOUNT, FAN = 'of-900001', 900002
COUNT = 0
ITEMS = {n: 1000 + n for n in (1, 2, 3, 4)}


def check(cond, label):
    global COUNT
    assert cond, label
    COUNT += 1


async def exercise(tmp):
    from db.engine import engine, get_session
    from db.models import (Base, Account, AccountAiConfig, Fan, Employee, Message, CatalogItem, ContentOffer)
    from sqlalchemy import delete, select
    from native_pilot.content import ContentPlan
    from native_pilot.engine import NativeEngine, NO_OFFER_NOTE
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    manifest = Path(tmp) / 'plan.json'
    manifest.write_text(json.dumps({'account_id': ACCOUNT, 'fan_id': FAN, 'items': [
        {'catalog_id': n, 'kind': 'video', 'price_cents': 300,
         'media': [{'id': m, 'type': 'photo'}, {'id': m + 100, 'type': 'video'}], 'preview_ids': [m]}
        for n, m in ITEMS.items()]}))
    plan = ContentPlan(manifest, ACCOUNT, FAN)

    def item(n, media, video=None):
        return CatalogItem(id=n, account_id=ACCOUNT, label=f'Video {n}', kind='video',
                           description_for_ai='One video. No other content details are verified.',
                           media_ids=json.dumps([media, video or media + 100]), preview_media_ids=json.dumps([media]),
                           duration_sec=60, price_cents=300, is_free_teaser=False, enabled=True, pricing_mode='fixed')
    async with get_session() as s:
        s.add(Account(id=ACCOUNT, nickname='Fixture'))
        s.add(AccountAiConfig(account_id=ACCOUNT, ai_chatter_config_json=json.dumps({"pack_send_enabled":True,"pack_on_ask_enabled":True,"qualification_gate_enabled":True})))
        s.add(Employee(id=1, display_name='Automation', is_active=True))
        s.add(Fan(account_id=ACCOUNT, fan_id=FAN))
        s.add_all([item(n, m) for n, m in ITEMS.items()])
    state = {'script': [], 'mid': 100, 'calls': [], 'unlinked': set()}
    sent, events = [], []

    async def chat(**kw):
        if kw.get('response_format'):
            return SimpleNamespace(content='{}', parsed={})
        state['calls'].append(kw)
        content = state['script'].pop(0) if state['script'] else 'haha tell me more'
        return SimpleNamespace(content=content, parsed=None)

    def send(fid, text, kw):
        term = plan.terms(kw)
        sent.append((text, kw))
        result = {'id': 50000 + len(sent), 'text': text, 'createdAt': datetime.utcnow().isoformat(), 'fromUser': {'id': 900001}}
        if term:
            result.update(media=[{'id': m, 'type': 'photo' if m < 1100 else 'video'} for m in kw['media_files']],
                          mediaCount=len(kw['media_files']), previews=kw['previews'],
                          price=term['price_cents'] / 100, lockedText=False)
            plan.evidence(result, term)
        return result

    def card(n, lead='ok one for you'):
        return f"{lead}\nThis one is $3 to unlock.\n>>OFFER {n}"

    async def turn(*script):
        """One fan message (everything before it is aged so it is the newest and old enough), one native run."""
        state['script'] = list(script)
        state['calls'].clear()
        state['mid'] += 1
        async with get_session() as s:
            f = await s.get(Fan, (ACCOUNT, FAN))
            f.automation_paused_until = None
            for m in (await s.execute(select(Message).where(Message.fan_id == FAN))).scalars():
                m.created_at = m.created_at - timedelta(seconds=90)
            for o in (await s.execute(select(ContentOffer).where(ContentOffer.status == 'open'))).scalars():
                o.offered_at = o.offered_at - timedelta(seconds=90)
            s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=state['mid'], direction='in', body='yes please',
                          created_at=datetime.utcnow() - timedelta(seconds=30)))
        before, ev = len(sent), len(events)
        with NativeEngine(ACCOUNT, FAN, chat=chat, send=send, emit=events.append, content=plan,
                          unlinked_paid=lambda: FAN in state['unlinked']) as native:
            stats = await native.run(state['mid'])
        assert not stats.get('errors'), stats
        return stats, sent[before:], [e for e in events[ev:] if e['event'] == 'offer-declined']

    async def offers():
        async with get_session() as s:
            return [(o.item_id, o.status) for o in (await s.execute(select(ContentOffer).order_by(ContentOffer.id))).scalars()]

    async def reset(config=None):
        async with get_session() as s:
            await s.execute(delete(ContentOffer))
            await s.execute(delete(AccountAiConfig))
            s.add(AccountAiConfig(account_id=ACCOUNT, ai_chatter_config_json=json.dumps({**{"pack_send_enabled":True,"pack_on_ask_enabled":True,"qualification_gate_enabled":True}, **(config or {})})))

    def cards(out):
        return [k for _, k in out if k]

    # 1. Two unpaid cards on the table: the third turn still chats, no third card, a visible reason.
    stats, out, declined = await turn(card(1))
    check(stats['replies_sent'] == 1 and len(cards(out)) == 1 and not declined, 'first card')
    stats, out, declined = await turn(card(2))
    check(len(cards(out)) == 1 and await offers() == [(1, 'open'), (2, 'open')], 'second card')
    stats, out, declined = await turn(card(3, lead='haha you are funny'))
    check(stats['replies_sent'] == 1 and [t for t, _ in out] == ['haha you are funny'] and not cards(out),
          'cap: ordinary text reply goes out, price bubble binned, no card')
    check(len(declined) == 1 and declined[0]['reason'] == 'max-open-offers' and declined[0]['item_id'] == 3
          and declined[0]['regenerated'] is False, 'cap: visible reason on the runner event')
    check(await offers() == [(1, 'open'), (2, 'open')], 'cap: no third offer row')
    # A reply that is ONLY price talk used to be dropped (silence): one plain regeneration instead.
    stats, out, declined = await turn(card(3, lead='$3 to unlock'), 'ok that is wild lol')
    check(stats['replies_sent'] == 1 and [t for t, _ in out] == ['ok that is wild lol'] and not cards(out),
          'cap: a price-only reply is regenerated as plain chat, not dropped')
    check(len(state['calls']) == 2 and state['calls'][1]['messages'][-1] == {'role': 'system', 'content': NO_OFFER_NOTE}
          and NO_OFFER_NOTE not in json.dumps(state['calls'][0]['messages']), 'cap: one retry, with the no-offer note')
    check(len(declined) == 1 and declined[0]['regenerated'] is True and len(await offers()) == 2, 'cap: retry is recorded')
    # A turn whose model does not offer anything is untouched and silent about it.
    stats, out, declined = await turn('just chatting here')
    check(stats['replies_sent'] == 1 and not declined and not cards(out), 'no offer asked: nothing recorded')

    # 2. Explicit limits only: fastt_max_open_offers / fastt_max_offers_per_day = 4 allow a third distinct item.
    await reset({'fastt_max_open_offers': 4, 'fastt_max_offers_per_day': 4})
    async with get_session() as s:
        s.add(item(5, 1005))
    manifest_raw = json.loads(manifest.read_text())
    manifest_raw['items'].append({'catalog_id': 5, 'kind': 'video', 'price_cents': 300,
                                  'media': [{'id': 1005, 'type': 'photo'}, {'id': 1105, 'type': 'video'}], 'preview_ids': [1005]})
    manifest.write_text(json.dumps(manifest_raw))
    plan = ContentPlan(manifest, ACCOUNT, FAN)
    for n in (1, 2, 3, 4):
        stats, out, declined = await turn(card(n))
        check(len(cards(out)) == 1 and not declined, f'limit 4: card {n}')
    check([s for _, s in await offers()] == ['open'] * 4, 'limit 4: four open cards')
    stats, out, declined = await turn(card(5, lead='ha yes'))
    check(not cards(out) and stats['replies_sent'] == 1 and declined and declined[0]['reason'] == 'max-open-offers',
          'limit 4: the fifth is held at the explicit limit, chat continues')

    # 3. Daily cap counts expired/cancelled offers of the last 24h and rolls over. Stock offer
    # spacing (min_fan_msgs_between_offers) is zeroed here so the daily cap is what is tested.
    await reset({'min_fan_msgs_between_offers': 0})
    async with get_session() as s:
        now = datetime.utcnow()
        for n in (1, 2):
            s.add(ContentOffer(account_id=ACCOUNT, fan_id=FAN, item_id=n, mode='ppv', price_cents=300, status='cancelled',
                               offered_at=now - timedelta(hours=1), updated_at=now))
    stats, out, declined = await turn(card(3, lead='lol stop'))
    check(not cards(out) and stats['replies_sent'] == 1 and declined and declined[0]['reason'] == 'daily-offer-cap',
          'daily cap: chat continues with a visible reason')
    async with get_session() as s:
        for o in (await s.execute(select(ContentOffer))).scalars():
            o.offered_at = o.offered_at - timedelta(hours=25)
    stats, out, declined = await turn(card(3))
    check(len(cards(out)) == 1 and not declined, 'daily rollover: a card is allowed again')
    check((3, 'open') in await offers(), 'daily rollover: the new offer is recorded')

    # 4. Repeats: never a card for a pending piece, identical pending media, or an item already bought.
    # Stock offer spacing is zeroed so the repeat rules are what is tested.
    await reset({'min_fan_msgs_between_offers': 0})
    await turn(card(1))
    unpaid_before = await offers()
    stats, out, declined = await turn(card(1, lead='hmm ok'))
    check(not cards(out) and [t for t, _ in out] == ['hmm ok'] and declined[0]['reason'] == 'item-already-pending',
          'repeat: no second card for a pending item, the marker is stripped, chat continues')
    async with get_session() as s:
        s.add(item(6, 1006, video=1101))      # another catalogue row sharing item 1's video (its hero media)
        manifest_raw = json.loads(manifest.read_text())
        manifest_raw['items'].append({'catalog_id': 6, 'kind': 'video', 'price_cents': 300,
                                      'media': [{'id': 1006, 'type': 'photo'}, {'id': 1101, 'type': 'video'}], 'preview_ids': [1006]})
    manifest.write_text(json.dumps(manifest_raw))
    plan = ContentPlan(manifest, ACCOUNT, FAN)
    stats, out, declined = await turn(card(6, lead='mm'))
    check(not cards(out) and declined[0]['reason'] == 'media-already-pending', 'repeat: media already pending under another catalogue row')
    check(await offers() == unpaid_before, 'repeat: the unpaid card is never closed or re-recorded')
    # He pays for item 1 (the existing ledger reconciliation marks the offer delivered): never again.
    async with get_session() as s:
        o = (await s.execute(select(ContentOffer).where(ContentOffer.item_id == 1))).scalar_one()
        o.status, o.resolved_by, o.resolved_at = 'delivered', 'ppv_ledger', datetime.utcnow()
    stats, out, declined = await turn(card(1, lead='again?'))
    check(not cards(out) and declined[0]['reason'] == 'item-already-purchased', 'repeat: no card for an item he bought')
    stats, out, declined = await turn(card(2))
    check(len(cards(out)) == 1 and not declined and stats['offers_made'] == 1, 'a different item is still offered')
    check(await offers() == [(1, 'delivered'), (2, 'open')], 'purchased row kept, only the new card is open')

    # 5. A fan with a paid purchase no message explains (excluded from the account hold or not) is never sold a card,
    # but is still answered; the same account's other fans are untouched. Per fan only.
    await reset({'min_fan_msgs_between_offers': 0})
    state['unlinked'] = {FAN}
    stats, out, declined = await turn(card(3, lead='haha you are funny'))
    check(stats['replies_sent'] == 1 and [t for t, _ in out] == ['haha you are funny'] and not cards(out),
          'unlinked payment: no card, the chat reply still goes out')
    check(len(declined) == 1 and declined[0]['reason'] == 'unlinked-payment' and declined[0]['item_id'] == 3,
          'unlinked payment: visible reason on the runner event')
    check(await offers() == [], 'unlinked payment: no offer row recorded')
    stats, out, declined = await turn(card(3, lead='$3 to unlock'), 'wow that is wild lol')
    check(stats['replies_sent'] == 1 and [t for t, _ in out] == ['wow that is wild lol'] and not cards(out)
          and declined[0]['regenerated'] is True, 'unlinked payment: a price-only reply is regenerated as plain chat')
    state['unlinked'] = {FAN + 1}          # somebody else on the account has one; this fan does not
    stats, out, declined = await turn(card(3))
    check(len(cards(out)) == 1 and not declined and await offers() == [(3, 'open')], 'another fan is still sold the card')
    state['unlinked'] = {FAN}
    await reset({'min_fan_msgs_between_offers': 0})
    # The failed-read case withholds rather than sells.
    boom = []
    def broken():
        boom.append(1)
        raise RuntimeError('db unavailable')
    state['unlinked'] = set()
    async with get_session() as s:
        f = await s.get(Fan, (ACCOUNT, FAN)); f.automation_paused_until = None
    probe = NativeEngine(ACCOUNT, FAN, chat=chat, send=send, emit=events.append, content=plan, unlinked_paid=broken)
    check(await probe._fan_unlinked_paid() is True and boom, 'unlinked payment: an unreadable ledger withholds the card')
    check(await NativeEngine(ACCOUNT, FAN, chat=chat, send=send, content=plan)._fan_unlinked_paid() is False,
          'no source wired: behaviour unchanged')
    await engine.dispose()


def main():
    import dotenv
    dotenv.load_dotenv = lambda *a, **kw: False
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix='native-offers-fixture-') as tmp:
        with patch.dict(os.environ, {'DATABASE_URL': 'sqlite+aiosqlite:///' + (Path(tmp) / 'fixture.db').as_posix(), 'PYTHON_DOTENV_DISABLED': '1'}, clear=True):
            async def run():
                def deny(*a, **kw): raise AssertionError('network forbidden')
                with patch.object(socket.socket, 'connect', deny), patch.object(socket.socket, 'connect_ex', deny), patch.object(socket, 'create_connection', deny):
                    await exercise(tmp)
            asyncio.run(run())
    print(f'{COUNT} checks; all cases passed')


if __name__ == '__main__':
    main()

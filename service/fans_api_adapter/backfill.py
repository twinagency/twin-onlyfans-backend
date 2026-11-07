"""Budgeted, resumable repair of gaps in webhook coverage.

Webhooks are not buffered while an endpoint is paused and are abandoned after
three failed attempts, so silence is never proof of "nothing happened". After a
gap this module replays what the provider still holds into the SAME evidence
projection the webhooks use, with an explicit start date and the provider's
own continuation values:

* transactions   ``startDate`` + opaque ``marker``  -> transaction evidence
* chargebacks    ``start_date`` + ``offset``        -> chargeback evidence
* chat history   ``next_page`` cursor per chat      -> purchased PPV (``isOpened`` with a price) as unlock evidence,
                                                       inbound fan messages as canonical ingress

Every provider page costs one unit of the caller's budget. A run that cannot
finish inside its budget stops, saves its cursor and reports
``budget_exhausted``; it never claims completeness it does not have.
"""
from datetime import datetime, timedelta
import json
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.onlyfans_fans_intake_models import OfFansBackfill, OfFansLink
from onlyfans_runtime.store import Refused, utcnow
from . import payment_evidence as ev

OVERLAP = timedelta(minutes=15)  # transactions.new alone may be 3 minutes late
MAX_PAGES = 200


def _fmt(value):
    return value.strftime('%Y-%m-%d %H:%M:%S')


class FansBackfill:
    def __init__(self, intake):
        self.intake, self.store, self.engine = intake, intake.store, intake.engine

    def gap_start(self, account, *, now=None, silence_seconds=300):
        """Start date for a repair, or None when the link has not been silent long enough to suspect a gap."""
        now = now or utcnow()
        with Session(self.engine) as s:
            seen = [r[0] for r in s.execute(select(OfFansLink.last_event_at).where(OfFansLink.account_id == account))
                    if r[0] is not None]
        if not seen or now - max(seen) < timedelta(seconds=silence_seconds):
            return None
        return max(seen) - OVERLAP

    def run(self, tenant, account, client, *, reason, start_date=None, pages_budget=5, page_size=100,
            history_chats=()):
        if (type(pages_budget) is not int or not 1 <= pages_budget <= MAX_PAGES or type(page_size) is not int
                or not 1 <= page_size <= 100 or reason not in {'pause_gap', 'missed_webhooks', 'operator'}):
            raise Refused('invalid backfill budget')
        now = utcnow()
        with Session(self.engine) as s, s.begin():
            self.store._runtime(s, tenant, account)
            run = s.scalar(select(OfFansBackfill).where(OfFansBackfill.account_id == account,
                OfFansBackfill.status.in_(('running', 'budget_exhausted'))).order_by(OfFansBackfill.started_at.desc())
                .limit(1).with_for_update())
            if run is None:
                start = start_date or self.gap_start(account, now=now, silence_seconds=0)
                if start is None:
                    raise Refused('an explicit start date is required when no webhook was ever seen')
                state = {'phase': 'tx', 'tx_marker': None, 'cb_offset': 0,
                         'chats': [str(ev.numeric(c)) for c in history_chats], 'hist_next': None}
                run = OfFansBackfill(account_id=account, run_id=str(uuid.uuid4()), reason=reason, start_date=start,
                    status='running', pages_used=0, pages_budget=pages_budget, marker=json.dumps(state),
                    rows_seen=0, rows_new=0, started_at=now, updated_at=now)
                s.add(run)
            run_id = run.run_id
        return self._advance(tenant, account, client, run_id, pages_budget, page_size)

    def _advance(self, tenant, account, client, run_id, budget, page_size):
        calls = 0
        while True:
            with Session(self.engine) as s, s.begin():
                run = s.get(OfFansBackfill, (account, run_id), with_for_update=True)
                state, start = json.loads(run.marker), run.start_date
                run.status, run.pages_budget = 'running', max(run.pages_budget, budget)
            if state['phase'] == 'done':
                return self._finish(account, run_id, 'complete', None, state)
            if calls >= budget:
                return self._finish(account, run_id, 'budget_exhausted', 'page budget reached', state)
            calls += 1
            try:
                seen, new = self._page(tenant, account, client, state, start, page_size)
            except (Refused, ValueError, TypeError, KeyError) as exc:
                return self._finish(account, run_id, 'failed', ('page_unusable:' + str(exc))[:96], state)
            with Session(self.engine) as s, s.begin():
                run = s.get(OfFansBackfill, (account, run_id), with_for_update=True)
                run.pages_used += 1
                run.rows_seen += seen
                run.rows_new += new
                run.marker, run.updated_at = json.dumps(state), utcnow()

    def _page(self, tenant, account, client, state, start, page_size):
        seen = new = 0
        phase = state['phase']
        if phase == 'tx':
            result = client.transactions(limit=page_size, marker=state['tx_marker'], start_date=_fmt(start))
            if result.state != 'observed':
                raise Refused('transactions unavailable')
            for row in result.data:
                outcome = self.intake.ingest_transaction_row(account, row)[1]
                seen, new = seen + 1, new + (1 if outcome.endswith('_new') else 0)
            if result.next_page is None:
                state['phase'] = 'cb'
            else:
                state['tx_marker'] = result.next_page
        elif phase == 'cb':
            result = client.chargebacks(limit=page_size, offset=state['cb_offset'], start_date=_fmt(start))
            body = result.data
            rows = body['data']['list'] if result.state == 'unverified_observation' and type(body) is dict else None
            if type(rows) is not list or len(rows) > page_size:
                raise Refused('chargeback page shape')
            for row in rows:
                outcome = self.intake.ingest_chargeback_row(account, row)[1]
                seen, new = seen + 1, new + (1 if outcome.endswith('_new') else 0)
            if len(rows) < page_size:
                state['phase'] = 'hist'
            else:
                state['cb_offset'] += page_size
        elif phase == 'hist':
            if not state['chats']:
                state['phase'] = 'done'
                return 0, 0
            chat = state['chats'][0]
            result = client.history(chat, limit=page_size, next_page=state['hist_next'])
            if result.state != 'observed':
                raise Refused('history unavailable')
            oldest_in_window = False
            for item in result.data:
                created = ev.stamp(item.get('createdAt'))
                if created < start:
                    oldest_in_window = True
                    continue
                seen += 1
                if self.intake.ingest_history_item(tenant, account, chat, item) in ('accepted', 'ok'):
                    new += 1
            if result.next_page is None or oldest_in_window:
                state['chats'].pop(0)
                state['hist_next'] = None
                if not state['chats']:
                    state['phase'] = 'done'
            else:
                state['hist_next'] = result.next_page
        return seen, new

    def _finish(self, account, run_id, status, detail, state):
        with Session(self.engine) as s, s.begin():
            run = s.get(OfFansBackfill, (account, run_id), with_for_update=True)
            run.status, run.detail, run.updated_at = status, detail, utcnow()
            run.marker = json.dumps(state)
            summary = {'run': run_id, 'status': status, 'pages': run.pages_used, 'rows': run.rows_seen,
                       'new': run.rows_new, 'detail': detail}
        # Whatever was replayed may complete a pairing that webhooks left half-open.
        self.intake.sweep(account)
        return summary

"""Read-only measurements for one account; no model/provider requests or DDL."""
import argparse
from datetime import datetime, timezone
import json
import os

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session
from db.models import Message
from db.onlyfans_models import OfIngress
from onlyfans_runtime.history_policy import policy
from onlyfans_runtime.launch_history_memory.recovery import HistoryRecovery
from onlyfans_runtime.store import Store, Refused


def report(store, tenant, account):
    progress = HistoryRecovery(store).progress(tenant, account)
    with Session(store.engine) as s:
        runtime = store._runtime(s, tenant, account, lock=False)
        p = policy(s, account, runtime.binding_id)
        if p is None:
            raise Refused('recent-history policy is not active for this account')
        scope = [Message.account_id==account, select(OfIngress.event_key).where(
            OfIngress.account_id==Message.account_id,OfIngress.binding_id==runtime.binding_id,
            OfIngress.fan_id==Message.fan_id,OfIngress.message_id==Message.message_id,
            OfIngress.status=='applied').exists()]
        total, recent, all_in, recent_in, all_chars, recent_chars = s.execute(select(
            func.count(),func.count().filter(Message.created_at>=p.cutoff_at),
            func.count().filter(Message.direction=='in'),
            func.count().filter((Message.direction=='in') & (Message.created_at>=p.cutoff_at)),
            func.coalesce(func.sum(case((Message.direction=='in',func.length(Message.body)),else_=0)),0),
            func.coalesce(func.sum(case(((Message.direction=='in') & (Message.created_at>=p.cutoff_at),func.length(Message.body)),else_=0)),0)
        ).where(*scope)).one()
    return {'schemaVersion':'of-onboarding-measurement.v1','measuredAt':datetime.now(timezone.utc).isoformat(),
        'accountId':account,'progress':progress,'comparison':{
            'basis':'current-binding stored archive only; unimported lifetime history is unknown',
            'storedMessages':total,'storedWindowMessages':recent,'storedIncomingMessages':all_in,
            'storedWindowIncomingMessages':recent_in,'storedIncomingCharacters':all_chars,
            'storedWindowIncomingCharacters':recent_chars,
            'recentIncomingCharacterFraction':recent_chars/all_chars if all_chars else None,
            'inference':'Character counts describe source volume, not billed tokens or a measured time/cost saving.',
            'fullHistoryProviderRequests':None,'fullHistoryModelCostUsd':None,
            'fullHistoryEndToEndSeconds':None,'measuredSpeedup':None}}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--account',required=True)
    args=parser.parse_args();store=Store(os.environ['DATABASE_URL'])
    try:print(json.dumps(report(store,os.environ['ONLYFANS_WORKER_TENANT'],args.account),indent=2))
    finally:store.close()

if __name__=='__main__':main()

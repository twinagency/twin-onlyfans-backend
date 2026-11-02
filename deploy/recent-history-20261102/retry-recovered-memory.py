"""Retry at most four stale recent-memory failures after observed model recovery.

Default is read-only. --apply changes next_at only, preserving receipts, errors,
attempt counts, budget limits and reply controls. Run in the existing worker.
"""
import argparse
import json
import os
from sqlalchemy import text
from sqlalchemy.orm import Session
from onlyfans_runtime.store import Store
from onlyfans_runtime.history_policy import policy


def retry(store, tenant, account, *, apply=False):
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account, lock=True)
        assert not runtime.enabled, 'Replies must remain disabled'
        window = policy(s, account, runtime.binding_id)
        assert window is not None
        params = dict(tenant=tenant, account=account, binding=runtime.binding_id,
                      cutoff=window.cutoff_at, started=window.started_at)
        # A successful current-pilot call proves the older 402 no longer blocks
        # this account. Never release unresolved call reservations here.
        recovered = s.scalar(text("""SELECT count(*) FROM grok_calls WHERE
            account_id=:account AND purpose='gen_info' AND status='done'
            AND error_text IS NULL AND called_at>=:started"""), params)
        assert recovered > 0, 'No current successful model call'
        jobs = s.execute(text("""SELECT p.fan_id FROM of_profile_learning p
            WHERE p.tenant_id=:tenant AND p.account_id=:account AND p.binding_id=:binding
            AND p.fan_id>0 AND p.error_code='profile-model-request-failed'
            AND p.lease_token IS NULL AND p.next_at>CURRENT_TIMESTAMP
            AND NOT EXISTS (SELECT 1 FROM grok_calls c WHERE c.account_id=:account
                AND c.fan_id=p.fan_id AND c.purpose='gen_info'
                AND c.called_at>=:started AND c.error_text IS NOT NULL)
            AND EXISTS (SELECT 1 FROM messages m WHERE m.account_id=p.account_id
                AND m.fan_id=p.fan_id AND m.direction='in' AND m.created_at>=:cutoff
                AND EXISTS (SELECT 1 FROM of_ingress i WHERE i.account_id=m.account_id
                    AND i.binding_id=p.binding_id AND i.fan_id=m.fan_id
                    AND i.message_id=m.message_id AND i.status='applied')
                AND NOT EXISTS (SELECT 1 FROM of_profile_receipts r
                    WHERE r.tenant_id=p.tenant_id AND r.account_id=m.account_id
                    AND r.binding_id=p.binding_id AND r.fan_id=m.fan_id
                    AND r.source_fan=m.fan_id AND r.message_id=m.message_id
                    AND r.body_md5=md5(m.body) AND r.source_at=m.created_at
                    AND r.direction=m.direction AND r.unsent=m.is_unsent
                    AND r.automation=coalesce(m.automation_kind,'')
                    AND r.next_chunk=r.total_chunks))
            ORDER BY p.fan_id FOR UPDATE OF p"""), params).scalars().all()
        assert len(jobs) <= 4, 'Review unexpected retry volume before proceeding'
        changed = 0
        if apply:
            for fan in jobs:
                changed += s.execute(text("""UPDATE of_profile_learning SET next_at=CURRENT_TIMESTAMP
                    WHERE tenant_id=:tenant AND account_id=:account AND binding_id=:binding
                    AND fan_id=:fan AND lease_token IS NULL"""), {**params, 'fan': fan}).rowcount
        return {'eligibleJobs':len(jobs), 'jobsRescheduled':changed,
                'successfulCurrentPilotCalls':recovered, 'aiRepliesEnabled':False}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    store=Store(os.environ['DATABASE_URL'])
    try:
        print(json.dumps(retry(store,os.environ['ONLYFANS_WORKER_TENANT'],
                               'of-323358988',apply=args.apply)))
    finally:
        store.close()

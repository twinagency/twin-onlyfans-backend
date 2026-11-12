"""Operator command: review ONE conflicting provider event key for a fan outside the trial.

DRY RUN by default; nothing is written unless --apply is given. Run it inside the reviewed image (like db_audit.py):
  python resolve_conflicting_webhook.py --tenant T --account A --event-key K --event-epoch 1 --expected-fan F \
     --scope-revision N --command-key <64 hex> --actor op-session:<64 hex> --evidence-ref 'operator:fansapi:<statement>' \
     [--variant-file PATH ...] [--apply]
Rules and limits are in conversation_engine/conflicting_webhook.py. It never sends, activates, or merges evidence.
"""
import argparse
import json
import os
import sys
from pathlib import Path


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for flag in ('--tenant', '--account', '--event-key', '--command-key', '--actor', '--evidence-ref'):
        p.add_argument(flag, required=True)
    for flag in ('--event-epoch', '--expected-fan', '--scope-revision'):
        p.add_argument(flag, required=True, type=int)
    p.add_argument('--variant-file', action='append', default=[], help='exact bytes of a variant whose bytes were not retained (repeatable)')
    p.add_argument('--database-url-file', default='/run/pilot-database-url')
    p.add_argument('--apply', action='store_true', help='record the review and release this one key (default: dry run)')
    return p


def open_store(args):
    os.environ['DATABASE_URL'] = Path(args.database_url_file).read_text().strip()
    from onlyfans_runtime.store import Store
    return Store(os.environ['DATABASE_URL'])


_DEFAULT_OPEN = open_store


def main(argv=None, *, open_store=open_store):
    args = parser().parse_args(argv)
    store = open_store(args)
    try:
        from conversation_engine import conflicting_webhook as cw
        from conversation_engine.operator_v2 import BadRequest
        from onlyfans_runtime.store import Refused
        try:
            bodies = [Path(f).read_bytes() for f in args.variant_file]
            result = cw.review(store, args.tenant, args.account, event_key=args.event_key, event_epoch=args.event_epoch,
                               expected_fan=args.expected_fan, scope_revision=args.scope_revision, command_key=args.command_key,
                               actor=args.actor, evidence_ref=args.evidence_ref, variant_bodies=bodies, apply=args.apply)
        except (Refused, BadRequest, OSError) as exc:
            print(json.dumps({'refused': True, 'reason': str(exc)[:300], 'mode': 'apply' if args.apply else 'dry_run'}, sort_keys=True))
            return 1
        print(json.dumps(result, sort_keys=True))
        return 0
    finally:
        close = getattr(store, 'close', None)
        if close and open_store is _DEFAULT_OPEN:       # never close a store the caller injected
            close()


if __name__ == '__main__':
    raise SystemExit(main())

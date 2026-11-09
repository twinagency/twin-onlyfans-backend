"""Explicit manual worker-owned billing read. Never run as part of startup, polling or a deployment."""
import argparse
import json
import os
from conversation_engine.model_config import ModelConfig
from db.schema import check_schema
from model_transport.openrouter import OpenRouterClient
from model_transport.reconciliation import reconcile_model_usage, pin_model_canonical
from onlyfans_runtime.store import Store, Refused


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('tenant','account','actor','reason'):
        parser.add_argument('--'+name,required=True)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--call-id')
    mode.add_argument('--pin-canonical',action='store_true')
    parser.add_argument('--command-key')
    parser.add_argument('--expected-pilot-revision',required=True,type=int)
    args=parser.parse_args(argv)
    if args.call_id and not args.command_key:
        raise Refused('billing reconciliation requires a command key')
    if os.environ.get('ONLYFANS_PROCESS_ROLE')!='worker' or not os.environ.get('DATABASE_URL'):
        raise Refused('explicit worker role and database are required')
    cfg=ModelConfig.from_env(os.environ)
    if not cfg.enabled or cfg.fallback_enabled or cfg.provider!='openrouter':
        raise Refused('configured single-provider worker is required')
    store=Store(os.environ['DATABASE_URL'])
    try:
        with store.engine.connect() as conn:check_schema(conn)
        client=OpenRouterClient(base_url=cfg.base_url,key_file=cfg.key_file,key_env=cfg.key_env,
            upstream_provider=cfg.upstream_provider,price_caps={model:cfg.prices_for(model) for model in cfg.models()})
        if args.pin_canonical:
            result=pin_model_canonical(store,cfg,client,tenant=args.tenant,account=args.account,
                actor=args.actor,expected_pilot_revision=args.expected_pilot_revision,reason=args.reason)
        else:
            result=reconcile_model_usage(store,cfg,client,tenant=args.tenant,account=args.account,
                call_id=args.call_id,actor=args.actor,command_key=args.command_key,
                expected_pilot_revision=args.expected_pilot_revision,reason=args.reason)
        print(json.dumps(result,sort_keys=True))
    finally:
        store.close()


if __name__=='__main__':main()

"""Manual worker-owned provider authentication verification; never invoked on startup."""
import argparse
import json
import os
from sqlalchemy import select
from sqlalchemy.orm import Session

from conversation_engine.pilot_scope import ProviderRequestGuard, authorize, require
from db.onlyfans_engine_models import OfFansConnection
from db.schema import check_schema
from fans_api_adapter.auth_recovery import recover_current_challenge, recover_reconnected_challenge
from fans_api_adapter.https_transport import FansHttpsTransport
from fans_api_adapter.protocol import FansApiClient
from fans_api_adapter.real_transport import load_real_config
from onlyfans_runtime.store import Refused, Store


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("tenant", "account", "actor", "command-key", "reason"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--expected-pilot-revision", required=True, type=int)
    parser.add_argument("--event-epoch", type=int)
    parser.add_argument("--event-key")
    parser.add_argument("--payload-sha256")
    args = parser.parse_args(argv)
    candidate = (args.event_epoch, args.event_key, args.payload_sha256)
    if any(x is not None for x in candidate) and not all(x is not None for x in candidate):
        raise Refused("complete reconnect evidence is required")
    if os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker" or not os.environ.get("DATABASE_URL"):
        raise Refused("explicit worker role and database are required")
    cfg = load_real_config(os.environ, require_flag=True)
    store = Store(os.environ["DATABASE_URL"])
    try:
        with store.engine.connect() as conn:
            check_schema(conn)
        with Session(store.engine) as s, s.begin():
            runtime = store._runtime(s, args.tenant, args.account)
            scope, pilot = require(s, runtime, active=True)
            authorize(pilot, args.actor, "reconcile")
            if scope.revision != args.expected_pilot_revision:
                raise Refused("pilot scope changed")
            if args.event_epoch is None:
                connection = s.scalar(select(OfFansConnection).where(
                    OfFansConnection.account_id == args.account,
                    OfFansConnection.binding_id == runtime.binding_id,
                    OfFansConnection.status == "active"))
                if connection is None or cfg.provider_account != connection.provider_account_id:
                    raise Refused("provider credential does not match current connection")
            else:
                connection = s.get(OfFansConnection, (args.account, args.event_epoch))
                if connection is None or connection.binding_id != runtime.binding_id:
                    raise Refused("retained connection is unavailable")
            creator, epoch = connection.provider_creator_id, connection.epoch
        guard = ProviderRequestGuard(store, args.tenant, args.account, args.expected_pilot_revision)
        client = FansApiClient(account=cfg.provider_account, creator_id=creator, epoch=epoch,
            token=cfg.token, http=FansHttpsTransport(enabled=True), timeout=cfg.timeout,
            enabled=True, base=cfg.base, request_guard=guard)
        common = dict(expected_revision=args.expected_pilot_revision, command_key=args.command_key,
                      actor=args.actor, reason=args.reason)
        if args.event_epoch is None:
            result = recover_current_challenge(store, args.tenant, args.account, client, **common)
        else:
            from model_transport.pilot_draft import _secret
            secret = _secret(os.environ.get("TWIN_PLAN_C_WEBHOOK_SECRET_FILE", ""))
            result = recover_reconnected_challenge(store, args.tenant, args.account, client,
                event_epoch=args.event_epoch, event_key=args.event_key,
                payload_sha256=args.payload_sha256, secret=secret, **common)
        print(json.dumps(result, sort_keys=True))
    finally:
        store.close()


if __name__ == "__main__":
    main()

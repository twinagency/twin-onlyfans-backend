"""Real Fans API delivery transport behind the one-use TextAuthority attempt.

Flow: TextAuthority.dispatch -> admit -> FansApiRealTransport.send -> consume_dispatch
(the one-use wire marker) -> FansApiClient.send -> FansHttpsTransport (httpx).
Nothing else in the engine can reach the network.

Outcome mapping (one logical message = one Idempotency-Key, derived from account + intent, reused on every
retry and every restart; at most 3 POSTs TOTAL including the first; only the provider's own outcome fields count,
never an HTTP status alone - https://docs.onlyfansapi.com/introduction/essentials/idempotency):
  200 with data.id                   accepted with the provider message id (a replay returns the same id)
  503 SERVICE_UNAVAILABLE not_sent   retry the SAME key; still not sent after the budget -> not accepted
  503 IDEMPOTENCY_UNAVAILABLE        this attempt was not executed: retry the SAME key (never after a 409: see _post)
  503 unknown / timeout / garbage    unknown: this fan is held, nobody else is touched
  409 IDEMPOTENCY_CONFLICT           retry the same key within the budget; still in progress -> unknown
  400/422 + request_outcome not_sent rejected -> not accepted (blockBannedWords=strict_ban is always sent)
  400/422 without that field         unknown (a 4xx alone is not proof of non-delivery)
  422 IDEMPOTENCY_KEY_MISMATCH       unknown (a human must fix the key mapping; never mint a new key)
A restart reconciles an attempt left 'dispatching' as UNKNOWN (recover_interrupted); it is never re-queued.

TextAuthority.dispatch accepts this exact type as a first-class transport. It requires the
account delivery policy to be transport_kind 'fansapi' (never 'synthetic' or 'native') and the
explicit config flag TWIN_ENGINE_FANSAPI_REAL=1 (default OFF) at dispatch time.

The transport is inert unless built through `build_real_transport`, which needs the
same flag and a 0600 credentials file that is not inside any git work tree. The key is
never logged or returned.
"""
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import time
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.onlyfans_engine_models import OfEngineDecision, OfFansConnection
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from onlyfans_runtime.store import Refused
from onlyfans_runtime.text_delivery import Accepted, Dispatch, NotAccepted, TextAuthority
from .protocol import ALLOWED_BASES, FansApiClient
from .https_transport import FansHttpsTransport

FLAG = 'TWIN_ENGINE_FANSAPI_REAL'
ENV_FILE = 'TWIN_ENGINE_FANSAPI_ENV_FILE'
_NAMESPACE = uuid.UUID('6d1f1c8e-2b0a-4f43-9a55-7f1f0c7e2a10')


def idempotency_key(account_id, intent_id):
    """One key per LOGICAL message (account + intent), never per attempt.

    The key is derived, so it is always recoverable from the stored intent after a crash, and it is the same
    on every retry, every restart and every same-action retry that follows a proven not-sent result. It is
    never rotated to escape an unknown outcome.
    """
    if type(account_id) is not str or not account_id or type(intent_id) is not str or not intent_id:
        raise Refused('account and intent id required for an idempotency key')
    return str(uuid.uuid5(_NAMESPACE, f'twin-engine-send:v2:{account_id}:{intent_id}'))


@dataclass(frozen=True)
class RealFansConfig:
    token: str = ''
    provider_account: str = ''
    base: str = ALLOWED_BASES[1]
    timeout: float = 20.0

    def __repr__(self):  # never expose the credential
        return f'RealFansConfig(provider_account={self.provider_account!r}, base={self.base!r})'


def load_real_config(env=None, *, require_flag=True):
    env = os.environ if env is None else env
    if require_flag and env.get(FLAG) != '1':
        raise Refused('real Fans API transport is not enabled')
    path = env.get(ENV_FILE)
    if not path:
        raise Refused('credentials file path not configured')
    p = Path(path).resolve()
    info = p.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise Refused('credentials file must be a regular 0600 file owned by this user')
    for parent in p.parents:
        if (parent / '.git').exists():
            raise Refused('credentials file must live outside any git work tree')
    values = {}
    for line in p.read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k, v = line.split('=', 1)
            values[k.strip()] = v.strip()
    base = values.get('FANSAPI_BASE', ALLOWED_BASES[1]).rstrip('/')
    if not values.get('FANSAPI_KEY') or not values.get('FANSAPI_ACCT') or base not in ALLOWED_BASES:
        raise Refused('credentials file is incomplete')
    return RealFansConfig(values['FANSAPI_KEY'], values['FANSAPI_ACCT'], base)


def real_transport_enabled(env=None):
    return (os.environ if env is None else env).get(FLAG) == '1'


def build_client(config, *, creator_id, epoch, transport=None):
    wire = FansHttpsTransport(enabled=True, transport=transport)
    client = FansApiClient(account=config.provider_account, creator_id=creator_id, epoch=epoch,
                           token=config.token, http=wire, timeout=config.timeout, enabled=True, base=config.base)
    if client.identity().state != 'verified':
        raise Refused('provider identity could not be verified')
    return client


def build_real_transport(store, tenant_id, account_id, *, creator_id, epoch, env=None, transport=None,
                         allowed_chats=None, **options):
    """Flag-gated factory: refuses unless TWIN_ENGINE_FANSAPI_REAL=1 and the credentials file is safe."""
    config = load_real_config(env, require_flag=True)
    client = build_client(config, creator_id=creator_id, epoch=epoch, transport=transport)
    return FansApiRealTransport(store, tenant_id, account_id, client, allowed_chats=allowed_chats,
                                enabled=True, **options)


class FansApiRealTransport:
    """Registered with TextAuthority.dispatch like the other scoped transports."""
    def __init__(self, store, tenant_id, account_id, client, *, allowed_chats=None, max_attempts=3,
                 retry_delay=1.0, sleep=time.sleep, enabled=False):
        if (enabled is not True or type(client) is not FansApiClient or not client._identity_verified
                or type(max_attempts) is not int or not 1 <= max_attempts <= 3
                or allowed_chats is not None and (type(allowed_chats) is not frozenset or not allowed_chats)):
            raise Refused('explicit real transport configuration required')
        self.store, self.tenant_id, self.account_id, self.client = store, tenant_id, account_id, client
        self.allowed_chats, self.max_attempts, self.retry_delay, self.sleep = allowed_chats, max_attempts, retry_delay, sleep
        self.sent_calls = 0  # POSTs issued (including same-key retries), for live-run accounting

    def _scope(self, dispatch):
        with Session(self.store.engine) as s, s.begin():
            runtime = self.store._runtime(s, self.tenant_id, self.account_id)
            decision = s.scalar(select(OfEngineDecision).where(OfEngineDecision.account_id == self.account_id,
                                                                 OfEngineDecision.intent_id == dispatch.intent_id))
            connection = s.get(OfFansConnection, (self.account_id, self.client.epoch))
            policy = s.get(OfDeliveryPolicy, self.account_id)
            if (decision is None or decision.connection_epoch != self.client.epoch or connection is None
                    or connection.binding_id != runtime.binding_id or connection.status != 'active'
                    or connection.provider_account_id != self.client.account
                    or connection.provider_creator_id != self.client.creator_id
                    or policy is None or policy.transport_kind != 'fansapi'):
                raise Refused('real transport binding changed')

    def send(self, dispatch, *, authority, tenant_id):
        if (type(authority) is not TextAuthority or type(dispatch) is not Dispatch or tenant_id != self.tenant_id
                or dispatch.account_id != self.account_id):
            raise Refused('invalid real transport context')
        if self.allowed_chats is not None and str(dispatch.fan_id) not in self.allowed_chats:
            raise Refused('chat is outside the allowed test scope')
        self._scope(dispatch)
        grant = authority.consume_dispatch(tenant_id, dispatch)
        if grant is None:
            raise Refused('dispatch is no longer eligible')
        key = idempotency_key(grant.account_id, grant.intent_id)
        proof = dict(account_id=grant.account_id, fan_id=grant.fan_id, intent_id=grant.intent_id,
                     attempt_id=grant.attempt_id, payload_hash=grant.payload_hash)
        result = self._post(grant.fan_id, key, grant.body, grant.media_ids, grant.preview_ids, grant.price_cents)
        stamp = datetime.now(timezone.utc).isoformat()
        if result.state == 'accepted':
            return Accepted(**proof, message_id=int(result.data['message_id']), occurred_at=stamp,
                            evidence_ref='fansapi:' + (result.trace_id or key))
        if result.state in ('not_sent', 'rejected'):
            return NotAccepted(**proof, occurred_at=stamp, evidence_ref=f'fansapi-{result.state.replace("_", "-")}:' + grant.attempt_id)
        return None  # unknown, conflict, key mismatch: stays unresolved and holds only this fan

    def _post(self, chat_id, key, body, media, previews, price_cents):
        """At most `max_attempts` (<= 3) POSTs TOTAL, first included, same key and payload every time.

        Only two provider outcomes are retried: an explicit not-sent 503 and a 409 key-in-progress. Everything
        else (accepted, unknown, key mismatch, malformed, rejected) ends the loop on the first answer. If a 409
        was seen, a later 'not sent' only describes that one attempt and says nothing about the earlier one (the
        key may still be running), so the final answer can never be 'not sent' after a conflict.
        """
        result = None
        reserved = False
        for attempt in range(self.max_attempts):
            self.sent_calls += 1
            result = self.client.send(chat_id, key=key, text=body, media=[str(v) for v in media],
                                      previews=[str(v) for v in previews], price_cents=price_cents,
                                      block_banned_words='strict_ban')
            if result.state not in ('not_sent', 'conflict'):
                return result
            reserved = reserved or result.state == 'conflict'
            if attempt + 1 < self.max_attempts:
                self.sleep(self.retry_delay * (attempt + 1))
        if reserved and result.state == 'not_sent':
            result = replace(result, state='unknown')
        return result

    def recover_interrupted(self):
        """Restart recovery: a started attempt with no process behind it becomes UNKNOWN (never queued or retried).

        An intent left 'dispatching' past its deadline may or may not have reached the provider. It is held as
        unknown for this fan only; reconcile_accepted replays the SAME key to find out. Returns how many.
        """
        return TextAuthority(self.store).expire_dispatches(self.tenant_id, self.account_id, limit=100,
                                                           synthetic_engine_only=True)

    def reconcile_accepted(self, intent_id):
        """Resolve an unknown attempt by replaying the SAME key and payload.

        The provider returns the stored result without sending again, so at most one
        message ever exists for the key. A 503/409/timeout leaves the attempt unknown.
        """
        authority = TextAuthority(self.store)
        with Session(self.store.engine) as s:
            row = s.get(OfTextIntent, (self.account_id, intent_id))
            if (row is None or row.status != 'unknown' or not row.attempt_id or not row.dispatch_token
                    or row.wire_started_at is None or row.payload_hash != authority._intent_hash(row)):
                return row.status if row is not None else 'unknown'
            if self.allowed_chats is not None and str(row.fan_id) not in self.allowed_chats:
                raise Refused('chat is outside the allowed test scope')
            args = (row.fan_id, idempotency_key(row.account_id, row.intent_id), row.body, tuple(json.loads(row.media_json)),
                    tuple(json.loads(row.preview_json)), row.price_cents)
            token, attempt, fan, payload_hash = row.dispatch_token, row.attempt_id, row.fan_id, row.payload_hash
        result = self._post(*args)
        if result.state != 'accepted':
            return 'unknown'
        proof = Accepted(account_id=self.account_id, fan_id=fan, intent_id=intent_id, attempt_id=attempt,
                         payload_hash=payload_hash, message_id=int(result.data['message_id']),
                         occurred_at=datetime.now(timezone.utc).isoformat(),
                         evidence_ref='fansapi-replay:' + (result.trace_id or attempt))
        outcome = authority.record_accepted(self.tenant_id, self.account_id, intent_id, token, proof)
        if outcome == 'accepted':
            authority.materialize(self.tenant_id, self.account_id, intent_id)
        return outcome

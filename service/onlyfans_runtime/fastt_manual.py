"""A human's dashboard message for a fan the FASTT runtime owns.

Ownership is a deployment allowlist, ``ONLYFANS_FASTT_SCOPE="<account>:<fan>[,...]"``,
an account-wide entry ``<account>:*``, or ``db:<account>`` (the operator selects the
fans per account in the settings), cut by the per-account selection; see
``fastt_scope``. Removing a fan from the selection stops FASTT serving it but never
hands it back to the legacy worker automatically. Ownership only decides WHO sends;
whether the AI replies stays Twin's per-fan state.
(account in Twin form ``of-176774528`` or FASTT form ``176774528``). It is read
only; nothing here enables a fan.

For an owned fan exactly one process may touch the provider: the FASTT runner.
So the dashboard path stays a normal ``TextAuthority`` *manual* intent (same
takeover, staff-ownership, revision, cap and throttle checks, the same
``of_text_intent`` / ``of_message_delivery`` evidence, human attribution at
``materialize``), with two differences, both confined to owned fans:

* ``_eligibility`` does not demand the account-wide history sync for a manual
  message. That gate protects an AI composing from a stale transcript; a human
  is looking at the live chat, and the live account is permanently
  ``recovery_needed`` while the FASTT runner, not the Twin worker, reads it.
* the provider POST is made by the runner's own single-attempt boundary,
  handed in as ``post_once``; Twin's worker connector is refused for these
  fans, so there is no second sender.

``post_once(dispatch)`` must be the runner's once-only path (pilot ledger
admission before the POST, one attempt, no retry) and return the provider's
created-message dict. Raise ``NotSent`` only if nothing was written to the
wire; any other exception is an ambiguous outcome and becomes an unresolved
attempt that is never replayed.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import fastt_scope
from .fastt_scope import SCOPE_ENV
from .runtime_stop import to_fastt_account_id
from .store import Refused


class NotSent(Exception):
    """Proof that nothing reached the provider (refused before the POST)."""


def _scope():
    """(fan pairs, whole accounts) the legacy worker must treat as FASTT's (the OWNERSHIP view).

    Env allowlist plus the per-account database selection, see ``fastt_scope``. With no database value this
    is exactly the old env-only answer. It only grows within a process; removing a fan never returns it.
    """
    return fastt_scope.owned_scope()


def fastt_account_owned(account_id) -> bool:
    """True when the whole account is owned (``<account>:*``, or database mode ``all``)."""
    account = to_fastt_account_id(account_id)
    return fastt_scope.platform_scope_enabled() or account in _scope()[1]


def fastt_owned(account_id, fan_id) -> bool:
    """The legacy worker must not send for this fan (never shrinks within a process)."""
    account = to_fastt_account_id(account_id)
    if fastt_scope.platform_scope_enabled():
        return True  # Fail closed: discovery failure never hands an account to a second sender.
    pairs, accounts = _scope()
    return account in accounts or (account, fan_id) in pairs


def fastt_serves(account_id, fan_id) -> bool:
    """The FASTT runner may act for this fan now: the current selection, which can shrink."""
    pairs, accounts = fastt_scope.serve_scope()
    account = to_fastt_account_id(account_id)
    return account in accounts or (account, fan_id) in pairs


class FasttManualTransport:
    """Registered transport: no I/O of its own, the runner's boundary does the POST."""

    def __init__(self, account_id, post_once, *, policy_revision):
        if not callable(post_once) or type(policy_revision) is not int or policy_revision < 0:
            raise Refused("invalid FASTT manual transport")
        self.account_id, self.post_once, self.policy_revision = account_id, post_once, policy_revision

    def send(self, dispatch, *, authority, tenant_id):
        from .text_delivery import Accepted, Dispatch, NotAccepted, TextAuthority
        if (type(authority) is not TextAuthority or type(dispatch) is not Dispatch
                or dispatch.account_id != self.account_id or dispatch.origin != "manual"
                or not fastt_owned(dispatch.account_id, dispatch.fan_id)):
            raise Refused("not a FASTT-owned manual send")
        # Final recheck of control, input, takeover ownership and allowance.
        grant = authority.consume_dispatch(tenant_id, dispatch, transport_generation=self.policy_revision)
        if grant is None:
            raise Refused("dispatch is no longer eligible")
        proof = dict(account_id=grant.account_id, fan_id=grant.fan_id, intent_id=grant.intent_id,
                     attempt_id=grant.attempt_id, payload_hash=grant.payload_hash)
        try:
            raw = self.post_once(grant)
        except NotSent:
            return NotAccepted(**proof, occurred_at=datetime.now(timezone.utc).isoformat(),
                               evidence_ref="fastt-not-sent:" + grant.attempt_id)
        mid = raw.get("id") if isinstance(raw, dict) else None
        if type(mid) is not int or isinstance(mid, bool) or mid <= 0:
            raise Refused("provider response carries no message id")   # ambiguous: held, never replayed
        return Accepted(**proof, message_id=mid,
                        occurred_at=str(raw.get("createdAt") or datetime.now(timezone.utc).isoformat()),
                        evidence_ref="fastt-manual:" + grant.attempt_id)


def next_queued_manual(store, tenant_id, account_id):
    """Oldest queued manual intent for an owned fan, or None."""
    from db.onlyfans_delivery_models import OfTextIntent
    with Session(store.engine) as s, s.begin():
        store._runtime(s, tenant_id, account_id)
        rows = s.execute(select(OfTextIntent.intent_id, OfTextIntent.fan_id).where(
            OfTextIntent.account_id == account_id, OfTextIntent.status == "queued",
            OfTextIntent.origin == "manual").order_by(OfTextIntent.created_at, OfTextIntent.intent_id).limit(50)).all()
    return next((identity for identity, fan in rows if fastt_serves(account_id, fan)), None)


def dispatch_next_manual(authority, tenant_id, account_id, transport):
    """Runner entry point: send one queued human message, if any. Returns the outcome or None."""
    identity = next_queued_manual(authority.store, tenant_id, account_id)
    return None if identity is None else authority.dispatch(tenant_id, account_id, identity, transport)


def post_operator_once(client, grant, *, owner_id):
    """Send the admitted manual payload once and require its matching receipt.

    Admission and uncertain-outcome holds remain owned by TextAuthority. This
    adapter reuses the current client; it adds no retry or alternate sender.
    """
    import json
    from twin_bridge.platform import escape_plain_text, validate_created_message
    from .native_protocol import API_PATH, acceptance, message, request
    from .text_delivery import Dispatch
    if type(grant) is not Dispatch or grant.origin != "manual":
        raise NotSent()
    if grant.media_ids:
        try:
            frozen = request(grant)
            payload = json.loads(frozen)
        except (ValueError, TypeError):
            raise NotSent() from None
        raw = client.post_json(f"{API_PATH}/chats/{grant.fan_id}/messages", json_body=payload,
            proxy_tries=1, allow_redirects=False)
        # Any malformed or contradictory response is UNKNOWN, never NotSent.
        acceptance(json.dumps(raw, ensure_ascii=False, allow_nan=False).encode(), grant,
            owner_id=owner_id, request_bytes=frozen)
    else:
        if grant.price_cents or grant.preview_ids:
            raise NotSent()
        body = escape_plain_text(grant.body)
        raw = client.send_text_once_body(grant.fan_id, body)
        event = message(raw, owner_id=owner_id, fan_id=grant.fan_id)
        if (event.direction != "out" or event.body != grant.body or event.media_ids
                or event.price_cents or raw.get("previews", []) != []
                or raw.get("lockedText") is not False or raw["text"] != body):
            raise Refused("provider response differs from the frozen manual text")
    validate_created_message(raw, chat_id=grant.fan_id, creator_id=owner_id)
    return raw

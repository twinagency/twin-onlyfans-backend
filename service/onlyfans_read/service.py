"""The three reads, their authorization, and nothing else.

Pure: takes a repository and a principal, returns an envelope or raises a
`refusal <errors>`. No FastAPI, no cookies, no globals — which is what lets the
offline suite drive every rule, including the ones that only happen when a
caller lies.

THE AUTHORIZATION IS TWO QUESTIONS, ASKED IN THIS ORDER:

  1. *What tenant is this credential?* Answered before this module runs, by
     `auth.verify_bearer`, and handed in as a `ServicePrincipal`.
  2. *Is this account (and this conversation) bound to that tenant?* Answered
     here, by the binding authority, for every single request. A creator id and
     an account id from the dashboard are a **request**, never a grant: they
     narrow a query that is already tenant-scoped, and a value that narrows to
     nothing is a refusal rather than an empty page.

That second rule is the whole reason this module is not a thin SQL wrapper.
Without it, `?account=<someone else's id>` would be answered with an empty list
— a 200 that says "that account has no conversations", which is a false
statement about a real account, and the single most dangerous shape an answer
to this API could take. `Refused` is one sentence for every cause, so a caller
cannot mine the difference between an unknown account and another tenant's.

READ-ONLY BY CONSTRUCTION. There is no command method here, none in the
repository, and no route that could carry one (api.py registers GET only, and
the offline suite asserts that).
"""

from __future__ import annotations

from datetime import datetime

from . import projections
from .contract import (
    DEFAULT_LIMIT,
    ID_PATTERN,
    MAX_LIMIT,
    clamp_limit,
    conversation_envelope,
    conversation_id_for,
    decode_cursor,
    encode_cursor,
    fan_id_from_conversation_id,
    iso,
    page_envelope,
    parse_iso,
    accounts_envelope,
)
from .errors import BadRequest, Refused
from .repo import DETAIL_MESSAGE_LIMIT, DETAIL_OWNED_LIMIT, OnlyFansReadRepo


def _require_id(value: str, *, what: str) -> str:
    candidate = (value or "").strip()
    if not ID_PATTERN.match(candidate):
        raise BadRequest(f"invalid_{what}", f"That {what} reference is not a valid identifier.")
    return candidate


async def _binding_for(repo: OnlyFansReadRepo, principal, account_id: str):
    """The one place an account is authorized. None means "refuse"."""
    return await repo.get_binding(tenant_id=principal.tenant_id, account_id=account_id)


async def list_accounts(
    repo: OnlyFansReadRepo,
    principal,
    *,
    creator_id: str | None = None,
    account_id: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Every account this credential may read, optionally narrowed.

    A narrowed request that matches nothing is a refusal. An unnarrowed request
    that matches nothing is a legitimate empty list — an installation with no
    bindings yet has, truthfully, no accounts — and the dashboard renders the
    two differently on purpose.
    """
    wanted_creator = _require_id(creator_id, what="creator") if creator_id is not None else None
    wanted_account = _require_id(account_id, what="account") if account_id is not None else None

    bindings = await repo.list_bindings(
        tenant_id=principal.tenant_id,
        creator_id=wanted_creator,
        account_id=wanted_account,
    )
    if wanted_account is not None and not bindings:
        raise Refused("unknown_account")
    if wanted_creator is not None and not bindings:
        raise Refused("unknown_creator")

    accounts = []
    for binding in bindings:
        anchor = await repo.account_activity_anchor(account_id=binding.account_id)
        account = projections.account_model(binding, anchor=anchor, now=now)
        health = getattr(repo, "account_health", None)
        if callable(health):account = {**account, **await health(account_id=binding.account_id)}
        accounts.append(account)

    return accounts_envelope(
        creator_id=wanted_creator,
        account_id=wanted_account,
        accounts=accounts,
    )


async def list_conversations(
    repo: OnlyFansReadRepo,
    principal,
    *,
    account_id: str,
    cursor: str | None = None,
    limit: int | None = None,
    now: datetime | None = None,
    ai_only: bool = False,
    search: str | None = None,
) -> dict:
    """One page of an account's conversations, newest activity first."""
    account = _require_id(account_id, what="account")
    binding = await _binding_for(repo, principal, account)
    if binding is None:
        raise Refused("unknown_account")

    from .search import normalize_search
    query = normalize_search(search)
    query_key = query + ("|ai" if ai_only else "")
    before: tuple[datetime | None, int] | None = None
    if cursor:
        try:
            raw_anchor, anchor_fan = decode_cursor(cursor, account_id=account, query_key=query_key)
            # `parse_iso`, not `datetime.fromisoformat`: a cursor's timestamp has
            # to be the same KIND of value the columns hold (naive UTC), or the
            # keyset comparison raises on an aware/naive mismatch.
            before = (parse_iso(raw_anchor) if raw_anchor else None, anchor_fan)
        except ValueError as exc:
            raise BadRequest("invalid_cursor", "That page cursor is not usable.") from exc

    size = clamp_limit(limit)
    # One extra row, so "is there another page" is answered by the data rather
    # than by a count that would be a second query.
    narrowing = {"ai_only": True} if ai_only else {}
    if query:narrowing["search"] = query
    rows = await repo.conversation_page(account_id=account, before=before, limit=size + 1, **narrowing)
    has_more = len(rows) > size
    page_rows = rows[:size]

    anchor = await repo.account_activity_anchor(account_id=account)
    items = []
    # Production repository batches the page; simple offline fixture repos can
    # retain their individual methods without changing the wire contract.
    metadata = getattr(repo, "conversation_metadata", None)
    batched = await metadata(account_id=account, rows=page_rows) if callable(metadata) else None
    for row in page_rows:
        if batched is not None:
            state = batched[0].get(row.fan_id)
            last_outcome = projections.delivery_outcome(batched[1].get(row.fan_id, "none"))
        else:
            state = await repo.conversation_state(account_id=account, fan_id=row.fan_id)
            deliveries = await repo.deliveries(account_id=account, fan_id=row.fan_id)
            last_outcome = "none"
            if row.last_message_id is not None:
                recorded = deliveries.get(int(row.last_message_id))
                if recorded is not None:
                    last_outcome = projections.delivery_outcome(recorded.outcome)
        summary = projections.conversation_summary(
            conversation=row,
            state=state,
            last_delivery=last_outcome,
            anchor=anchor,
            now=now,
        )
        items.append(summary)

    next_cursor = None
    if has_more and page_rows:
        tail = page_rows[-1]
        next_cursor = encode_cursor(
            account_id=account,
            last_activity_at=iso(tail.last_message_at),
            fan_id=tail.fan_id,
            query_key=query_key,
        )

    return page_envelope(account_id=account, items=items, next_cursor=next_cursor)


async def get_conversation(
    repo: OnlyFansReadRepo,
    principal,
    *,
    account_id: str,
    conversation_id: str,
    now: datetime | None = None,
    cursor: str | None = None,
) -> dict:
    """One conversation and its messages.

    The conversation id carries its account, and the account is re-authorized
    here. An id minted for another account is refused by
    `fan_id_from_conversation_id` before any query runs, so there is no path on
    which a thread is read under the wrong account's authorization.
    """
    account = _require_id(account_id, what="account")
    wanted = _require_id(conversation_id, what="conversation")
    binding = await _binding_for(repo, principal, account)
    if binding is None:
        raise Refused("unknown_account")

    fan_id = fan_id_from_conversation_id(wanted, account)
    if fan_id is None:
        # Either it belongs to another account or it is not an id at all; the
        # caller is told the same thing either way.
        raise Refused("unknown_conversation")

    row = await repo.conversation(account_id=account, fan_id=fan_id)
    if row is None:
        raise Refused("unknown_conversation")

    from .contract import decode_thread_cursor, encode_thread_cursor
    paging = {}
    if cursor is not None:
        try:
            paging["before_message_id"] = decode_thread_cursor(cursor, account_id=account, fan_id=fan_id)
        except ValueError as exc:
            raise BadRequest("invalid_thread_cursor", "That history cursor is not usable.") from exc
    messages = await repo.messages(
        account_id=account, fan_id=fan_id, limit=DETAIL_MESSAGE_LIMIT, **paging
    )
    deliveries = await repo.deliveries(account_id=account, fan_id=fan_id, **paging)
    state = await repo.conversation_state(account_id=account, fan_id=fan_id)
    facts = await repo.fan_facts(account_id=account, fan_id=fan_id)
    owned = await repo.owned_media(
        account_id=account, fan_id=fan_id, limit=DETAIL_OWNED_LIMIT
    )
    anchor = await repo.account_activity_anchor(account_id=account)

    detail = projections.detail_model(
        conversation=row,
        state=state,
        messages=messages,
        deliveries=deliveries,
        facts=facts,
        owned=owned,
        anchor=anchor,
        now=now,
    )
    spend = getattr(repo, "fan_spend", None)
    if callable(spend):detail["spend"] = await spend(account_id=account, fan_id=fan_id)
    detail["previousCursor"] = (encode_thread_cursor(account_id=account, fan_id=fan_id,
        before_message_id=messages[0].message_id) if len(messages) == DETAIL_MESSAGE_LIMIT else None)
    return conversation_envelope(account_id=account, conversation=detail)


# Re-exported so the router and the suite read one list of the reads this
# service performs, rather than each keeping its own copy.
READ_OPERATIONS = ("list_accounts", "list_conversations", "get_conversation")

#: Defaults surfaced for assertions: the bounded page size and the id shape.
DEFAULT_PAGE_SIZE = DEFAULT_LIMIT
MAX_PAGE_SIZE = MAX_LIMIT

__all__ = [
    "list_accounts",
    "list_conversations",
    "get_conversation",
    "READ_OPERATIONS",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "conversation_id_for",
]

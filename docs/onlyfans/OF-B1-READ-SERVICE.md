# OF-B1 — the OnlyFans read service and account-binding foundation

Historical OF-B1 record. The offline 18 October continuation supersedes the
contract version, schema lifecycle, shim and projection defaults described here:
see [FOUNDATION-2026-10-18.md](FOUNDATION-2026-10-18.md) for current behavior and
the remaining release gates. The original review history below is retained.

Status: **implemented and repaired in an isolated worktree, verified offline,
not deployed, not migrated, not connected to a provider.** This is the first real
read-only OnlyFans backend service behind the Twin dashboard's port.

Branch `codex/onlyfans-of-b1`, worktree `inflowwkiller-of-b1`, cut from
`e72316fd9c34092f767bc6e425ca9faea340da27` — the base `CONSOLIDATION_PLAN.md`
names for Fastt. Nothing is committed or pushed. §11 records the repair pass that
followed the first review.

---

## 1. What this is, in one paragraph

The dashboard already had a frontend read adapter for three operations
(`lib/onlyfans/backend.ts` → `{accounts}`, `{page:{items,nextCursor}}`,
`{conversation}`). This package is the *service* behind them: three GET
endpoints, a binding authority, a read repository boundary, and a schema version
negotiated in both directions. It is read-only by construction — no command
method, no provider client, no write route anywhere in the package — and it fails
closed: an unknown account, another tenant's account, an absent credential, an
undecided tenant, an unimplemented schema version and unreadable storage are six
different refusals, and none of them is ever an empty success.

It is **not** backend consolidation. No delivery authority, no intake, no
provider connection, no conversation-control writes and no commerce exist here.

## 2. Ownership and contracts this package confirmed and reused

Read and reused, not re-derived. Per the plan's milestone-1 requirement, this
document **only records the slice OF-B1 needed** — it is not a second audit.

| Milestone-1 artifact | Where it already lives | What OF-B1 took from it |
|---|---|---|
| Ownership map (one authority per fact) | `CONSOLIDATION_PLAN.md` §2 | The dashboard reads projections and submits commands; it is never a second writable ledger. Hence a read-only port and no dashboard-side store |
| Contracts for messages/history/commerce | `CONSOLIDATION_PLAN.md` §4 | The shapes the dashboard already models — revision, control mode, unresolved send, per-bubble receipt, freshness — are the projection fields this service emits |
| Audit findings and their owning gates | `COMPARATIVE_AUDIT.md` (at `output/independent-audit-20261016/`; the path in the reading list, `twin-sexbot/docs/onlyfans/COMPARATIVE_AUDIT.md`, does not exist) | F13 (populated startup reports success without migrating) shaped §6 below; F1 (tenant boundaries) shaped the binding authority; F11 (stale/partial sends) shaped the delivery projection |
| Dashboard-side read contract | `twin-dashboard-of-d0/docs/onlyfans/API_CONTRACTS.md` families 1 and 2 | Implemented exactly; families 3-7 remain proposals and no route exists for them |
| Source register / migration decisions | plan §1-§3 (Fastt as the Python foundation, PostgreSQL as the store) | The service is Fastt-side, reads Fastt's own tables, and adds migrations rather than a second store |

Nothing in the audit was re-run, and no new register was written: the only
artifacts this package adds are this file and the PostgreSQL rehearsal in §6.

## 3. The API

Namespace `/onlyfans/api/v1`, GET only:

| Route | Answers | Refuses |
|---|---|---|
| `/status` | `{ok, schemaVersion, supportedSchemaVersions, scope, serviceConfigured}` — reads no account data | 401 / 400 / 503 as below |
| `/accounts?creator=&account=` | `{schemaVersion, scope:{creatorId,accountId}, accounts:[…]}` | 404 for an unknown or another tenant's `account`/`creator` |
| `/accounts/{accountId}/conversations?cursor=&limit=` | `{schemaVersion, accountId, page:{items, nextCursor}}` | 404 for an unknown or unauthorized account; 400 for an unusable cursor |
| `/accounts/{accountId}/conversations/{conversationId}` | `{schemaVersion, accountId, conversation:{…}}` | 404 for an unknown account, an unknown conversation, or a conversation id minted for another account |

Statuses and what they mean to the dashboard: **401** no or incorrect
credential · **400** malformed request (missing/unsupported version, bad cursor,
bad id) · **403/404** refused · **503** unavailable (no token configured, no
tenant decided, storage unreachable) · **500** the read failed. Every answer is
`Cache-Control: private, no-store`, and every refusal is
`{ok:false, state, code, error}`, so the dashboard's existing classifier works
unchanged.

### Three decisions worth reviewing

1. **The namespace is `/onlyfans/api/v1`, deliberately not `/api/of/*`.** That
   prefix is scanned by the relay's account-isolation middleware, which answers
   a cookieless request with 401 — and a service client has no cookies by
   design. This namespace carries its own, stricter gate and declares itself to
   the share-token perimeter through one exported prefix check
   (`is_service_path`) rather than a copied string. The share token is a
   *browser* perimeter (an optional, GET-queryable shared secret); requiring it
   here would not add a check a service client can pass, it would only make the
   dashboard fail on any deployment that sets one.
2. **Conversation id = `"<account_id>:<fan_id>"`, derived, never stored.** A
   conversation *is* (account, fan) — that is the `chats` primary key — and the
   colon is already inside the id class the dashboard's scope parser accepts. A
   derived id means a conversation has one before any control row exists, and
   the id a URL carries cannot disagree with a row written later. An id minted
   for another account does not resolve, so a thread can never be read under the
   wrong account's authorization.
3. **The binding table is the authorization, and a request is never a grant.**
   `creator` and `account` narrow a query that is already tenant-scoped. A
   narrowing that matches nothing is a refusal (404), because a 200 with an
   empty list would be a false statement about a real account. An unnarrowed
   request from a tenant with no bindings is a legitimate empty list.

## 4. Schema version and the tenant decision, end to end

Both halves of the version contract are enforced, which was the original gap:
the dashboard's parser used to shape-check an answer and ignore its version.

* Request: the dashboard sends `X-OnlyFans-Schema-Version: of-preview.v1`; the
  service **refuses** a request that omits it (400 `missing_schema_version`) or
  names one it does not implement (400 `unsupported_schema_version`).
* Answer: the service stamps `schemaVersion` on every envelope; the dashboard
  **refuses** an answer whose version is absent or different, with
  `backend_schema_mismatch` — and that is a 500-shaped *error*, not
  `unavailable`, because the backend is present and answering; it simply speaks
  a contract this dashboard does not. Reporting that as "there is nothing to
  read from" would hide a needed code change behind the wrong sentence.

**The tenant decision is now explicit, and blank is not a scope.** A credential
is `ONLYFANS_API_TOKEN` plus `ONLYFANS_API_TENANT`, and the tenant has exactly
three treatments (`auth.resolve_tenant`, one pure function used by both the gate
and the tests):

| `ONLYFANS_API_TENANT` | Meaning |
|---|---|
| a `users.id` | the credential is narrowed to that agency's creators and accounts |
| the literal `deployment` | the installation's own operator credential: every binding |
| blank or unset | **refused** — 503 `tenant_not_configured`, on every route |

Blank used to mean "no narrowing", which made *read every binding in this
installation* the state you reach by forgetting a variable. Widening is now
something an operator writes down. The check runs after the token is proven, so
a misconfigured deployment does not describe its own configuration to whoever
sent a bad token: a wrong token stays a 401.

## 5. Files

Added in `inflowwkiller-of-b1`:

| File | What it is |
|---|---|
| `service/onlyfans_read/contract.py` | Schema version, envelopes, id/cursor rules, vocabulary guards, naive-UTC time helpers |
| `service/onlyfans_read/errors.py` | The refusal kinds (401/400/404/503) and their bodies |
| `service/onlyfans_read/auth.py` | Bearer verification, the tenant rule, version negotiation. Reads no cookie and no query parameter |
| `service/onlyfans_read/repo.py` | The read repository boundary — the only SQL in the package, and the only place storage failures become `unavailable` |
| `service/onlyfans_read/projections.py` | Relay rows → the dashboard's read models, with each over-claim decision stated |
| `service/onlyfans_read/service.py` | The three reads and their authorization, pure over an injected repository |
| `service/onlyfans_read/api.py` | The FastAPI router (GET only) |
| `service/db/migrations/versions/20261018_0100_onlyfans_read_bindings.py` | The three tables below. **Created, not applied** |
| `service/tests/test_onlyfans_read.py` | The offline suite (10 sections, 237 checks) |
| `service/tests/test_onlyfans_migration_pg.py` | The PostgreSQL rehearsal (44 checks), skip-with-notice without a database |
| this file | The record |

Changed additively:

| File | Change |
|---|---|
| `service/db/models.py` | `OfCreatorBinding`, `OfConversationState`, `OfMessageDelivery`; the partial unique index; the boolean `false()` default |
| `service/db/migrations/alembic.ini` | `version_locations` removed — it broke discovery (§6) |
| `service/server.py` | Two import lines, one `include_router`, one gate exemption built from the package's constant |
| `service/llm_models.py` | **Prerequisite repair** — the frozen dataclass default (§11.5) |
| `service/.env.example` | `ONLYFANS_API_TOKEN`, `ONLYFANS_API_TENANT`, with why blank is refused |

## 6. Database migrations — created, not applied, and rehearsed on PostgreSQL

Three tables, `down_revision = "0064_user_llm_keys"`, revision id
`0070_onlyfans_read_bindings`:

* **`of_creator_bindings`** — tenant → creator → account. **At most one binding
  per account may be ACTIVE**, enforced by a PARTIAL unique index on
  `account_id` where `revoked_at IS NULL`. A plain `UNIQUE(account_id)` counted
  revoked rows, so an account that was unlinked could never be linked again — to
  anybody — which is the opposite of what revoking is for. The composite
  `(tenant_id, creator_id, account_id)` constraint is gone for the same reason:
  it forbade re-binding an account to a creator it had previously belonged to,
  and it added nothing, because the partial index already permits at most one
  live row per account. The tenant stays a plain string rather than an FK, so
  deleting a login row cannot cascade a creator's identity away, and rows are
  revoked rather than deleted so "was this ever ours" stays answerable.
* **`of_conversation_state`** — control as a read projection: mode, monotonic
  `revision` (0 is the honest answer for an untouched conversation), takeover
  state, and the unresolved-send hold. `requires_attention` is a BOOLEAN whose
  server default is PostgreSQL's own `false`, not the integer `0` that SQLite
  tolerated.
* **`of_message_delivery`** — per-message receipts, with `unknown` as a
  first-class outcome.

**Not applied anywhere.** This repository runs migrations as an explicit release
operation, and `init_db()` does not call `command.upgrade` (asserted by parsing
the module, not by reading a comment). It seeds nothing: a deployment with no
bindings serves an empty account list truthfully, and an account that is not
bound is refused.

### Alembic could not see the revision, and now can

`service/db/migrations/alembic.ini` declared
`version_locations = %(here)s/versions`. Alembic expands `%(here)s` to an
absolute path, and with `version_path_separator` unset it falls back to its
LEGACY split-on-space-or-comma — so on this checkout, whose path contains
`TWIN CLONE CREATION`, that one path became three locations, none of which
existed, and the CLI reported **zero revisions**. The previous revision of this
package worked around that by parsing the migration files directly. The key is
now removed, which restores Alembic's default (`<script_location>/versions`, one
path, never split); the suite asserts the option is absent from the live keys
and then asserts the graph through `ScriptDirectory.from_config` — the same call
the CLI makes. The graph is 66 revisions with exactly one head, `0070`.

### The rehearsal

`service/tests/test_onlyfans_migration_pg.py` runs the migration against a
**disposable PostgreSQL** (started by the bundled `pgserver`, or pointed at a
server by `ONLYFANS_TEST_PG_URL`) and proves, in one order:

1. Alembic discovers the revision through its real configuration, with `0070` as
   the single head and `0064` as its parent.
2. The parent revision is built the way a deployment builds it. The relay's boot
   path is `create_all()` and `alembic upgrade` runs afterwards for deltas only
   (CLAUDE.md records that rule, and the historical chain depends on it: several
   pre-0064 migrations call `op.create_table` on tables `create_all` has already
   made). The parent schema is therefore built from the models **minus this
   package's three tables** — the state of a deployment running the previous
   release — and alembic is stamped at `0064_user_llm_keys`.
3. A clean `upgrade heads` from there. This is a release, and it is the only
   time this migration's own DDL runs, so the tables on the server are its work
   rather than `create_all`'s.
4. `downgrade 0064_user_llm_keys` — the migration is reversible, and the relay's
   other tables are untouched.
5. A second `upgrade heads`, which proves a re-run after a rollback.
6. Keys, foreign keys and indexes as PostgreSQL recorded them: a primary key per
   table; `of_creator_bindings.account_id` → `accounts`,
   `of_conversation_state (account_id, fan_id)` → `fans` and
   `of_message_delivery (account_id, fan_id, message_id)` → `messages`, all ON
   DELETE CASCADE; all five indexes present;
   `uq_of_creator_bindings_active_account` UNIQUE and PARTIAL
   (`WHERE (revoked_at IS NULL)`); no `UNIQUE(account_id)` constraint remaining;
   `requires_attention` defaulting to `false` while `revision` keeps its integer
   `0`.
7. The behaviour: a second ACTIVE binding for the same account refused by name
   from the same scope, from another creator and from another tenant; rebinding
   the SAME tenant and creator after a revocation accepted; a different creator
   able to take the account over afterwards; every revoked row still present
   with its original owner; an already-revoked row accepted; and a revoked-only
   account with no live binding.

Two honest caveats. The rehearsal builds its parent schema with a **shim** that
corrects integer boolean defaults on PRE-EXISTING tables in memory (§9.10) —
those tables cannot be created on PostgreSQL at all without it, and repairing
the shared models is outside this package's scope. And it is deliberately NOT
the historical chain from empty, for the reason in step 2; that chain's
replayability is a separate, pre-existing question (§9.11).

## 7. Prototype behaviour versus implemented capability

| Capability | State |
|---|---|
| Read accounts + connection health | **Implemented** (`connected` / `expired_session`; other states arrive with the connection lifecycle) |
| Read conversation list, cursor-paged | **Implemented** |
| Read one conversation and its messages | **Implemented** (last 100 messages, bounded) |
| Account/creator binding authority | **Implemented** (table + enforcement per request) |
| Service-to-service auth | **Implemented** (bearer, constant-time, fail-closed) |
| Tenant decision | **Implemented** (explicit; blank refused) |
| Schema version negotiation | **Implemented** (both directions) |
| Conversation control (mode, revision, takeover) | **Read only.** Nothing writes these rows yet, so every conversation reads automated/none/0 — which is true |
| Per-message delivery receipts | **Read only.** Absent rows project per direction: inbound → none, an unreconciled placeholder → dispatching, confirmed history → sent |
| Sending, takeover, pause, reconnect, resolve-evidence | **Not implemented.** No route, no method; capabilities report `not_configured` |
| Content, catalogue, sales, settings | **Not implemented** (families 3-7 have no route) |
| Provider connection / session capture | **Not implemented and not needed** for reads: this service reads the relay's own records |
| Applying the migration | **Not done anywhere.** Created and rehearsed only |

## 8. Verification

All of it offline: no network, no provider, no live database, no send. The
backend suites write only to temp directories they create — and, for the
rehearsal, a scratch database on a throwaway PostgreSQL that is dropped.

| # | Command | Result |
|---|---|---|
| 1 | `python service/tests/test_onlyfans_read.py` | exit 0, **237 checks, 0 failed** |
| 2 | `python service/tests/test_onlyfans_migration_pg.py` | exit 0, **44 checks, 0 failed** (PostgreSQL 16 via `pgserver`); **SKIPPED, exit 0** without a database |
| 3 | `python -m py_compile` on all changed Python | exit 0 |
| 4 | `node_modules/.bin/tsc --noEmit` (dashboard) | exit 0 |
| 5 | `npm run test:onlyfans-read` (dashboard) | exit 0, **111 checks, 0 failed** |
| 6 | `npm run test:onlyfans-unconfigured` | exit 0, **39 checks, 0 failed** |
| 7 | `npm run test:onlyfans-preview` | exit 0, **101/101** |
| 8 | `npm run test:channel-integration` | exit 0, **97 checks, 0 failed** |
| 9 | `node preview/onlyfans/scripts/check-isolation.mjs` (browser) | exit 0, **70/70** |
| 10 | `check-sidebar-layout` / `check-lynq-management` / `check-creator-workspace` / `check-creator-routing` | exit 0 each |

Environment notes, honest rather than convenient. The backend suites cannot run
**inside the sandbox**: creating an asyncio event loop on Windows needs a
loopback socketpair, and starting PostgreSQL needs a port, so both were run with
escalated permission — they are nevertheless entirely offline. The disposable
PostgreSQL and the `psycopg2` driver are **not** project dependencies: the
wheels were unpacked into a temp directory and put on `PYTHONPATH` for the
rehearsal only, and nothing was installed into the worktree's virtualenv.

## 9. Unresolved dependencies and conflicts

1. **The uncommitted bridge chain.** The working checkout carries an untracked
   `twin_bridge`/`native_pilot`/`telegram_contract` body of work plus migrations
   `0065`-`0067`, all descending from the same `0064_user_llm_keys`. This branch
   was cut from the committed base, so it neither includes nor depends on that
   work — but **when the two land together this migration must be re-parented
   onto the bridge head** rather than merged as two branches from one parent.
   The `0070` id was chosen to leave that range free.
2. **Nothing binds an account yet.** The binding table is empty by design.
   Creating bindings (and deciding whether they derive from `user_accounts`) is
   the cutover decision; until then a correct deployment serves an empty account
   list, and every specific account id is refused.
3. **Control and delivery rows are written by nobody.** The projections are
   ready for milestone 3; until it exists, `revision` is 0, mode is `automated`,
   and a delivery outcome is derived from message history rather than from a
   receipt.
4. **Retracted messages have no vocabulary.** The relay can mark a message
   `is_unsent` after acceptance. The dashboard's outcome union has no
   "retracted" member, so such a message projects as `sent` — the bubble was
   accepted, and the retraction lives in the relay's own UI. Adding the state is
   a contract change, recorded rather than smuggled in.
5. **Freshness is an initial policy**, not a measured one: an account projection
   older than 30 minutes renders as "last known", anchored on the newest
   observed activity, or on the binding's own timestamp when there is none. Its
   boundary is asserted; the value is for review.
6. **`/status` reports configuration, not reachability.** It deliberately does
   not touch storage, because a health check that read the database would be a
   data read wearing a health check's name. A storage probe is still missing,
   and the first real read is what discovers an unmigrated database (as a 503).
7. **No capacity or concurrency work.** The page size is bounded (25 default,
   100 maximum) and the detail read is bounded (100 messages, 50 purchased
   items), but nothing here was load-tested; that is milestone 7.
8. **WHO an account belongs to is decided here; WHICH account a browser may
   request is decided by the dashboard.** The service refuses an account that is
   not bound to the calling credential's tenant, and that is all it can do. A
   binding row does not by itself grant a browser access; the dashboard's own
   `user_accounts` scoping is a separate surface.
9. **One migration decision is still open: what happens to
   `of_creator_bindings` when an account row is deleted.** The FK is
   `ON DELETE CASCADE`, matching `user_accounts`, so the binding history goes
   with the account. If the history is meant to outlive the account, that is a
   cutover decision and a different FK.
10. **PRE-EXISTING, BLOCKING for any PostgreSQL deployment: parts of the shared
    schema cannot be created on PostgreSQL.** Fifteen columns across eleven
    migrations — and the same seven columns in the ORM: `users.is_admin`,
    `fans.is_muted`, `transaction_scan_history.fully_backfilled`,
    `mass_broadcast_cache.{is_free,is_tip,is_canceled,can_unsend}` — declare an
    INTEGER server default on a BOOLEAN column. SQLite accepts it; PostgreSQL
    rejects it outright, so `0001_initial`'s `create_all()` fails before it
    creates a single table. This is not caused by OF-B1 and is not repaired
    here, because the shared models are outside this package's declared scope.
    The three tables this package adds were written with `sa.false()` from the
    start, and the rehearsal corrects the pre-existing defaults in memory only,
    printing every column it touched. The fix is mechanical when someone owns
    it: `text("0")` → `false()`, `text("1")` → `true()`.
11. **PRE-EXISTING: the historical chain is not replayable from empty.** At
    least `20260519_0001_saved_replies` (and, by the same pattern, others) call
    `op.create_table` on tables `0001_initial`'s `create_all()` has already
    created, so `alembic upgrade head` on an empty database fails with "relation
    already exists". This is consistent with the repository's documented
    operating rule — the schema heals on boot and alembic runs for deltas only —
    which is why the rehearsal follows the deployment path rather than the
    chain. Reconciling the chain is milestone 2's "canonical populated
    migrations" work.

## 10. What this package did not do

No provider or account connection, no session material, no private conversation
fetched, no send, no activation, no deployment, no live migration, no commit, no
push, no paid model call. No Telegram route, identity module, agency policy,
engine, media path or model configuration was touched. The `inflowwkiller`
checkout, `twin-sexbot` and `twin-dashboard-of-d0` were read, not modified — the
dashboard's own shared components were left exactly as they were.

---

## 11. The repair pass (five repairs, and the prerequisite repairs)

### 11.1 Account rebinding, with history retained

`UNIQUE(account_id)` and the composite `UNIQUE(tenant_id, creator_id,
account_id)` are replaced by one PARTIAL UNIQUE index,
`uq_of_creator_bindings_active_account`, on `account_id` where
`revoked_at IS NULL`. The predicate is declared for BOTH dialects
(`sqlite_where` and `postgresql_where`), because a dialect that silently ignored
it would recreate the constraint being removed. Proven in two places: the
offline suite drives the real repository against a real SQLite database (two
simultaneous active bindings refused under the same scope, under a different
creator and from another tenant; revoke then rebind to the SAME scope accepted;
a different creator able to take over; reads resolving ONLY the active binding;
every revoked row still present with its owner; a revoked-only account read as
unbound), and the PostgreSQL rehearsal proves the same against the real index,
reporting the constraint PostgreSQL actually refused by name.

### 11.2 The tenant decision fails closed

`ONLYFANS_API_TOKEN` with no `ONLYFANS_API_TENANT` answers 503
`tenant_not_configured` on every route, including `/status`. The deployment-wide
scope requires the literal `deployment`; anything else non-blank is a tenant id.
`auth.resolve_tenant` is the one pure definition, used by the gate and driven
directly by the tests. `.env.example` and §4 are updated. Proven at three levels:
the rule itself, `verify_bearer`, and the constructed application answering 503
for both a missing token and a blank tenant — with a live binding present in
storage, so the gate is demonstrably the only thing withholding it.

### 11.3 The migration is PostgreSQL-correct, and Alembic finds it

Boolean server defaults in this package became `false()` (rendering `false` on
PostgreSQL, `0` on SQLite); `revision` keeps its integer `0`. The
`version_locations` key that made Alembic report zero revisions is gone, and
`ScriptDirectory.from_config` is now asserted to see all 66 revisions with
`0070` as the single head. The migration is guarded per table
(`if … not in existing`), matching the house convention, so it is correct both
on a database that predates it and on one whose schema `create_all` built. The
rehearsal in §6 covers the clean upgrade, all three tables with their keys,
foreign keys and indexes, the partial index's behaviour, the downgrade, and a
second clean upgrade.

### 11.4 The constructed application, read-only, failing closed

`import server` now succeeds, so these assertions are about the real FastAPI app
rather than about the source that builds it: the OnlyFans router is mounted with
exactly the four declared routes, every one is a GET, no POST/PUT/PATCH/DELETE
exists in the namespace or anywhere else under `/onlyfans`, and missing
credentials fail closed — both with no token and with a token and no tenant.

### 11.5 Prerequisite repair: the boolean default that blocked the chain

The rehearsal could not get past `0001_initial`'s `create_all()` because
`users.is_admin` declares an integer default on a BOOLEAN column. That defect is
pre-existing, it covers fifteen migration columns and seven ORM columns, and
repairing the shared models is outside this package's scope — so it is
documented as a blocking dependency (§9.10) and corrected in memory by the
rehearsal, which prints every column it touched. The three tables this package
adds were written with `sa.false()` from the start.

### 11.6 Prerequisite repair: `llm_models.py` did not import

`service/llm_models.py` declared `reasoning_body: Mapping[str, Any] =
_NO_REASONING`, a `MappingProxyType`. Since Python 3.11 `@dataclass` rejects a
bare default whose type has no `__hash__`, so it raised at IMPORT time —
`mutable default <class 'mappingproxy'> for field reasoning_body is not allowed:
use default_factory` — and took the whole relay down before a single request was
served. The file is byte-identical to the commit this branch was cut from, so it
is pre-existing, and it is why "the relay constructs with the router mounted"
could not previously be demonstrated at all. The smallest correct change is what
the error message asks for: `field(default_factory=…)` returning the SAME shared
frozen mapping, so the immutable contract the constant exists for is unchanged.
Focused regression coverage: `import server` in the offline suite, which now
runs and fails loudly if the module stops importing, plus the route and refusal
assertions built on the constructed app.

### 11.7 Prerequisite repair: Alembic discovery

Described in §6. Worth recording separately because no file-parsing test could
see it, and because it was identical in the untouched checkout: Alembic's own
configuration could not load the revision graph at all, so `alembic upgrade`
would have had nothing to run.

### 11.8 What the repair pass deliberately left alone

The pre-existing integer boolean defaults across the shared schema (§9.10) and
the historical chain's non-replayability (§9.11). Both are documented with the
exact columns and files, both are outside this package's declared scope, and
both are now visible to a future reader instead of hidden behind a workaround.

# Dashboard route inventory

Source baseline: a39e34a524b74bdf3bb2e5571453e56401796864; isolated test commit: 95a2cf3ad87bb0d89f4fc256847efa9b08eedd33.
Retain means preserve the existing surface; adapt means a versioned compatibility decision is needed. Inventory is not browser or end-to-end qualification.

| Route | Disposition | Evidence and outstanding work |
|---|---|---|
| `app/api/admin/v1/onlyfans/upload-permission/route.ts` | retain / unverified | Read authorization and wrong-account synthetic cases pass; complete route and browser coverage remains open. |
| `app/api/onlyfans-campaigns/accounts/[accountId]/campaigns/[campaignId]/route.ts` | retain / unverified | Legacy ownership and scheduled egress require explicit fencing and end-to-end verification. |
| `app/api/onlyfans-campaigns/accounts/[accountId]/campaigns/route.ts` | retain / unverified | Legacy ownership and scheduled egress require explicit fencing and end-to-end verification. |
| `app/api/onlyfans-configuration/accounts/[accountId]/moxy/route.ts` | retain / adapt | Configuration acknowledgement version/account/revision fixture passes; new-engine settings need an explicit compatible projection or new version. |
| `app/api/onlyfans-configuration/accounts/[accountId]/route.ts` | retain / adapt | Configuration acknowledgement version/account/revision fixture passes; new-engine settings need an explicit compatible projection or new version. |
| `app/api/onlyfans-connections/accounts/[accountId]/route.ts` | retain / adapt | Connection identity, reconnect epoch and account binding are unqualified. |
| `app/api/onlyfans-connections/accounts/route.ts` | retain / adapt | Connection identity, reconnect epoch and account binding are unqualified. |
| `app/api/onlyfans-connections/creators/[creatorId]/sign-in/route.ts` | retain / adapt | Connection identity, reconnect epoch and account binding are unqualified. |
| `app/api/onlyfans-connections/creators/route.ts` | retain / adapt | Connection identity, reconnect epoch and account binding are unqualified. |
| `app/api/onlyfans-connections/proxies/route.ts` | retain / adapt | Connection identity, reconnect epoch and account binding are unqualified. |
| `app/api/onlyfans-content/accounts/[accountId]/imports/[[...path]]/route.ts` | retain / unverified | Vault, media and content semantics require combined vault integration and browser tests. |
| `app/api/onlyfans-content/accounts/[accountId]/items/[itemId]/route.ts` | retain / unverified | Vault, media and content semantics require combined vault integration and browser tests. |
| `app/api/onlyfans-content/accounts/[accountId]/items/route.ts` | retain / unverified | Vault, media and content semantics require combined vault integration and browser tests. |
| `app/api/onlyfans-content/accounts/[accountId]/message-media/[fanId]/[messageId]/[mediaId]/[variant]/route.ts` | retain / unverified | Vault, media and content semantics require combined vault integration and browser tests. |
| `app/api/onlyfans-content/accounts/[accountId]/shared-media/route.ts` | retain / unverified | Vault, media and content semantics require combined vault integration and browser tests. |
| `app/api/onlyfans-content/accounts/[accountId]/sync/[[...path]]/route.ts` | retain / unverified | Vault, media and content semantics require combined vault integration and browser tests. |
| `app/api/onlyfans-content/accounts/[accountId]/vault/manage/[...resource]/route.ts` | retain / unverified | Vault, media and content semantics require combined vault integration and browser tests. |
| `app/api/onlyfans-content/accounts/[accountId]/vault/previews/[mediaId]/route.ts` | retain / unverified | Vault, media and content semantics require combined vault integration and browser tests. |
| `app/api/onlyfans-content/accounts/[accountId]/vault/route.ts` | retain / unverified | Vault, media and content semantics require combined vault integration and browser tests. |
| `app/api/onlyfans-content/accounts/[accountId]/vault/sync/route.ts` | retain / unverified | Vault, media and content semantics require combined vault integration and browser tests. |
| `app/api/onlyfans-content/accounts/[accountId]/vault/uploads/[[...path]]/route.ts` | retain / unverified | Vault, media and content semantics require combined vault integration and browser tests. |
| `app/api/onlyfans-content/accounts/[accountId]/vault/videos/[mediaId]/route.ts` | retain / unverified | Vault, media and content semantics require combined vault integration and browser tests. |
| `app/api/onlyfans-content/accounts/[accountId]/vault/videos/[mediaId]/stream/route.ts` | retain / unverified | Vault, media and content semantics require combined vault integration and browser tests. |
| `app/api/onlyfans-controls/accounts/[accountId]/conversations/[conversationId]/route.ts` | retain / adapt | Control acknowledgement, stale revision, scope and unconfirmed transport fixtures pass; full route authorization and provider ID compatibility remain unverified. |
| `app/api/onlyfans-launch/abc/accounts/[accountId]/[...path]/route.ts` | retain / unverified | Legacy ownership and scheduled egress require explicit fencing and end-to-end verification. |
| `app/api/onlyfans-launch/automation/accounts/[accountId]/emergency-stop/route.ts` | retain / unverified | Legacy ownership and scheduled egress require explicit fencing and end-to-end verification. |
| `app/api/onlyfans-launch/automation/accounts/[accountId]/follow-ups/[actionId]/route.ts` | retain / unverified | Legacy ownership and scheduled egress require explicit fencing and end-to-end verification. |
| `app/api/onlyfans-launch/automation/accounts/[accountId]/follow-ups/route.ts` | retain / unverified | Legacy ownership and scheduled egress require explicit fencing and end-to-end verification. |
| `app/api/onlyfans-launch/automation/accounts/[accountId]/settings/route.ts` | retain / unverified | Legacy ownership and scheduled egress require explicit fencing and end-to-end verification. |
| `app/api/onlyfans-launch/memory/accounts/[accountId]/backfill/route.ts` | retain / unverified | Shared sidebar/reply memory projection and correction propagation are not integrated. |
| `app/api/onlyfans-launch/memory/accounts/[accountId]/fans/[fanId]/backfill/route.ts` | retain / unverified | Shared sidebar/reply memory projection and correction propagation are not integrated. |
| `app/api/onlyfans-launch/memory/accounts/[accountId]/fans/[fanId]/route.ts` | retain / unverified | Shared sidebar/reply memory projection and correction propagation are not integrated. |
| `app/api/onlyfans-launch/memory/accounts/[accountId]/fans/route.ts` | retain / unverified | Shared sidebar/reply memory projection and correction propagation are not integrated. |
| `app/api/onlyfans/accounts/[accountId]/conversations/[conversationId]/handover/route.ts` | retain / unverified | Read authorization and wrong-account synthetic cases pass; complete route and browser coverage remains open. |
| `app/api/onlyfans/accounts/[accountId]/conversations/[conversationId]/route.ts` | retain / unverified | Read authorization and wrong-account synthetic cases pass; complete route and browser coverage remains open. |
| `app/api/onlyfans/accounts/[accountId]/conversations/route.ts` | retain / unverified | Read authorization and wrong-account synthetic cases pass; complete route and browser coverage remains open. |
| `app/api/onlyfans/accounts/[accountId]/operations/route.ts` | retain / unverified | Read authorization and wrong-account synthetic cases pass; complete route and browser coverage remains open. |
| `app/api/onlyfans/accounts/[accountId]/workspace/[section]/route.ts` | retain / unverified | Read authorization and wrong-account synthetic cases pass; complete route and browser coverage remains open. |
| `app/api/onlyfans/accounts/route.ts` | retain / unverified | Read authorization and wrong-account synthetic cases pass; complete route and browser coverage remains open. |
| `app/api/onlyfans/earnings/route.ts` | retain / unverified | Read authorization and wrong-account synthetic cases pass; complete route and browser coverage remains open. |

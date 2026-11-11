# Disabled creator preparation milestone

The commercial sales plan remains open. This milestone adds creator identity verification and resumable latest-1,000 combined photo/video metadata preparation while replies and sales are held.

It uses OfEnginePilotScope (account preparation rows above legacy row 1), OfLaunchCommand, OfFansConnection/OfFansLink, the existing vault importer, and shared job/observation/metadata records. Migration 0132 preserves legacy pilot semantics and monotonic reservations. A preparation row cannot become active through the database guard. Common delivery admission and delivery-policy enabling refuse preparation accounts even after preparation stops.

The existing worker has an exclusive --preparation-only mode. It does identity and at most one metadata page per account per pass, with an advisory lock across I/O and short reservation transactions. There is no model, download, planner or sender path in that mode. Pages are fenced before read and before apply. A failed or stopped read preserves the cursor; completed jobs avoid polling until an explicit new sync is queued. Terminal semantic failures require an explicit new sync command, while transient failures resume the same cursor. Old ownership, corrections, jobs and sends are retained. Native workers skip explicitly selected FansAPI accounts.

The existing configuration purpose and agency session/BFF own preparation status, configure and stop. The dashboard shows pending history/ownership coverage separately and retains exact unconfirmed requests for retry. The form starts with no budget defaults.

## Real preparation remains default off

Installation requires an approved private registry outside git, owned by the worker user with mode 0600. The registry is a JSON array of at most ten unique accounts/creators. Every entry contains tenant, account, provider_account, creator_id, env_file and limits (provider_requests, provider_credit_budget, provider_credits_per_request). Each env_file uses the existing private FansAPI credential loader and its exact provider account. The API cannot supply secret paths or override deployment ceilings.

Set TWIN_FANSAPI_PREPARATION_REAL=1 and TWIN_FANSAPI_PREPARATION_REGISTRY to that approved file only after qualification and explicit read budget approval. The worker also requires ONLYFANS_PROCESS_ROLE=worker, explicit ONLYFANS_WORKER_TENANT and the isolated qualified DATABASE_URL. Run python service/onlyfans_worker.py --preparation-only --once for one bounded pass. This does not use the sender flag and does not require a native connection key.

The dashboard account must already have its existing tenant/creator binding. New disabled accounts with no connector policy can explicitly select FansAPI through preparation; existing native policies are refused rather than silently switched. Existing account provisioning remains with connections.provision_account.

## Limits of this milestone

Metadata selection does not establish lifetime history, ownership completeness, factual descriptions, creator rights or sales approval. AI categorisation/descriptions, complete account-scoped commercial terms/discounts/tips/staff interleaving, normal unseeded sale qualification, real provider/model qualification, final production dashboard image and operational stop/backup/rollback rehearsal remain work under the full plan. No install, deployment, real provider/model charge, sender activation or customer contact is implied by this source milestone.

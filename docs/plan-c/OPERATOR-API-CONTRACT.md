# Current Plan C operator contract

The backend owner is conversation_engine/operator_v2.py and the existing approval, control, pilot-scope and delivery authorities. The unchanged dashboard is commit 3cedc8f5c6d3c9f493d6ef35f4162a5207b5b589. Routine replies/cards use the approved autonomous policy; no new per-message human approval rule is introduced. Historical supervised approve/reject/edit endpoints remain for their existing mode. The dashboard shows autonomous drafts and the existing controls with their actual mode.

Backend routes remain under /onlyfans/control/v2/accounts/{account}/, protected by bearer authentication, X-OnlyFans-Engine-Operator-Version: of-engine-operator.v2, exact JSON fields, <=8KB commands, named actor permissions, revision checks and idempotent commandKey. The Next server-side proxy holds its operator credential; it is not exposed to the browser. Owner/session SQL remains unchanged.

- GET drafts exposes current payload/version/context and authorization mode. Existing edit/approve/reject commands retain mode, validation and stale-version guards.
- POST conversations/{id}/stop, takeover and handback retain expectedRevision. Handback restores eligibility only; it does not replay old authority.
- Existing pilot configure/status operations remain stopped/disabled, explicit and audited. Coverage snapshot reading creates no authority; configure records exact approval before/after for the versioned product fingerprint and explicit renewal.
- GET unresolved retains unknown sends, financial/access discrepancies and review-only model calls, requests and provider-event items. A delivered card is not an unresolved proposal and financial settlement is not access proof.
- POST unresolved/{id}/reconcile keeps the original commandKey/actor/evidenceRef contract for existing supported kinds. provider_event requires the separate exact fields documented in OFFLINE-OPERATING-PROCEDURES.md. That command can classify only authenticated retained bytes proving a documented irrelevant counter. It cannot clear malformed/material/conflicting events or any other hold.

Provider-event and model-call rows are already review-only in the unchanged dashboard. No generic note/clear button was added. The exact-event extension is backend administrative functionality. Model cost settlement is not an API operation: service.tools.plan_c_reconcile_model runs manually under the existing worker credential owner, performs authenticated call-correlated provider metadata verification and preserves unknown outcomes. The API role receives no model credential.

See OFFLINE-OPERATING-PROCEDURES.md for exact fields, existing offboarding order, limitations and live-stage gates. Offline tests/scripts are not authorization to invoke these controls against real accounts.

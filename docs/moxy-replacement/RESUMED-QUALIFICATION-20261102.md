# Moxy resumed qualification, 2 November 2026

Status: implementation candidate; not complete or release-qualified.

Observed on the dedicated Work Server:
- Full current Moxy fixture suite: 108 passed in 293.55 seconds, eight modules.
  Includes source rules, integrated workflows, campaigns, constraints, resume,
  unknown delivery and actual populated historical schema upgrades.
- Earlier focused run: 34 passed in 150.19 seconds. Overlaps the full run;
  do not add counts.
- Real Next loopback HTTP fixture passed: roles, CSRF, request bounds, tenant
  scope, revision conflict, malformed acknowledgement and access-version fence.
- Dashboard TypeScript and contract checks passed, including conservative
  unconfirmed save handling for 503 responses and no automatic retries.
- Browser fixture passed at 1280/390/320 widths: readiness, mode changes,
  lost acknowledgement refresh, read-only and unavailable states.
  This used the real component and fixture services, not the live dashboard.

Changes: keep no-send and refused-sale input constraints durable; retain
source-quoted buying questions; close questions only from accepted replies;
preserve paid obligations until fulfilment; current readiness; canonical
scheduler ownership; preview isolation; dashboard mode accessible name.
Browser libraries were extracted under the task workspace, not system-installed.

Remaining requirements:
- Broader provider fault/restart and multi-creator load qualification.
- End-to-end import/change asset descriptions and complete service workflows.
- Integrate the newer onboarding changes without losing Moxy routing fences.
- Approved actual assets, prices, complete creator sequences and exact purchase
  links; current operating authority and external automation coordination.
- More held-out contextual examples and bounded actual-model evaluation.
- Exact live dashboard source remains unavailable in the existing record.

No actual-model evaluation was run; no customer or live-provider throughput,
sales quality or revenue improvement is established by these fixture tests.
No merge, deployment, activation, production setting change, import restart,
purchase or customer message was performed.

Logs are /srv/projects/moxy-final-suite.log, moxy-http-resumed.log,
moxy-dashboard-final.log and moxy-browser-resumed.log. Do not replace production
from this dashboard archive or treat this passing fixture checkpoint as launch
approval. Continue the original START_HERE requirements.

## Onboarding reconciliation

Selected connection response negotiation and tenant-scoped reporting fixes from
c8216d33ef409be5bc99aed0f654b1ccfe3023b3 were carried into this isolated branch.
No deployment scripts or live pilot settings were copied. Moxy coordination
fences remain unchanged. Integrated history/connection/Moxy checks: 70 passed
in 253.34 seconds. This overlaps earlier results; do not add counts.

## Bounded synthetic load

Two creator configurations were tested separately, one account at a time.
Each scenario prepared and queued 100 independent-fan reply decisions, then
dispatched with four threads under a synthetic 20 requests/second, burst-four
provider policy and the existing inbox worker's pool_size=8 configuration.
Amelia: preparation 66.137 seconds, total 134.741 seconds.
Cristina: preparation 72.038 seconds, total 138.650 seconds.
Both scenarios verified 100 distinct accepted provider IDs, 100 materialized
messages and completed Moxy events. Two tests passed in 293.11 seconds.

Limits: synthetic model and transport, plain replies, no approved reply pacing,
one creator at a time, no actual AI/provider latency. This is not a 50-creator
or live 25-180-second qualification. Actual-model evaluation remains unrun.

Failed exploratory runs are retained as evidence: the first omitted the shared
runtime schema, the next reused the same synthetic provider ID, and a later run
used the generic fixture's two-connection pool with four dispatch threads and
exhausted it. No production gate or receipt validation was weakened. Only the
worker-equivalent eight-connection configuration has this passing load result.

## Remaining completion work

Do not label this candidate complete. Remaining work includes broader provider
fault/recovery and simultaneous multi-creator qualification; source-required
budget/timing/service workflows; end-to-end imported asset descriptions;
additional held-out examples and actual-model evaluation; and approved actual
catalogue/sequence/purchase mappings and operational ownership.

GitHub publication and copying a source/log archive back to the agent were
rejected by automatic approval review for missing explicit permission to
transmit private material. No push/export was performed by those blocked calls.
Source commits and evidence remain on the dedicated Work Server.

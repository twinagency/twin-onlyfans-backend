# Code handover: 28 October 2026

## Branch for new backend changes

Use `codex/onlyfans-of-b1`, the existing default branch, after the handover consolidation PR is merged. It includes runtime source `293b23dbdf8dd5e97f27d6c0bef8ef4710415c06` and deployment evidence `d17fa2c516c7f2e12acc9dacadd095c99ae3b4a6`.

This is source consolidation. It does not deploy, activate accounts, widen the eight-chat scope, change payment holds or authorize a paid test.

## Required operational scripts

The operations directory contains the preserved cutover, backup, restore, scoped activation and post-deployment scripts. `astra-current-financial-gates.py` and `caption-ssm-exec.cjs` were recovered from the Windows workspace for this handover. The local `astra-post-deploy-check.py` is identical to the already committed copy after normalizing line endings.

These are dated operational records, not an unattended deployment command. Read the release README and SCOPED-ACTIVATION-20261028.md before use. Several scripts intentionally mutate production and require the current runbook conditions. Do not execute them as part of repository cleanup.

The preserved SSM helper uses the operator's existing AWS CLI `tsocial` profile, region `ap-southeast-2`, and instance `i-09a9dd3bd749643a5`. It accepts a Python script path, or `get <command-id>`. It creates adjacent `.ssm-private.json` and `.command.json` files; these execution outputs must stay outside Git. No credentials are included.

## Creator sign-in source

`creator-sign-in/`, `Dockerfile.sign-in`, and `deploy/creator-sign-in-compose.yml` are included in the same repository. The deployment record identifies image `sha256:b63d926bf03c7b6abe5a52c9bfa77b4d6821d7543d16ec1ab64155c1dc925b6e`. Its server source was verified against `293b23d:creator-sign-in/server.mjs`, SHA256 `2678c63ef05abaea2d4471e70ae24b04dcebaafbc624ffd89d0801339b77452f`. This is recorded source evidence, not a claim that rebuilding now reproduces the original image digest.

## Dashboard source lines

Twin/Moxy: `twinagency/twin-dashboard`, `release/twin-20261028`, incorporating deployed source `4599999761dbf2ff2b517359642d10262324867a`.

FLIRT is maintained separately because it has independent branding, configuration and deployed source. Consult that repository's handover record for its final release branch. Do not merge the FLIRT product line wholesale into Twin/Moxy.

## Existing limits

The prior acceptance evidence remains attributed to its exact source pair and image identities. Production/provider proof, full activation, payment reconciliation and capacity decisions remain separate. Older draft PRs and experimental branches are not automatically production-approved by this source handover.

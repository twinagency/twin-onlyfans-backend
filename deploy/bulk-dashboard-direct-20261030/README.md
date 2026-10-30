# Direct AWS release of existing-chat AI controls

User explicitly authorized a direct AWS deployment after GitHub Actions refused startup for failed payment authorization. Source is the already reviewed/tested dashboard commit 2f98dd5a1f629db17e14bdf03cd960382d9cf432 (dashboard PR340). Companion API source cb937394e0c9bd914b499080d5913b9b0342c5f3 is already live; no backend restart or fan activation is part of this dashboard rollout.

## Build path

The existing AWS CodeBuild project twin-onlyfans-release builds an exact GitHub source archive copied to the existing private release-transfer bucket. GitHub Actions and its billing are not used. A temporary expiring permission grants the existing build role read access to only this archive. It is removed after the build. No role can read broader source paths as a result of this release.

The existing build role can publish only to twin-onlyfans ECR, so the dashboard artifact uses the unique tag dashboard-bulk-2f98dd5 in that repository. Deploy by its immutable digest, not by the tag. The default CodeBuild project configuration is unchanged; source, buildspec, image and compute selections are per-build overrides. No long-lived credential is included in source or buildspec.

First build d72fdd5a-61c5-4de5-9820-9009f37e4731 stopped safely before image construction because Ubuntu22's FFmpeg4.4 lacks the display_rotation option required by media tests. The retry uses AWS standard8 (Ubuntu24), preserving the checks rather than weakening the tests. Retry build5445a1a2-7d71-435a-adaf-9bfa9fdf624d.

The build runs full TypeScript checking, existing onboarding/session/creator/permission regressions, test:creator-upload and test:offline before the normal Linux Docker build. The image verifies that the new bulk route is packaged. Existing native image load checks remain in Dockerfile.

## Deployment and recovery

The deploy script requires a successful build record and immutable image reference. It refuses concurrent image/source changes, pulls the artifact, checks both button labels in the browser assets and the new route manifest, and waits for staged creator uploads to clear before each stop. It retains the preceding containers under names next-app-rollback-2f98dd5 and next-app-of-rollback-2f98dd5.

Each replacement preserves all runtime environment values except the intentional GIT_SHA update, plus commands, entrypoints, mounts, ports, resource limits, log configuration and security settings. Failures restore every container already changed. Protected runtime snapshots stay on the host only, never in this repository. An unauthenticated write must be refused. Each dashboard must reach the live bulk API with its existing server credential and receive strict validation refusal for an empty batch, which performs no fan writes. The existing live earnings/private-media recovery suite must pass. The API container ID must stay unchanged.

Protected host evidence: /var/lib/twin-release/bulk-dashboard-direct-2f98dd5.

No real fan bulk enable/disable or message send is used as a release test. Runtime confirmation is recorded separately after rollout.

## Verified live result

AWS retry build5445a1a2-7d71-435a-adaf-9bfa9fdf624d completed SUCCEEDED, including PRE_BUILD, BUILD and POST_BUILD. Source archive and checks unchanged; Ubuntu24 resolves the builder-only video dependency problem. Published immutable image: 323149985112.dkr.ecr.ap-southeast-2.amazonaws.com/twin-onlyfans@sha256:34dd3c4995040e7906506f8502618ac8e9f9ba6cb515a5f158576d29fcffb382.

Direct deployment SSM64b9d332-d10a-45c8-8426-000fccbcc21e completed Success. Both next-app and next-app-of serve source2f98dd5a1f629db17e14bdf03cd960382d9cf432 and image34dd3c4995040e7906506f8502618ac8e9f9ba6cb515a5f158576d29fcffb382. Both return HTTP200 on readiness and have zero restarts. Runtime environment and host configuration comparisons passed. Both browser bundles contain Enable AI replies for existing chats and Disable AI replies for existing chats. The new Next route is packaged; unauthenticated calls redirect307 to sign-in. From each running dashboard, the existing control credential reached the live API and an empty batch was refused400/invalid_request before any fan writes.

The live dashboard earnings and private-media recovery suite passed. The API container was not replaced. Previous dashboards remain stopped as rollback containers. The temporary exact-archive read policy ReadBulkDashboard2f98dd5 was deleted successfully after build completion. GitHub billing and account limits were not changed.

This verifies deployment, packaged controls, authorization refusal and actual dashboard-to-API connectivity. No real fan's AI state was toggled for testing and no message was sent. The user must refresh their existing inbox to load the new browser bundle.

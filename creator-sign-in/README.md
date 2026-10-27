# Private creator sign-in

Paired with dashboard PR #197. This service hosts temporary login browsers; canonical identities, content, connections and messaging remain in the existing Twin systems. No external OnlyFans API vendor is involved.

## Runtime

Build `Dockerfile.sign-in` outside the production host. The Playwright library and browser image are pinned to the same version. `deploy/creator-sign-in-compose.yml` adds a non-root, read-only container to the existing native compose project with Chromium sandboxing, dropped capabilities, an init process, bounded memory/processes and temporary storage. It publishes no host port. The seccomp profile derives from Microsoft Playwright v1.61.1 `utils/docker/seccomp_profile.json` (Apache-2.0): <https://github.com/microsoft/playwright/blob/v1.61.1/utils/docker/seccomp_profile.json>. Its user-namespace allowance additionally includes `chroot`, needed by Chromium's sandbox when all host capabilities are dropped. Linux still requires the appropriate capability inside the isolated user namespace; no host capability is granted.

The default rollout allows **one** browser at a time. A full slot returns an honest retry message: HTTP 429 with a distinct code (`capacity`, `start_limit`, `frame_limit`) and `retryAfter` seconds (also the `Retry-After` header). Only `opening`/`waiting` attempts own a browser; a captured attempt (`ready`) has already closed it and does not count. An attempt nobody polls for `ONLYFANS_SIGNIN_IDLE_SECONDS` (default 300) is discarded as expired/`idle`, so a closed tab frees the slot in minutes instead of after the full 15-minute lifetime. A browser launch that does not return within 60 s fails the attempt and does not count against the creator's six starts per hour. `GET /v1/attempts/<id>` also returns `detail` (`reason`, `challenge`, `idleExpiresAt`); the `attempt` object itself is unchanged. Do not put this on a loaded small host without measuring available RAM and confirming that its user-namespace/AppArmor configuration supports Chromium's sandbox. A failed sandbox launch is a deployment failure; do not switch to root, privileged mode or `--no-sandbox`.

## Configuration and rollout

1. Run the browser fixture and native PostgreSQL acceptance workflows. Build the image and run the headed container fixture with the same security flags as compose. Verify these flags on the target host as well; a CI host has a different kernel policy.
2. Deploy matching native API and worker code through the existing native release process. No new database migration is required. Keep the existing private connection token and operator-controlled activation settings.
3. Generate a distinct high-entropy `ONLYFANS_SIGNIN_TOKEN` in the deployment secret mechanism; do not reuse native read/control/connection credentials. Supply it only to the browser service and dashboard server. Do not print it, commit an env file or expose it through a `NEXT_PUBLIC_` variable.
4. Set `ONLYFANS_SIGNIN_IMAGE` to the tested image digest. From the existing native deployment directory, combine `onlyfans-compose.yml` with `creator-sign-in-compose.yml` and start only the new `creator-sign-in` service. Do not restart the database or unrelated workers. Docker compose resolves the seccomp path relative to the first compose file's directory.
5. Verify the service health and unauthenticated refusal over the private Docker network. The dashboard server already joins `twin-onlyfans_default`; its `ONLYFANS_SIGNIN_BASE_URL` is `http://creator-sign-in:8479`. Do not publish port 8479 or proxy the private REST API through Apache.
6. Deploy the paired dashboard and set its private sign-in URL/token. Without these two settings it keeps the existing desktop path. Verify the running container commit, not just a built image tag.
7. Have the explicitly selected creator open the normal `/creator/platforms` page, sign in and confirm the displayed account. Verify the exact numeric owner, accepted enrollment and verified unexpired connection. Then check real history sync, expiry/reconnect, cleanup and preserved agency/proxy/model settings. A remote mobile browser test is not proof of a physical phone keyboard or provider challenge compatibility.

The first-account path currently uses **direct** egress. Keep the browser on the same host and egress as the worker. Proxied reconnects use the existing account's private proxy. Moving the service to another host requires an explicit common proxy assignment before release; the direct fingerprint only denotes configuration and cannot prove public IP equality.

## Failure and rollback

An unavailable browser or expired attempt shows a recoverable error; it cannot create an account by itself. Each request is bound to the current authenticated creator via the dashboard. A capture closes its browser before the explicit confirmation. The existing encrypted enrollment is durable before acknowledgement removes the in-memory capture. Retrying a lost acknowledgement submits the exact same existing enrollment command.

On cancellation, timeout, failure or shutdown, browsers and transient captures are discarded. Browser context files and Xvfb artifacts use the container's temporary filesystem. No keystroke, password, request-body or frame recording is configured. Provider challenge completion is left to the creator, with no challenge bypass.

To disable the new entry point, remove the dashboard's two private sign-in settings and redeploy the last verified dashboard configuration, then stop only `creator-sign-in`. Active attempts are discarded; previously accepted native connections remain in the existing encrypted store. Connecting and rollback never enable outgoing messages. Live sending is controlled separately by the existing agency/operator activation path.

## Checks

- `npm ci --prefix creator-sign-in && node creator-sign-in/check.mjs`: actual browser/interceptor against local provider fixtures.
- `.github/workflows/creator-sign-in.yml`: browser fixture and headed sandboxed-container fixture.
- `.github/workflows/creator-platforms.yml`: disposable PostgreSQL ownership, connection and worker acceptance.
- Dashboard `scripts/check-creator-platforms.cjs` and `scripts/check-creator-sign-in-ui.mjs`: protected routes, mobile/desktop input, confirmation, exact retry, expiry and auth loss.

The generic inherited native CI runner currently invokes pytest-based files directly without installing pytest; its missing-pytest failure is separate from these dedicated acceptance jobs. Record all job results, including failures, before merging.

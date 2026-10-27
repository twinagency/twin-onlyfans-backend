# Targeted FASTT requested-caption revert

This reverses only the four-module optional named-request caption enhancement from 98923531d0531944b8dfe5a28d24f03f2718f170. The integration source retains b2a00c324b186fb5394b9b973334fc39753cc1f7 as an ancestor. Product changes are the reverse of the original hunks, not whole-file restoration.

Production builds use the pinned current handover image as parent and COPY only the four reverted modules plus their restored-behaviour test. They do not deploy the integration tree's unrelated pending migrations, dependencies or services. Existing entrypoint, runtime environment, mounts, proxies, secrets, account scope and persistent state are retained. Runtime files outside those four must match the captured parent hashes.

The separate AI-05 native/catalogue composer in onlyfans_runtime/native_planning.py remains unchanged in the integration source. Its reply-first selected-description/price composition and complete-final-text validation are a different path from FASTT's active vault ask sender; native/catalogue acceptance must be reported separately from active-sender verification.

Build from an archive of this commit, using this Dockerfile, SOURCE_COMMIT equal to the commit and SOURCE_DATE_EPOCH equal to its commit timestamp. The release proof records the resulting image ID, source archive checksum, all live Python hashes and retained parent/rollback archive checksums. No image layer contains newly copied production credentials or mutable data.

Required checks: all named matching requests make zero action-phrase model calls; substitute/unnamed legacy generation remains; attachment order, exact price, preview, recipient/account, audience, removed-media and uncertain-attempt safeguards remain. Run native caption/vault/pricing, runner/service/recovery and PostgreSQL media/isolation/send-recovery regressions. A real-provider check remains separate and must not fabricate a fan input or resend an uncertain attempt.

Use Docker Buildx/BuildKit with provenance and SBOM disabled, SOURCE_DATE_EPOCH from the commit, and the docker exporter rewrite-timestamp=true. Before build, bind the local base tag above to the full ab3bfc200c0e93a8fe5ebfd1f93790efb5f4e96981af7999cb33b314e8e42207 image ID and verify it. Perform two clean builds and compare the loaded image IDs. The separately committed PostgreSQL fixture correction names existing control columns; it removes no assertions.

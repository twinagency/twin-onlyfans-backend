#!/usr/bin/env bash
# build-candidate.sh  --  PREPARED SCRIPT (Step 7 frozen-release packet).  Run on the Work Server only.
#
# Builds ONE immutable candidate image from ONE committed source revision and runs the named suites on that exact
# image with NO source overlay: the run commands below contain no bind mount of source, tests, ops, baseline reader,
# test packages or pg tools.  Everything the suites need is baked into the image from the commit or from a pinned
# input recorded in the build manifest.
#
# It does NOT deploy, push, tag a registry, call a provider or model, touch AWS, or modify any worktree
# (the repository is read through `git archive` only).  Containers use --cpus 1 --memory 1g and are removed on exit.
#
# Usage:
#   Without a suite (or --tests) the invocation is REFUSED before anything is built; only an explicit --build-only builds
#   without qualifying. Qualification always reports every required group, and exits non-zero while any is missing.
#   build-candidate.sh <commit> [--tag IMAGE:TAG] [--label NAME] [--suite NAME]... [--tests "tests/a.py tests/b.py"]
#                      [--build-only] [--no-build] [--dry-run] [--keep-pg]
#   suites: smoke | smoke-probes | r0 | step4 | step5 | migrations | operator | ops | planc-all | matrix | completion | list
#   Example (the trial recorded in RELEASE-PACKET.md):
#     build-candidate.sh 48e007a --tag twin-commercial-candidate:48e007a-trial --label trial --suite smoke
#
# Environment overrides (defaults are the values used for the trial):
#   REPO             read-only source repository          /srv/projects/claude-commercial-integration4-20261112
#   OUT_ROOT         where build records are written      /srv/projects/claude-release-packet-20261112
#   TEST_PACKAGES    pytest 8.3.5 + deps (pure python)    /srv/projects/twin-conversation-engine-20261107/test-packages
#   PY_IMAGE         base image (Dockerfile.onlyfans)     python:3.12-slim   (used from the local store; never pulled)
#   PG_IMAGE         source of the pg16 client binaries   postgres:16        (also the server image for the tests)
#   BASELINE_COMMIT  old reader baked at /opt/r0-baseline 659ea2913a7ea203024ea58a992f24b0fcea6759 (the R0 trial baseline)
#   DEP_REFERENCE_IMAGE  image whose `pip freeze` is compared (information only)  twin-engine-offline-main:20261107-r6
#   SUITE_TIMEOUT    seconds per suite                    3600
#   LOAD_WAIT_MAX    seconds to wait for loadavg < 4      900
#   DASHBOARD_IMAGE  expected Q14 dashboard image digest sha256:<64 hex>  (required for qualification)
#   DASHBOARD_COMMIT expected Q14 dashboard source commit, 40 hex; must equal that image's revision label (required)
set -euo pipefail

EXPECTED_HOST=ubuntu-s-4vcpu-8gb-syd1
REPO=${REPO:-$(git rev-parse --show-toplevel)}
OUT_ROOT=${OUT_ROOT:?Set OUT_ROOT to a private evidence directory outside the repository}
TEST_PACKAGES=${TEST_PACKAGES:-/srv/projects/twin-conversation-engine-20261107/test-packages}
RUNTIME_IMAGE=${RUNTIME_IMAGE:-sha256:0312342500f2ac5f672f38debe8ba48b5c22835c65f4007e6f8322eb49310e15}
PY_IMAGE=${PY_IMAGE:-python:3.12-slim}
PG_IMAGE=${PG_IMAGE:-postgres:16}
BASELINE_COMMIT=${BASELINE_COMMIT:-659ea2913a7ea203024ea58a992f24b0fcea6759}
ROLLBACK_COMMIT=${ROLLBACK_COMMIT:-e79380fe5facb97d49791d527780db4a92bb2f37}
DEP_REFERENCE_IMAGE=${DEP_REFERENCE_IMAGE:-twin-engine-offline-main:20261107-r6}
SUITE_TIMEOUT=${SUITE_TIMEOUT:-3600}
LOAD_WAIT_MAX=${LOAD_WAIT_MAX:-900}

# The test-harness environment, exactly as the Step 4-6 runs used it (minus every source mount).
TEST_PYTHONPATH=/opt/test-packages:/app:/app/service:/app/service/tests:/app/ops/plan-c-supervision

die() { echo "build-candidate: $*" >&2; exit 2; }

# ---- suite definitions (paths relative to /app/service; each group is the file set the earlier logs used) -----------
suite_files() {
  case "$1" in
    smoke)        echo "tests/test_tip_evidence_kind_migration_pg.py" ;;
    smoke-probes) echo "tests/test_plan_c_r0_acceptance_pg.py::test_mixed_versions_fail_closed_and_rollback_never_drops_retry_evidence" ;;
    r0)           echo "tests/test_commercial_retry_due_lineage_pg.py tests/test_plan_c_r0_operator_timing_pg.py tests/test_plan_c_r0_red_green_pg.py tests/test_plan_c_r0_acceptance_pg.py tests/test_plan_c_model_retry_due_pg.py tests/test_plan_c_compressed_retry_pg.py" ;;
    step4)        echo "tests/test_commercial_normal_worker_pg.py tests/test_commercial_sale_rules_pg.py tests/test_commercial_unlock_pg.py tests/test_commercial_composed_pg.py" ;;
    step5)        echo "tests/test_coexistence_scope_contract.py tests/test_coexistence_staff_pg.py tests/test_voluntary_tips_pg.py tests/test_tip_evidence_kind_migration_pg.py" ;;
    migrations)   echo "tests/test_commercial_retry_due_lineage_pg.py tests/test_engine_rc_join_pg.py tests/test_history_coverage_schema_pg.py tests/test_plan_c_model_billing_pg.py tests/test_plan_c_grant_migration_pg.py tests/test_plan_c_scope_pg.py tests/test_commercial_terms_migration_pg.py tests/test_commercial_scope_guard_pg.py tests/test_preparation_retry_state_pg.py tests/test_onlyfans_migration_pg.py tests/test_contextual_item_scope_migration.py tests/test_onlyfans_launch_schema_upgrade_pg.py tests/test_tip_evidence_kind_migration_pg.py" ;;
    operator)     echo "tests/test_engine_operator_pg.py tests/test_plan_c_q2_n4_operator_http_pg.py tests/test_plan_c_u3_readiness_pg.py" ;;
    matrix)       echo "tests/test_commercial_matrix_pg.py tests/test_commercial_ten_creators_pg.py tests/test_commercial_import_to_sale_pg.py" ;;   # Step 6; files exist only once committed
    completion)   echo "tests/test_commercial_qualification_gate.py tests/test_commercial_completion_pg.py tests/test_commercial_stopped_activation_pg.py tests/conversation_engine/test_claims_semantics.py tests/test_openrouter_decoding.py tests/test_plan_c_unknown_send_evidence_pg.py" ;;
    preparation)  echo "tests/test_commercial_preparation_pg.py tests/test_preparation_retry_state_pg.py tests/test_commercial_classification_refresh_pg.py tests/test_history_import_pg.py" ;;
    normal)       echo "tests/test_commercial_normal_control_journey_pg.py tests/test_commercial_terms_http_pg.py tests/test_commercial_shared_provider_pg.py tests/test_commercial_parallel_workers_pg.py tests/test_plan_c_inflight_readiness.py tests/test_commercial_readiness_budget_pg.py" ;;
    legacy)       echo "tests/test_commercial_progress.py tests/test_commercial_full_path.py tests/test_history_capacity_turns.py tests/test_r2_complete_answers.py tests/test_plan_c_sim_pg.py" ;;
    recovery)     echo "tests/test_commercial_ops_recovery_pg.py tests/test_onlyfans_release_package_pg.py tests/test_onlyfans_recovery_pg.py" ;;
    delivery)     echo "tests/test_onlyfans_text_delivery_pg.py tests/test_concurrent_delivery_pg.py tests/test_plan_c_compressed_retry_pg.py tests/test_plan_c_auth_recovery_pg.py tests/test_plan_c_conflicting_webhook_pg.py" ;;
    ops)          echo "OPS" ;;       # handled specially: pytest in /app/ops/plan-c-supervision
    planc-all)    echo "PLANC_ALL" ;; # every tests/test_plan_c_*.py present in the image
    *) return 1 ;;
  esac
}
# Required groups cover the frozen release contract; custom tests supplement rather than replace them.

# ---- argument parsing -------------------------------------------------------------------------------------------------
REQUIRED_SUITES=(completion matrix step4 step5 r0 operator migrations ops preparation normal legacy recovery delivery)
COMMIT_ARG=""; TAG=""; LABEL=""; SUITES=(); CUSTOM_TESTS=""; BUILD_ONLY=0; NO_BUILD=0; DRY=0; KEEP_PG=0
while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG=$2; shift 2 ;;
    --label) LABEL=$2; shift 2 ;;
    --suite) if [ "$2" = all ]; then SUITES+=("${REQUIRED_SUITES[@]}"); else SUITES+=("$2"); fi; shift 2 ;;
    --tests) CUSTOM_TESTS=$2; shift 2 ;;
    --build-only) BUILD_ONLY=1; shift ;;
    --no-build) NO_BUILD=1; shift ;;
    --dry-run) DRY=1; shift ;;
    --keep-pg) KEEP_PG=1; shift ;;
    -h|--help) sed -n '2,31p' "$0"; exit 0 ;;
    list) echo "smoke smoke-probes r0 step4 step5 migrations operator matrix completion ops planc-all"; exit 0 ;;
    -*) die "unknown option $1" ;;
    *) [ -z "$COMMIT_ARG" ] || die "one commit only"; COMMIT_ARG=$1; shift ;;
  esac
done
[ -n "$COMMIT_ARG" ] || die "usage: build-candidate.sh <commit> [options]  (see header)"
for s in "${SUITES[@]:-}"; do [ -z "$s" ] || suite_files "$s" >/dev/null || die "unknown suite '$s'"; done
if [ "$BUILD_ONLY" = 1 ]; then
  [ "${#SUITES[@]}" = 0 ] && [ -z "$CUSTOM_TESTS" ] || die "--build-only cannot be combined with qualification suites"
else
  [ "${#SUITES[@]}" -gt 0 ] || [ -n "$CUSTOM_TESTS" ] || die "qualification incomplete: no suites requested; use --suite all or explicit --build-only"
fi

# ---- preconditions (read-only checks) -----------------------------------------------------------------------------------
[ "$(hostname)" = "$EXPECTED_HOST" ] || die "run on the Work Server ($EXPECTED_HOST), not $(hostname)"
command -v docker >/dev/null || die "docker not found"
[ -d "$REPO/.git" ] || [ -f "$REPO/.git" ] || die "REPO is not a git checkout: $REPO"
FULL=$(git -C "$REPO" rev-parse --verify "${COMMIT_ARG}^{commit}") || die "commit not found in $REPO: $COMMIT_ARG"
SHORT=${FULL:0:7}
BASE_FULL=$(git -C "$REPO" rev-parse --verify "${BASELINE_COMMIT}^{commit}") || die "baseline commit not found: $BASELINE_COMMIT"
TREE=$(git -C "$REPO" rev-parse "${FULL}^{tree}")
ROLLBACK_FULL=$(git -C "$REPO" rev-parse "${ROLLBACK_COMMIT}^{commit}")
for f in Dockerfile.onlyfans requirements.txt requirements-onlyfans-test.txt; do
  git -C "$REPO" cat-file -e "${FULL}:${f}" 2>/dev/null || die "$f missing at $SHORT"
done
[ -d "$TEST_PACKAGES/pytest" ] || die "TEST_PACKAGES has no pytest: $TEST_PACKAGES"
TAG=${TAG:-twin-commercial-candidate:${SHORT}}
LABEL=${LABEL:-$SHORT}
BUILD=$OUT_ROOT/build-$LABEL
RUN_NET=codex-candidate-net-$LABEL-$$
RUN_PG=claude-rp-pg-$LABEL-$$
RUN_T=claude-rp-run-$LABEL-$$

# ---- generated build inputs ---------------------------------------------------------------------------------------------
generate_dockerfile() {
cat <<'DOCKER'
ARG RUNTIME_IMAGE
FROM ${RUNTIME_IMAGE}
WORKDIR /app
RUN rm -rf /app/service /app/ops
COPY src/service/ /app/service/
COPY src/ops/ /app/ops/
COPY rollback/ /opt/compatible-rollback/
ARG SOURCE_COMMIT
ARG SOURCE_TREE
ARG BASELINE_COMMIT
ARG ROLLBACK_COMMIT
LABEL org.opencontainers.image.revision=$SOURCE_COMMIT io.twin.source.tree=$SOURCE_TREE io.twin.r0.baseline=$BASELINE_COMMIT io.twin.rollback.revision=$ROLLBACK_COMMIT
ENV BUILD_SHA=$SOURCE_COMMIT
DOCKER
}

if [ "$DRY" = 1 ]; then
  echo "# DRY RUN: nothing is built, started or written."
  echo "commit      $FULL (tree $TREE)"
  echo "baseline    $BASE_FULL"
  echo "tag         $TAG"
  echo "build dir   $BUILD"
  echo "base images $PY_IMAGE=$(docker image inspect "$PY_IMAGE" --format '{{.Id}}' 2>/dev/null || echo MISSING)  $PG_IMAGE=$(docker image inspect "$PG_IMAGE" --format '{{.Id}}' 2>/dev/null || echo MISSING)"
  for s in "${SUITES[@]:-}"; do [ -z "$s" ] || echo "suite $s -> $(suite_files "$s")"; done
  echo "---- Dockerfile.candidate ----"; generate_dockerfile
  exit 0
fi

# ---- cleanup -----------------------------------------------------------------------------------------------------------
cleanup() {
  docker rm -f "$RUN_T" >/dev/null 2>&1 || true
  [ "$KEEP_PG" = 1 ] || docker rm -fv "$RUN_PG" >/dev/null 2>&1 || true
  [ "$KEEP_PG" = 1 ] || docker network rm "$RUN_NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

wait_for_load() {
  local waited=0
  while [ "$(awk '{print int($1)}' /proc/loadavg)" -ge 4 ]; do
    [ "$waited" -lt "$LOAD_WAIT_MAX" ] || die "host load stayed >= 4 for ${LOAD_WAIT_MAX}s; not starting (other work is running)"
    sleep 15; waited=$((waited+15))
  done
}

# ---- build -------------------------------------------------------------------------------------------------------------
[ ! -e "$BUILD" ] || die "$BUILD already exists; choose a fresh --label (records are never overwritten)"
mkdir -p "$BUILD/ctx/src" "$BUILD/ctx/baseline" "$BUILD/ctx/rollback"
git -C "$REPO" archive "$FULL" service ops | tar -x -C "$BUILD/ctx/src"
git -C "$REPO" archive "$ROLLBACK_FULL" service | tar -x -C "$BUILD/ctx/rollback"
git -C "$REPO" archive "$BASE_FULL" service | tar -x -C "$BUILD/ctx/baseline"
( cd "$BUILD/ctx/baseline" && find service -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum ) > "$BUILD/baseline-files.sha256"
( cd "$BUILD/ctx/rollback" && find service -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum ) > "$BUILD/rollback-files.sha256"
( cd "$BUILD/ctx/src" && find service ops -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum ) > "$BUILD/source-files.sha256"
printf 'requested_commit=%s\nrequested_tree=%s\nruntime_image=%s\n' "$FULL" "$TREE" "$RUNTIME_IMAGE" > "$BUILD/manifest.txt"
if [ "$NO_BUILD" = 0 ]; then
  git -C "$REPO" archive "$FULL" service ops Dockerfile.onlyfans requirements.txt requirements-onlyfans-test.txt | tar -x -C "$BUILD/ctx/src"
  git -C "$REPO" archive "$BASE_FULL" service | tar -x -C "$BUILD/ctx/baseline"
  printf '#!/bin/sh\n# runs the unmodified postgres client binary with its own libraries\nLD_LIBRARY_PATH=/opt/pg16/lib exec /opt/pg16/real/$(basename "$0") "$@"\n' > "$BUILD/ctx/pg-wrapper.sh"
  generate_dockerfile > "$BUILD/ctx/Dockerfile.candidate"
  cp "$BUILD/ctx/Dockerfile.candidate" "$BUILD/Dockerfile.candidate"

  # pins recorded BEFORE the build: what exactly goes in
  ( cd "$BUILD/ctx/src" && find service ops -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum ) > "$BUILD/source-files.sha256"
  ( cd "$BUILD/ctx/baseline" && find service -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum ) > "$BUILD/baseline-files.sha256"
  sha256sum "$BUILD/ctx/src/requirements.txt" "$BUILD/ctx/src/requirements-onlyfans-test.txt" "$BUILD/ctx/src/Dockerfile.onlyfans" > "$BUILD/dependency-locks.sha256"
  {
    echo "source_commit=$FULL"; echo "source_tree=$TREE"; echo "baseline_commit=$BASE_FULL"; echo "tag=$TAG"
    echo "runtime_image=$RUNTIME_IMAGE id=$(docker image inspect "$RUNTIME_IMAGE" --format '{{.Id}}')"
    echo "pg_image=$PG_IMAGE id=$(docker image inspect "$PG_IMAGE" --format '{{.Id}}')"
    echo "built_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"; echo "docker=$(docker --version)"
    echo "source_files=$(wc -l < "$BUILD/source-files.sha256") baseline_files=$(wc -l < "$BUILD/baseline-files.sha256")"
  } > "$BUILD/manifest.txt"

  wait_for_load
  # Reuse the digest-pinned runtime dependencies; building and testing require no external egress.
  docker build --pull=false --memory 1g --cpu-period 100000 --cpu-quota 100000 \
    --network none --build-arg RUNTIME_IMAGE="$RUNTIME_IMAGE" \
    --build-arg SOURCE_COMMIT="$FULL" --build-arg SOURCE_TREE="$TREE" --build-arg BASELINE_COMMIT="$BASE_FULL" --build-arg ROLLBACK_COMMIT="$ROLLBACK_FULL" \
    -f "$BUILD/ctx/Dockerfile.candidate" -t "$TAG" "$BUILD/ctx" > "$BUILD/build.log" 2>&1 || { tail -30 "$BUILD/build.log" >&2; die "docker build failed (see $BUILD/build.log)"; }
fi

# Resolve and pin the actual image, never keep using a mutable tag.
IMAGE_ID=$(docker image inspect "$TAG" --format '{{.Id}}') || die "image $TAG not found"
ACTUAL_COMMIT=$(docker image inspect "$IMAGE_ID" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')
ACTUAL_TREE=$(docker image inspect "$IMAGE_ID" --format '{{index .Config.Labels "io.twin.source.tree"}}')
[ "$ACTUAL_COMMIT" = "$FULL" ] && [ "$ACTUAL_TREE" = "$TREE" ] || die "image revision/tree does not match requested commit"
BUILD_RECORD=$BUILD
docker image inspect "$TAG" > "$BUILD_RECORD/image-inspect.json"
{ echo "image_id=$IMAGE_ID"; echo "repo_digests=$(docker image inspect "$TAG" --format '{{json .RepoDigests}}')"; } >> "$BUILD_RECORD/manifest.txt"

[ "$(docker image inspect "$IMAGE_ID" --format '{{index .Config.Labels "io.twin.rollback.revision"}}')" = "$ROLLBACK_FULL" ] || die "rollback revision mismatch"
docker run --rm --network none --read-only --entrypoint sh "$IMAGE_ID" -c 'cd /opt/compatible-rollback && find service -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum' > "$BUILD_RECORD/rollback-image-files.sha256"
diff -q "$BUILD_RECORD/rollback-files.sha256" "$BUILD_RECORD/rollback-image-files.sha256" || die "rollback code differs from its commit"

# ---- verify the artifact: image tree == commit tree, tools present, dependencies ------------------------------------------
IMG_RUN=(docker run --rm --network none --pull never --cpus 1 --memory 1g --pids-limit 256 --cap-drop ALL --security-opt no-new-privileges --read-only --user 1000:1000 --tmpfs /tmp:rw,nosuid,size=256m -e HOME=/tmp)
"${IMG_RUN[@]}" --entrypoint sh "$IMAGE_ID" -c 'cd /app && find service ops -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum' > "$BUILD_RECORD/image-files.sha256"
if [ -f "$BUILD_RECORD/source-files.sha256" ] && diff -q "$BUILD_RECORD/source-files.sha256" "$BUILD_RECORD/image-files.sha256" >/dev/null; then
  echo "tree_check=PASS image /app/service+/app/ops equals git archive of $SHORT ($(wc -l < "$BUILD_RECORD/image-files.sha256") files, no extras)" | tee -a "$BUILD_RECORD/manifest.txt"
else
  echo "tree_check=FAIL image tree differs from the commit (see diff of source-files.sha256 / image-files.sha256)" | tee -a "$BUILD_RECORD/manifest.txt"
  die "image is not an exact build of $SHORT"
fi
"${IMG_RUN[@]}" --entrypoint sh "$IMAGE_ID" -c 'cd /opt/r0-baseline && find service -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum' > "$BUILD_RECORD/baseline-image-files.sha256"
diff -q "$BUILD_RECORD/baseline-files.sha256" "$BUILD_RECORD/baseline-image-files.sha256" >/dev/null || die "baseline reader differs from requested commit"
"${IMG_RUN[@]}" --entrypoint sh "$IMAGE_ID" -c 'set -eu; pg_dump --version; pg_restore --version; psql --version; python --version; ls -d /opt/r0-baseline/service /opt/test-packages/pytest /app/ops/plan-c-supervision' > "$BUILD_RECORD/tools.txt" 2>&1 || die "required image tools are missing"
PG_DUMP_VERSION=$(grep -m1 '^pg_dump' "$BUILD_RECORD/tools.txt" || true)
echo "pg_dump=${PG_DUMP_VERSION:-ABSENT}" | tee -a "$BUILD_RECORD/manifest.txt"
"${IMG_RUN[@]}" --entrypoint python "$IMAGE_ID" -m pip freeze > "$BUILD_RECORD/pip-freeze.txt" 2>&1 || die "dependency inventory failed"
"${IMG_RUN[@]}" --entrypoint python "$IMAGE_ID" -m pip check > "$BUILD_RECORD/pip-check.txt" 2>&1 || die "image dependencies are inconsistent"
if docker image inspect "$DEP_REFERENCE_IMAGE" >/dev/null 2>&1; then
  docker run --rm --network none --pull never --entrypoint python "$DEP_REFERENCE_IMAGE" -m pip freeze > "$BUILD_RECORD/pip-freeze-reference.txt" 2>&1 || true
  diff "$BUILD_RECORD/pip-freeze-reference.txt" "$BUILD_RECORD/pip-freeze.txt" > "$BUILD_RECORD/pip-freeze.diff" || true
  echo "pip_freeze_vs_reference_diff_lines=$(wc -l < "$BUILD_RECORD/pip-freeze.diff") (reference $DEP_REFERENCE_IMAGE; information only)" >> "$BUILD_RECORD/manifest.txt"
fi

[ "$BUILD_ONLY" = 0 ] || { echo "built $TAG = $IMAGE_ID"; exit 0; }

# ---- suites ------------------------------------------------------------------------------------------------------------

wait_for_load
docker network create --internal "$RUN_NET" >/dev/null
PG_IMAGE=$(docker image inspect "$PG_IMAGE" --format '{{.Id}}')
docker run -d --name "$RUN_PG" --network "$RUN_NET" --cpus 0.5 --memory 512m --shm-size 256m -e POSTGRES_HOST_AUTH_METHOD=trust "$PG_IMAGE" >/dev/null
# TCP check on purpose: the image's init server listens on the unix socket only, then restarts
for _ in $(seq 1 90); do docker exec "$RUN_PG" pg_isready -h 127.0.0.1 -U postgres >/dev/null 2>&1 && break; sleep 1; done
docker exec "$RUN_PG" pg_isready -h 127.0.0.1 -U postgres >/dev/null 2>&1 || die "postgres test container did not become ready"
echo "postgres: $(docker exec "$RUN_PG" postgres --version)" >> "$BUILD_RECORD/manifest.txt"

FAILED=0
RESULTS=$BUILD_RECORD/results.tsv; [ -f "$RESULTS" ] || printf 'suite\texit\tsummary\tlog\n' > "$RESULTS"
run_group() {   # name, then pytest args
  local name=$1; shift
  local log=$BUILD_RECORD/suite-$name.log rc=0 run_user=1000:1000
  local caps=()
  # ops tests build a root-owned 0600 marker (CHOWN/FOWNER/DAC_OVERRIDE) and then fork into the unprivileged worker
  # identity (setgroups/setgid/setuid -> SETUID/SETGID) to prove what THAT identity can read or write. Only this
  # test container gets them: nothing is added for production workers and every other group stays 1000:1000, no caps.
  [ "$name" != ops ] || { run_user=0:0; caps=(--cap-add CHOWN --cap-add FOWNER --cap-add DAC_OVERRIDE --cap-add SETUID --cap-add SETGID); }
  { echo "IMAGE=$IMAGE_ID"; echo "COMMIT=$FULL"; echo "GROUP=$name"; echo "OVERLAY=none (no -v mounts of source, tests, ops, baseline, packages or pg tools)"; echo "ARGS=$*"; } > "$log"
  set +e
  timeout "$SUITE_TIMEOUT" docker run --rm --name "$RUN_T" --pull never --cpus 1 --memory 1g --pids-limit 256 --cap-drop ALL "${caps[@]}" \
    --security-opt no-new-privileges --read-only --user "$run_user" --network "container:$RUN_PG" \
    --tmpfs /tmp:rw,nosuid,size=256m -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONHASHSEED=0 \
    -e "PYTHONPATH=$TEST_PYTHONPATH" -e PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    -e ONLYFANS_TEST_PG_EMPTY_TEMPLATE=1 -e ONLYFANS_TEST_PG_URL=postgresql://postgres@127.0.0.1:5432/postgres \
    -e ONLYFANS_TEST_PG_BIN=/opt/pg16/bin -e ONLYFANS_COMPATIBLE_ROLLBACK_DIR=/opt/compatible-rollback \
    -w /app/service --entrypoint python "$IMAGE_ID" -m pytest -q -p no:cacheprovider -p no:randomly -rs "$@" >> "$log" 2>&1
  rc=$?
  set -e
  docker rm -f "$RUN_T" >/dev/null 2>&1 || true
  echo "EXIT=$rc" >> "$log"
  local summary; summary=$(grep -E '^[0-9]+ (passed|failed|error|skipped)|^(=+ )?no tests ran|[0-9]+ (passed|failed)' "$log" | tail -1 || true)
  printf '%s\t%s\t%s\t%s\n' "$name" "$rc" "${summary:-no summary line}" "$log" | tee -a "$RESULTS"
  [ "$rc" = 0 ] || FAILED=1
  if grep -Eq '[0-9]+ skipped' "$log"; then
    echo "INCOMPLETE: skipped required checks need individual disposition" >> "$log"
    FAILED=1
  fi
  return 0
}

for s in "${SUITES[@]:-}"; do
  [ -n "$s" ] || continue
  case "$(suite_files "$s")" in
    OPS)       run_group "$s" /app/ops/plan-c-supervision ;;
    PLANC_ALL) mapfile -t F < <("${IMG_RUN[@]}" --entrypoint sh "$IMAGE_ID" -c 'cd /app/service && ls tests/test_plan_c_*.py'); run_group "$s" "${F[@]}" ;;
    *)         # shellcheck disable=SC2046
               present=(); for f in $(suite_files "$s"); do present+=("$f"); done
               if [ "$s" = matrix ]; then
                 missing=0; for f in "${present[@]}"; do "${IMG_RUN[@]}" --entrypoint sh "$IMAGE_ID" -c "test -f /app/service/$f" || missing=1; done
                 [ "$missing" = 0 ] || { FAILED=1; printf '%s\t%s\t%s\t%s\n' "$s" BLOCKED "Step 6 matrix files are not in this commit" - | tee -a "$RESULTS"; continue; }
               fi
               run_group "$s" "${present[@]}" ;;
  esac
done
if [ -n "$CUSTOM_TESTS" ]; then
  # shellcheck disable=SC2206
  run_group custom $CUSTOM_TESTS
fi
for required in "${REQUIRED_SUITES[@]}"; do
  found=0
  for chosen in "${SUITES[@]:-}"; do [ "$chosen" != "$required" ] || found=1; done
  if [ "$found" = 0 ]; then
    FAILED=1
    printf '%s\t%s\t%s\t%s\n' "$required" INCOMPLETE "required group omitted" - | tee -a "$RESULTS"
  fi
done
# Q14 must run against a frozen dashboard, BFF/backend and PostgreSQL. The expected dashboard artifact is an explicit
# input (DASHBOARD_IMAGE digest + DASHBOARD_COMMIT) that THIS host verifies from its own image store; the receipt is
# only accepted when it names exactly that backend/dashboard pair. A receipt can never choose its own dashboard.
browser_problem=""
if [ -z "${DASHBOARD_IMAGE:-}" ] || [ -z "${DASHBOARD_COMMIT:-}" ]; then
  browser_problem="Q14 expected dashboard image/commit not supplied"
elif ! [[ "$DASHBOARD_IMAGE" =~ ^sha256:[0-9a-f]{64}$ ]] || ! [[ "$DASHBOARD_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
  browser_problem="Q14 expected dashboard image/commit must be an immutable digest and a full commit"
elif [ "$(docker image inspect "$DASHBOARD_IMAGE" --format '{{.Id}}' 2>/dev/null || true)" != "$DASHBOARD_IMAGE" ]; then
  browser_problem="Q14 expected dashboard image is not in the local image store"
elif [ "$(docker image inspect "$DASHBOARD_IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" != "$DASHBOARD_COMMIT" ]; then
  browser_problem="Q14 dashboard image revision label differs from DASHBOARD_COMMIT"
elif [ -z "${BROWSER_QUALIFICATION_RECEIPT:-}" ] || [ ! -s "${BROWSER_QUALIFICATION_RECEIPT:-}" ]; then
  browser_problem="Q14 exact-artifact browser receipt missing"
fi
if [ -n "$browser_problem" ]; then
  FAILED=1
  printf '%s\t%s\t%s\t%s\n' browser INCOMPLETE "$browser_problem" - | tee -a "$RESULTS"
elif ! "${IMG_RUN[@]}" -v "$BROWSER_QUALIFICATION_RECEIPT:/browser-receipt.json:ro" --entrypoint python "$IMAGE_ID" /app/ops/verify-browser-receipt.py /browser-receipt.json "$IMAGE_ID" "$DASHBOARD_IMAGE" "$DASHBOARD_COMMIT" > "$BUILD_RECORD/browser-receipt-check.log" 2>&1; then
  FAILED=1
  printf '%s\t%s\t%s\t%s\n' browser INCOMPLETE "Q14 receipt invalid or backend/dashboard artifact mismatch" "$BUILD_RECORD/browser-receipt-check.log" | tee -a "$RESULTS"
else
  printf '%s\t%s\t%s\t%s\n' browser 0 "Q14 exact-artifact receipt verified" "$BUILD_RECORD/browser-receipt-check.log" | tee -a "$RESULTS"
fi
printf 'qualification_exit=%s\n' "$FAILED" >> "$BUILD_RECORD/manifest.txt"
echo "done: $TAG = $IMAGE_ID ; records in $BUILD_RECORD ; exit=$FAILED"
exit "$FAILED"

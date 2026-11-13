"""Qualification failures cannot be reported as a completed release (handoff section 11 and 9).

Covers the two false-success gaps found at 0969bee and the ops-container capability contract:
  A. build-candidate.sh run with no suites must never exit 0; only an explicit --build-only may skip qualification.
  B. the browser receipt must name the EXPLICITLY expected, independently verified dashboard artifact.
Prerequisite, missing-group, required-skip, source/tree and stale-manifest checks stay in the script and are pinned below
so that nobody "fixes" a red run by dropping a mandatory group.
"""
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ops/build-candidate.sh"
VERIFIER = ROOT / "ops/verify-browser-receipt.py"
SPEC = importlib.util.spec_from_file_location("browser_receipt", VERIFIER)
RECEIPT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECEIPT)
BACKEND = "sha256:" + "a" * 64
DASHBOARD = "sha256:" + "b" * 64
COMMIT = "7c798f58cc91ca386886cec93a7ee7da1c308cf9"


def receipt(tmp_path, **changes):
    value = dict(status="passed", sourceOverlay=False, backendImage=BACKEND, dashboardImage=DASHBOARD,
                 dashboardCommit=COMMIT, posts=4, paidCards=1, access=1,
                 journey="browser/BFF/backend/Postgres/autonomous worker")
    value.update(changes)
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps({k: v for k, v in value.items() if v is not ...}))
    return path


def verify(tmp_path, **changes):
    return RECEIPT.verify(receipt(tmp_path, **changes), BACKEND, DASHBOARD, COMMIT)


# ----------------------------------------------------------------------------------------------- B. receipt binding
def test_receipt_requires_exact_release_pair(tmp_path):
    assert verify(tmp_path)["status"] == "passed"
    for changes in ({"backendImage": "sha256:" + "c" * 64}, {"dashboardImage": "sha256:" + "c" * 64},
                    {"dashboardCommit": "0" * 40}, {"sourceOverlay": True}, {"status": "failed"},
                    {"paidCards": 0}, {"access": 0}, {"posts": 3}, {"journey": "api only"}):
        with pytest.raises(ValueError):
            verify(tmp_path, **changes)


def test_the_synthetic_mismatched_dashboard_probe_is_rejected(tmp_path):
    """The 0969bee reproduction: expected sha256:bbbb... but the receipt names sha256:cccc... and was accepted."""
    path = receipt(tmp_path, dashboardImage="sha256:" + "c" * 64)
    with pytest.raises(ValueError):
        RECEIPT.verify(path, BACKEND, "sha256:" + "b" * 64, COMMIT)
    result = subprocess.run(["python3", str(VERIFIER), str(path), BACKEND, "sha256:" + "b" * 64, COMMIT],
                            text=True, capture_output=True)
    assert result.returncode == 1 and "rejected" in result.stdout


def test_receipt_must_carry_the_expected_dashboard_commit_in_either_recorded_form(tmp_path):
    assert verify(tmp_path, dashboardCommit=..., heads={"dashboard": COMMIT, "backend": "1" * 40})["status"] == "passed"
    for changes in ({"dashboardCommit": ...},                                     # no commit at all
                    {"dashboardCommit": ..., "heads": {"dashboard": "0" * 40}},   # recorded commit differs
                    {"heads": {"dashboard": "0" * 40}},                           # the two recorded forms disagree
                    {"dashboardCommit": ..., "heads": "7c798f5"}):
        with pytest.raises(ValueError):
            verify(tmp_path, **changes)


@pytest.mark.parametrize("expected", ["", "dashboard:latest", "sha256:bad", None, "sha256:" + "B" * 64, BACKEND])
def test_receipt_refuses_missing_mutable_or_aliased_expected_dashboard(tmp_path, expected):
    with pytest.raises(ValueError):
        RECEIPT.verify(receipt(tmp_path, dashboardImage=expected or DASHBOARD), BACKEND, expected, COMMIT)


@pytest.mark.parametrize("commit", ["", "main", "7c798f5", "Z" * 40, None])
def test_receipt_refuses_missing_or_abbreviated_expected_commit(tmp_path, commit):
    with pytest.raises(ValueError):
        RECEIPT.verify(receipt(tmp_path), BACKEND, DASHBOARD, commit)


@pytest.mark.parametrize("field,value", [("posts", True), ("paidCards", True), ("access", True), ("posts", 4.0)])
def test_receipt_counts_are_integers_not_booleans_or_floats(tmp_path, field, value):
    with pytest.raises(ValueError):
        verify(tmp_path, **{field: value})


@pytest.mark.parametrize("argv", [[], ["r"], ["r", BACKEND], ["r", BACKEND, DASHBOARD]])
def test_verifier_cli_needs_all_four_explicit_inputs(tmp_path, argv):
    result = subprocess.run(["python3", str(VERIFIER), *argv], text=True, capture_output=True)
    assert result.returncode == 1 and "rejected" in result.stdout


def test_verifier_cli_accepts_only_the_exact_pair(tmp_path):
    path = receipt(tmp_path)
    ok = subprocess.run(["python3", str(VERIFIER), str(path), BACKEND, DASHBOARD, COMMIT], text=True, capture_output=True)
    assert ok.returncode == 0 and "exact backend/dashboard pair" in ok.stdout
    bad = subprocess.run(["python3", str(VERIFIER), str(path), BACKEND, "sha256:" + "d" * 64, COMMIT], text=True, capture_output=True)
    assert bad.returncode == 1


# ----------------------------------------------------------------------------------------------- A. invocation
def run_script(tmp_path, *args):
    return subprocess.run(["bash", str(SCRIPT), "HEAD", *args],
                          env={**os.environ, "REPO": str(ROOT), "OUT_ROOT": str(tmp_path)},
                          text=True, capture_output=True)


@pytest.mark.parametrize("args,reason", [
    ([], "qualification incomplete: no suites requested"),
    (["--label", "x"], "qualification incomplete: no suites requested"),
    (["--no-build"], "qualification incomplete: no suites requested"),
    (["--keep-pg"], "qualification incomplete: no suites requested"),
    (["--build-only", "--suite", "all"], "cannot be combined"),
    (["--build-only", "--tests", "tests/a.py"], "cannot be combined"),
    (["--suite", "missing"], "unknown suite"),
])
def test_harness_refuses_incomplete_invocation_before_build(tmp_path, args, reason):
    result = run_script(tmp_path, *args)
    assert result.returncode != 0, "a no-suite run must never exit 0"
    assert reason in result.stderr
    assert not list(tmp_path.iterdir()), "nothing may be written before the refusal"


def test_an_explicit_build_only_is_not_refused_by_the_invocation_check(tmp_path):
    """It then fails on this machine for a different reason (not the Work Server / not a git checkout), never on usage."""
    result = run_script(tmp_path, "--build-only")
    assert "qualification incomplete" not in result.stderr and "cannot be combined" not in result.stderr


# ----------------------------------------------------------------------------------------------- the script keeps its gates
SOURCE = SCRIPT.read_text()


def test_the_no_suite_early_exit_is_gone_and_build_only_is_the_only_skip():
    assert "no suite requested" not in SOURCE
    assert len(re.findall(r'^\[ "\$BUILD_ONLY" = 0 \] \|\| \{ echo "built', SOURCE, re.M)) == 1


def test_existing_completeness_gates_are_still_present():
    for needle in (
            'REQUIRED_SUITES=(completion matrix step4 step5 r0 operator migrations ops preparation normal legacy recovery delivery)',
            '"required group omitted"',                    # missing groups
            "INCOMPLETE: skipped required checks",         # required skips
            'die "image is not an exact build of $SHORT"',  # source/tree mismatch
            'die "image revision/tree does not match requested commit"',
            'die "baseline reader differs from requested commit"',
            'die "required image tools are missing"',       # broken prerequisites
            'die "image dependencies are inconsistent"',
            'die "$BUILD already exists; choose a fresh --label',   # stale manifests are never overwritten
            'Step 6 matrix files are not in this commit'):
        assert needle in SOURCE, needle


def test_rollback_image_is_verified_unconditionally_not_only_when_it_carries_a_label():
    assert 'if [ "$NO_BUILD" = 0 ] || [ -n "$(docker image inspect "$IMAGE_ID" --format \'{{index .Config.Labels "io.twin.rollback.revision"}}\')" ]' not in SOURCE
    assert 'die "rollback revision mismatch"' in SOURCE and 'die "rollback code differs from its commit"' in SOURCE


def test_the_browser_gate_takes_an_explicit_dashboard_pair_and_verifies_it_locally():
    assert '"$IMAGE_ID" "$DASHBOARD_IMAGE" "$DASHBOARD_COMMIT"' in SOURCE
    for needle in ("Q14 expected dashboard image is not in the local image store",
                   "Q14 dashboard image revision label differs from DASHBOARD_COMMIT",
                   "Q14 expected dashboard image/commit not supplied"):
        assert needle in SOURCE
    # the old "any receipt with a well-formed dashboard digest passes" call shape must not come back
    assert re.search(r'verify-browser-receipt\.py /browser-receipt\.json "\$IMAGE_ID" >', SOURCE) is None


# ----------------------------------------------------------------------------------------------- ops container contract
def group_block():
    start = SOURCE.index("run_group() {")
    return SOURCE[start:SOURCE.index("for s in", start)]


def test_only_the_ops_group_runs_as_root_with_exactly_the_five_capabilities_its_assertions_need():
    block = group_block()
    caps = re.findall(r'caps=\((.*?)\)', block)
    assert len(caps) == 2 and caps[0] == ""        # default: no added capabilities
    added = set(re.findall(r"--cap-add (\w+)", caps[1]))
    # CHOWN/FOWNER/DAC_OVERRIDE build the root-owned 0600 marker and group-readable resolution; SETUID/SETGID let the
    # test fork into the unprivileged worker identity (setgroups/setgid/setuid) so it can prove that worker's access.
    assert added == {"CHOWN", "FOWNER", "DAC_OVERRIDE", "SETUID", "SETGID"}
    assert 'run_user=1000:1000' in block and '[ "$name" != ops ] ||' in block and 'run_user=0:0' in block
    for forbidden in ("SYS_ADMIN", "NET_ADMIN", "NET_RAW", "SYS_PTRACE", "--privileged", "ALL --cap-add ALL", "MKNOD",
                      "SETPCAP", "SYS_CHROOT", "DAC_READ_SEARCH"):
        assert forbidden not in block
    assert "--cap-drop ALL" in block and "--security-opt no-new-privileges" in block and "--read-only" in block
    assert "--network container:" in block or '--network "container:$RUN_PG"' in block


def test_every_results_row_is_a_tab_separated_record():
    """results.tsv rows must be real TSV (a mangled printf once wrote 'browsertINCOMPLETEt...')."""
    rows = re.findall(r"^\s*(?:\S.*)?printf (\S.*?) (?:browser|\"\$required\"|\"\$s\"|\"\$name\")", SOURCE, re.M)
    assert rows, "no results-row printf found"
    assert all(r == "'%s\\t%s\\t%s\\t%s\\n'" for r in rows), rows


def test_every_docker_format_template_is_quoted_and_labels_are_quoted_keys():
    """An unquoted {{index .Config.Labels org.x}} makes the shell/Go template evaluate a variable, not the label."""
    for line in SOURCE.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for match in re.finditer(r"--format (\S.*?)(?: 2>|\)| >|;|$)", line):
            template = match.group(1)
            assert template[0] in "'\"", line
            if "Labels" in template:
                assert re.search(r'Labels \\?"[\w.]+\\?"', template), line


def test_a_shard_run_can_never_qualify_and_combine_applies_the_unchanged_completeness_rules():
    """--shard exits before the required-group and browser checks and says so; --combine is the only path that can qualify a sharded
    release: every required group (plus the isolated legacy process) must have exactly one passing row on ONE image id, no skips."""
    shard = SOURCE[SOURCE.index('if [ "$SHARD" = 1 ]; then'):]
    assert shard.index('qualification_exit=shard:') < shard.index('for required in "${REQUIRED_SUITES[@]}"; do')
    assert 'exit "$FAILED"' in shard[:shard.index('for required in "${REQUIRED_SUITES[@]}"; do')]
    combine = SOURCE[SOURCE.index('if [ "${#COMBINE_DIRS[@]}" -gt 0 ]; then\n  FAILED=0'):]
    combine = combine[:combine.index('exit "$FAILED"')]
    for needle in ('for required in "${REQUIRED_SUITES[@]}" legacy-isolated; do', '"$IMAGE_ID"', '"$FULL"', 'skipped', 'browser_check', '$n" = 1'):
        assert needle in combine, needle


def test_browser_check_is_defined_before_either_mode_calls_it():
    """The first sharded combine died with 'browser_check: command not found': a shell function must be defined before it runs."""
    definition = SOURCE.index('browser_check() {')
    calls = [m.start() for m in re.finditer(r"^\s*browser_check$", SOURCE, re.M)]
    assert len(calls) == 2 and all(definition < call for call in calls), (definition, calls)


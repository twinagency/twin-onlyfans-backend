"""Whole-file shard partition and the merger's union == collected-manifest proof (no database)."""
import importlib.util
import json
from pathlib import Path
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def load(name, filename):
    import sys
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shards = load("acceptance_shards_under_test", "onlyfans_acceptance_shards.py")
merger = load("acceptance_merge_under_test", "onlyfans-acceptance-merge.py")
MANIFEST = json.loads((SCRIPTS / "onlyfans-release-acceptance-manifest.json").read_text())


@pytest.mark.parametrize("suite", ["combined", "commerce"])
@pytest.mark.parametrize("count", [1, 2, 4, 7])
def test_real_manifest_partition_is_a_deterministic_disjoint_cover(suite, count):
    first = shards.plan(ROOT, MANIFEST[suite], count)
    assert first == shards.plan(ROOT, MANIFEST[suite], count)
    files = [name for shard in first["shards"] for name in shard["files"]]
    scripts = [name for shard in first["shards"] for name in shard["scripts"]]
    assert sorted(files) == first["allFiles"] and len(files) == len(set(files))
    assert sorted(scripts) == sorted(first["allScripts"]) and len(scripts) == len(set(scripts))
    assert first["digest"] != shards.plan(ROOT, MANIFEST[suite], count + 1)["digest"]
    if count > 1 and len(first["allFiles"]) >= count:
        assert all(shard["files"] or shard["scripts"] for shard in first["shards"])


@pytest.mark.parametrize("value", ["", "4", "4/4", "-1/4", "0/0", "a/b", "0/65"])
def test_invalid_shard_specifications_are_refused(value):
    with pytest.raises(ValueError):
        shards.parse_shard(value)
    assert shards.parse_shard("3/4") == (3, 4)


def evidence(tmp_path, count=3, *, tamper=None):
    """Build synthetic manifest + shard evidence for a tiny fake tree."""
    tree = tmp_path / "tree"; (tree / "service/tests").mkdir(parents=True)
    names = [f"service/tests/test_onlyfans_{n}.py" for n in "abcdef"] + ["service/tests/test_onlyfans_read.py"]
    for index, name in enumerate(names):
        (tree / name).write_text("x" * (10 + index * 7))
    manifest = {"patterns": ["service/tests/test_onlyfans*.py"], "separateScripts": ["service/tests/test_onlyfans_read.py"]}
    plan = shards.plan(tree, manifest, count)
    source = {"sha": "a" * 40, "tree": "b" * 40}
    nodes = {name: [f"{name}::test_{i}" for i in range(3)] for name in plan["allFiles"]}
    base = {"schemaVersion": "of-release-acceptance.v1", "suite": "combined", "backend": source,
            "dashboard": {"method": "git", "sha": "c" * 40, "tree": "d" * 40}}
    top = tmp_path / "manifest"; top.mkdir()
    selected = [node for name in plan["allFiles"] for node in nodes[name]]
    (top / "collection.json").write_text(json.dumps({"selected": selected, "deselected": [], "collectionErrors": []}))
    (top / "expanded-manifest.json").write_text(json.dumps({"allPytestFiles": plan["allFiles"], "allSeparateScripts": plan["allScripts"]}))
    (top / "summary.json").write_text(json.dumps({**base, "scope": "manifest", "manifestOnly": True, "accepted": False,
        "dashboard": {"method": "pin", "sha": "c" * 40},
        "shard": {"index": 0, "count": count, "digest": plan["digest"], "files": plan["allFiles"], "scripts": []}}))
    dirs = []
    for index, assignment in enumerate(plan["shards"]):
        directory = tmp_path / f"shard-{index}"; directory.mkdir(); dirs.append(directory)
        chosen = [node for name in assignment["files"] for node in nodes[name]]
        (directory / "collection.json").write_text(json.dumps({"selected": chosen, "deselected": [], "collectionErrors": []}))
        (directory / "execution.json").write_text(json.dumps({"selected": chosen, "deselected": [], "collectionErrors": [],
            "outcomes": {node: "passed" for node in chosen}}))
        (directory / "expanded-manifest.json").write_text(json.dumps({"pytestFiles": assignment["files"], "separateScripts": assignment["scripts"]}))
        (directory / "summary.json").write_text(json.dumps({**base, "scope": "shard", "accepted": True,
            "shard": {"index": index, "count": count, "digest": plan["digest"], **assignment},
            "separateScripts": [{"path": name, "passed": True, "exitStatus": 0} for name in assignment["scripts"]]}))
    if tamper:
        tamper(top, dirs)
    return top, dirs


def edit(path, change):
    value = json.loads(path.read_text()); change(value); path.write_text(json.dumps(value))


def test_merger_accepts_a_complete_disjoint_cover_and_derives_the_count(tmp_path):
    top, dirs = evidence(tmp_path)
    result = merger.merge(top, dirs, backend_sha="a" * 40, dashboard_sha="c" * 40, suite="combined")
    assert result["accepted"] and result["collectedNodes"] == result["executedNodes"] == 18
    assert result["pytestFiles"] == 6 and result["separateScripts"] == 1 and result["shards"] == 3


def drop_node(top, dirs):
    target = next(d for d in dirs if json.loads((d / "collection.json").read_text())["selected"])
    for name in ("collection.json", "execution.json"):
        edit(target / name, lambda v: v["selected"].pop())
    edit(target / "execution.json", lambda v: v["outcomes"].pop(next(iter(v["outcomes"]))))


def duplicate_node(top, dirs):
    first, second = [d for d in dirs if json.loads((d / "collection.json").read_text())["selected"]][:2]
    extra = json.loads((first / "collection.json").read_text())["selected"][0]
    for name in ("collection.json", "execution.json"):
        edit(second / name, lambda v: v["selected"].append(extra))
    edit(second / "execution.json", lambda v: v["outcomes"].update({extra: "passed"}))


def skipped_node(top, dirs):
    target = next(d for d in dirs if json.loads((d / "execution.json").read_text())["outcomes"])
    edit(target / "execution.json", lambda v: v["outcomes"].update({next(iter(v["outcomes"])): "skipped"}))


def missing_shard(top, dirs):
    shutil.rmtree(dirs.pop())


def other_revision(top, dirs):
    edit(dirs[0] / "summary.json", lambda v: v["backend"].update(sha="e" * 40))


def other_partition(top, dirs):
    edit(dirs[1] / "summary.json", lambda v: v["shard"].update(digest="0" * 64))


def script_not_run(top, dirs):
    target = next(d for d in dirs if json.loads((d / "summary.json").read_text())["separateScripts"])
    edit(target / "summary.json", lambda v: v.update(separateScripts=[]))


def script_failed(top, dirs):
    target = next(d for d in dirs if json.loads((d / "summary.json").read_text())["separateScripts"])
    edit(target / "summary.json", lambda v: v["separateScripts"][0].update(passed=False))


def shard_not_accepted(top, dirs):
    edit(dirs[0] / "summary.json", lambda v: v.update(accepted=False))


def overlapping_files(top, dirs):
    donor = next(d for d in dirs if json.loads((d / "summary.json").read_text())["shard"]["files"])
    name = json.loads((donor / "summary.json").read_text())["shard"]["files"][0]
    other = next(d for d in dirs if d != donor)
    edit(other / "summary.json", lambda v: v["shard"]["files"].append(name))
    edit(other / "expanded-manifest.json", lambda v: v["pytestFiles"].append(name))


def unselected_outcome(top, dirs):
    edit(dirs[0] / "execution.json", lambda v: v["outcomes"].update({"service/tests/x.py::ghost": "passed"}))


@pytest.mark.parametrize("tamper", [drop_node, duplicate_node, skipped_node, missing_shard, other_revision, other_partition,
    script_not_run, script_failed, shard_not_accepted, overlapping_files, unselected_outcome])
def test_merger_refuses_any_gap_duplicate_skip_or_mixed_revision(tmp_path, tamper):
    top, dirs = evidence(tmp_path, tamper=tamper)
    with pytest.raises(ValueError):
        merger.merge(top, dirs, backend_sha="a" * 40, dashboard_sha="c" * 40, suite="combined")


def test_merger_refuses_a_manifest_from_another_revision(tmp_path):
    top, dirs = evidence(tmp_path)
    with pytest.raises(ValueError, match="different revision"):
        merger.merge(top, dirs, backend_sha="f" * 40)


def test_merger_refuses_shard_directories_in_place_of_the_manifest(tmp_path):
    top, dirs = evidence(tmp_path)
    with pytest.raises(ValueError, match="manifest-only"):
        merger.merge(dirs[0], dirs)

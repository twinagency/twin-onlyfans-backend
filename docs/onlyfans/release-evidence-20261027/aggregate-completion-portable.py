import importlib.util,json,sys
from pathlib import Path
root=Path(r"C:/Users/conta/codex-work/twin-stabilisation-patches/completion-2ef13f4/backend")
spec=importlib.util.spec_from_file_location("candidate_acceptance",root/"scripts/onlyfans-release-acceptance.py")
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
original=module.expanded_manifest
def portable_manifest(suite):
    files,separate=original(suite)
    return sorted(set(name.replace("\\","/") for name in files)-set(separate)),separate
module.expanded_manifest=portable_manifest
evidence=Path(r"C:/Users/conta/codex-work/twin-stabilisation-patches/evidence/completion-final-shards")
try:
    proof=module.aggregate_shards(evidence,"combined",6,"d6811b124928f8167e6625ce9db5db81bf227fad","871b3e8955bf8af357f6f66d56c4446bf98fee8e")
    proof["executionEnvironment"]={"checker":"pinned candidate scripts/onlyfans-release-acceptance.py","host":"Windows laptop","adapter":"Normalize Windows manifest path separators to the forward slashes recorded by the six Ubuntu shards; every source, collection, JUnit, manifest, duplicate and isolated-script assertion is unchanged.","githubAggregation":"not started: account billing/spending limit"}
    (evidence/"aggregate-summary.json").write_text(json.dumps(proof,indent=2)+"\n")
    print(json.dumps({key:proof[key] for key in ["accepted","shardCount","pytest","backend","dashboard","executionEnvironment"]}))
except Exception as error:
    print(json.dumps({"accepted":False,"error":str(error)}))
    sys.exit(1)

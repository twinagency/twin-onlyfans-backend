"""Use the immutable Docker artifact in the existing populated rollback proof.

The existing assertions and database fixtures are unchanged. Only the legacy
API process launcher changes from a source interpreter to the retained image.
"""
from contextlib import contextmanager
import json
import os
import re
import subprocess
import uuid

def pytest_collection_modifyitems(items):
    image=os.environ["ONLYFANS_ROLLBACK_IMAGE"]
    if not re.fullmatch(r"sha256:[a-f0-9]{64}",image):
        raise RuntimeError("an immutable rollback image ID is required")
    inspected=json.loads(subprocess.check_output(["docker","image","inspect",image],text=True))[0]
    if inspected["Config"]["Labels"]["org.opencontainers.image.revision"]!="d6811b124928f8167e6625ce9db5db81bf227fad":
        raise RuntimeError("rollback source label mismatch")
    matched=False
    for item in items:
        if item.name!="test_schema_compatible_rollback_preserves_evidence_with_commands_closed":
            continue
        module=item.module
        original=module.process
        @contextmanager
        def image_process(args,*,cwd,env,log):
            assert args[1:3]==["-m","uvicorn"] and args[3]=="onlyfans_app:app"
            assert env["ONLYFANS_PROCESS_ROLE"]=="api"
            assert env["ONLYFANS_API_TOKEN"]=="synthetic-rollback-read-token"
            assert not any(env.get(k) for k in ("ONLYFANS_CONTROL_TOKEN","ONLYFANS_CONFIGURATION_TOKEN","ONLYFANS_CONNECTION_KEY"))
            name="astra-rollback-"+uuid.uuid4().hex
            child_env=dict(env,PYTHONPATH="/app/service:/app")
            command=["docker","run","--rm","--name",name,"--network","host"]
            for key in ("ONLYFANS_PROCESS_ROLE","DATABASE_URL","ONLYFANS_API_TOKEN","ONLYFANS_API_TENANT",
                        "PYTHONPATH","PYTHON_DOTENV_DISABLED","PYTHONDONTWRITEBYTECODE"):
                command.extend(["-e",key+"="+child_env[key]])
            command.extend([image,"python",*args[1:]])
            try:
                with original(command,cwd=cwd,env=env,log=log) as child:
                    yield child
            finally:
                subprocess.run(["docker","rm","-f",name],capture_output=True,timeout=30)
        module.process=image_process
        matched=True
    if not matched:
        raise RuntimeError("required populated rollback proof was not collected")

import os
import tempfile
from pathlib import Path
import incident_state as state
import test_incident_state as existing

def test_preexisting_root_only_marker_becomes_readable_to_worker_after_resolution():
    case=existing.IncidentTests()
    case.setUp()
    try:
        os.chmod(case.root,0o750)
        os.chown(case.root,0,65532)
        os.chmod(case.marker,0o600)
        os.chown(case.marker,0,0)
        assert state.resolve(case.root,case.receipt,case.audit.append)=='resolved'
        assert not state.unresolved(case.marker)
        pid=os.fork()
        if pid==0:
            os.setgroups([])
            os.setgid(65532)
            os.setuid(65532)
            os._exit(1 if state.unresolved(case.marker) else 0)
        _,status=os.waitpid(pid,0)
        assert os.waitstatus_to_exitcode(status)==0, 'worker still held: original 0600 marker cannot be read'
    finally:
        case.tearDown()

def test_new_failure_before_resolution_invalidates_prior_incident_evidence():
    case=existing.IncidentTests()
    case.setUp()
    try:
        original_hash=state.digest(case.marker.read_bytes())
        state.record_failure(case.root,{'failure':'new_material_failure_after_collection'})
        try:
            state.resolve(case.root,case.receipt,case.audit.append)
        except RuntimeError:
            pass
        assert state.unresolved(case.marker), 'new failure arriving before resolver lock was absorbed and old evidence released the hold'
    finally:
        case.tearDown()

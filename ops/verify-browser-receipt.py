"""Validate browser qualification against the exact backend image AND the explicitly expected dashboard artifact.

usage: verify-browser-receipt.py RECEIPT BACKEND_IMAGE DASHBOARD_IMAGE DASHBOARD_COMMIT

The expected dashboard image digest and commit are inputs the caller has verified independently (build-candidate.sh
checks them against the local image store); this script never accepts "any well-formed digest" from the receipt.
"""
import json,re,sys
from pathlib import Path
DIGEST=re.compile(r"sha256:[0-9a-f]{64}")
COMMIT=re.compile(r"[0-9a-f]{40}")
def _count(value,expected):
 return type(value) is int and value==expected    # True == 1 in Python; a boolean is not a count
def verify(path,image,dashboard_image,dashboard_commit):
 if not all(DIGEST.fullmatch(str(v)) for v in (image,dashboard_image)) or not COMMIT.fullmatch(str(dashboard_commit)):
  raise ValueError("expected immutable backend image, dashboard image and dashboard commit required")
 if image==dashboard_image:
  raise ValueError("backend and dashboard must be distinct artifacts")
 value=json.loads(Path(path).read_text())
 if type(value) is not dict:
  raise ValueError("incomplete or mismatched browser evidence")
 heads=value.get("heads")
 commits={c for c in (value.get("dashboardCommit"),heads.get("dashboard") if type(heads) is dict else None) if c is not None}
 if (value.get("status")!="passed" or value.get("sourceOverlay") is not False
     or value.get("backendImage")!=image or value.get("dashboardImage")!=dashboard_image
     or commits!={dashboard_commit}
     or not _count(value.get("posts"),4) or not _count(value.get("paidCards"),1) or not _count(value.get("access"),1)
     or value.get("journey")!="browser/BFF/backend/Postgres/autonomous worker"):
  raise ValueError("incomplete or mismatched browser evidence")
 return value
if __name__=="__main__":
 try:
  if len(sys.argv)!=5:
   raise ValueError("usage: RECEIPT BACKEND_IMAGE DASHBOARD_IMAGE DASHBOARD_COMMIT")
  verify(*sys.argv[1:])
 except Exception as exc:
  print("Browser qualification rejected:",type(exc).__name__)
  raise SystemExit(1)
 print("Browser receipt matches exact backend/dashboard pair and completed normal journey")

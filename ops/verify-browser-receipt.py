"""Validate browser qualification against this exact backend image."""
import json,re,sys
from pathlib import Path
def verify(path,image):
 value=json.loads(Path(path).read_text())
 if (type(value) is not dict or value.get("status")!="passed" or value.get("sourceOverlay") is not False
     or value.get("backendImage")!=image or not re.fullmatch(r"sha256:[0-9a-f]{64}",str(value.get("dashboardImage","")))
     or value.get("posts")!=4 or value.get("paidCards")!=1 or value.get("access")!=1
     or value.get("journey")!="browser/BFF/backend/Postgres/autonomous worker"):
  raise ValueError("incomplete or mismatched browser evidence")
 return value
if __name__=="__main__":
 try:
  verify(sys.argv[1],sys.argv[2])
 except Exception as exc:
  print("Browser qualification rejected:",type(exc).__name__)
  raise SystemExit(1)
 print("Browser receipt matches exact backend and completed normal journey")

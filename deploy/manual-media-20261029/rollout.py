"""Roll out reviewed manual-media overlays while preserving live container settings."""
import ast, copy, fcntl, http.client, json, os, pathlib, socket, subprocess, sys, time, urllib.request
ROOT=pathlib.Path("/var/lib/twin-release/inbox-media-repair-20261029")
COMPOSE=pathlib.Path("/etc/twin-onlyfans/media-agency-release-compose.json")
SOURCE="eb29e5c0f2a7d43a96692231f7c89a3b17ae91dd"
class UnixHTTP(http.client.HTTPConnection):
 def connect(self):
  self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.settimeout(60);self.sock.connect("/var/run/docker.sock")
def private(path,data):
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
 with os.fdopen(fd,"w") as f:f.write(data)
def docker(method,path,data=None,missing=False):
 c=UnixHTTP("localhost")
 try:
  c.request(method,path,None if data is None else json.dumps(data).encode(),{"Content-Type":"application/json"})
  r=c.getresponse();body=r.read()
  if missing and r.status==404:return None
  if not (200<=r.status<300 or r.status==304):
   private(ROOT/"docker-error.private",body.decode(errors="replace"));raise RuntimeError("Docker operation failed; private diagnostic retained")
  return json.loads(body) if body else None
 finally:c.close()
def inspect(name):return docker("GET","/containers/"+name+"/json")
def networks(old):
 return {n:{"Aliases":[a for a in p.get("Aliases") or [] if a not in {old["Id"],old["Id"][:12]}]} for n,p in old["NetworkSettings"]["Networks"].items()}
def health():
 for _ in range(45):
  try:
   with urllib.request.urlopen("http://127.0.0.1:8788/healthz",timeout=2) as r:
    if r.status==200:return
  except Exception:pass
  time.sleep(1)
 raise RuntimeError("API health check did not pass")
def db(code):
 return subprocess.check_output(["docker","exec","twin-onlyfans-api-1","python","-c",code],text=True,stderr=subprocess.PIPE)
READ="""import os,json
from sqlalchemy import create_engine,text
e=create_engine(os.environ['DATABASE_URL'])
with e.connect() as c:
 print(json.dumps({'version':c.scalar(text('SELECT version_num FROM alembic_version')),'active':c.scalar(text("SELECT count(*) FROM of_text_intent WHERE status='dispatching'"))}))
"""
def replace(old,name,image):
 current=docker("GET","/containers/"+name+"/json",missing=True)
 if current:
  docker("POST","/containers/"+current["Id"]+"/stop?t=30")
  docker("POST","/containers/"+current["Id"]+"/rename?name="+name+".media-archive-"+current["Id"][:10])
  for n in networks(current):docker("POST","/networks/"+n+"/disconnect",{"Container":current["Id"],"Force":True})
 config=copy.deepcopy(old["Config"]);config["Image"]=image
 config["Labels"]={**config.get("Labels",{}),"io.twin.manual_media":SOURCE}
 config["HostConfig"]=copy.deepcopy(old["HostConfig"])
 config["NetworkingConfig"]={"EndpointsConfig":networks(old)}
 identity=docker("POST","/containers/create?name="+name,config)["Id"]
 docker("POST","/containers/"+identity+"/start")
 return identity
def save_compose(original,images):
 value=copy.deepcopy(original)
 for service,role in {"api":"api","worker":"worker","fastt-supervisor":"sender","profile-learner":"api","migrate":"api","fastt-runner":"sender"}.items():
  if service in value["services"]:value["services"][service]["image"]=images[role]
 private(COMPOSE.with_suffix(".manual-media.tmp"),json.dumps(value,indent=2))
 os.replace(COMPOSE.with_suffix(".manual-media.tmp"),COMPOSE)
def main(apply=False):
 locks=[]
 for p in [ROOT/"cutover.lock",pathlib.Path("/var/lib/twin-release/profiles-memory-20261029/cutover.lock"),pathlib.Path("/var/lib/twin-release/agency-crm-20261029/cutover.lock"),pathlib.Path("/var/lib/twin-release/astra-compatible-20261028/cutover.lock")]:
  handle=p.open("a");fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB);locks.append(handle)
 candidates=json.loads((ROOT/"candidates.json").read_text())
 roles={x["name"]:role for role,x in candidates.items()}
 roles["twin-onlyfans-profile-learner"]="api"
 baseline={name:inspect(name) for name in roles}
 for name,x in baseline.items():
  assert x["Image"]==candidates[roles[name]]["baseImage"] and x["State"]["Running"],name+" changed"
 for role,x in candidates.items():
  assert docker("GET","/images/"+x["candidate"]+"/json")["Id"]==x["candidate"]
  assert docker("GET","/images/"+x["compatible"]+"/json")["Id"]==x["compatible"]
 compose=json.loads(COMPOSE.read_text())
 for name,x in baseline.items():
  service=x["Config"]["Labels"]["com.docker.compose.service"]
  assert compose["services"][service]["image"]==x["Image"],"Compose changed"
 state=json.loads(db(READ));assert state["version"]=="0099_fan_identity_sync" and state["active"]==0,state
 print("Preflight passed: exact running images, preserved settings, no active manual delivery.",flush=True)
 if not apply:return
 private(ROOT/"baseline.private.json",json.dumps(baseline))
 private(ROOT/"compose-before.private.json",json.dumps(compose))
 migrated=False;stopped=[];created={}
 try:
  for name in roles:
   if name!="twin-onlyfans-api-1":
    docker("POST","/containers/"+baseline[name]["Id"]+"/stop?t=45");stopped.append(name)
  state=json.loads(db(READ));assert state["active"]==0,"Delivery still active"
  path=ROOT/"image-api/service/db/migrations/versions/20261029_0300_manual_free_media.py"
  tree=ast.parse(path.read_text())
  fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=="upgrade")
  statements=[ast.literal_eval(n.value.args[0]) for n in fn.body if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call)]
  assert len(statements)==2
  code="""import os,json
from sqlalchemy import create_engine,text
e=create_engine(os.environ['DATABASE_URL'])
with e.begin() as c:
 c.exec_driver_sql("SET LOCAL lock_timeout='5s'")
 c.exec_driver_sql("SET LOCAL statement_timeout='30s'")
 assert c.scalar(text('SELECT version_num FROM alembic_version'))=='0099_fan_identity_sync'
 for sql in STATEMENTS:c.exec_driver_sql(sql)
 assert c.execute(text("UPDATE alembic_version SET version_num='0100_manual_free_media' WHERE version_num='0099_fan_identity_sync'")).rowcount==1
print('Manual free-media migration committed')
""".replace("STATEMENTS",repr(statements))
  print(db(code).strip(),flush=True);migrated=True
  for name in ["twin-onlyfans-api-1","twin-onlyfans-worker-1","twin-onlyfans-fastt-supervisor-1","twin-onlyfans-profile-learner"]:
   created[name]=replace(baseline[name],name,candidates[roles[name]]["candidate"])
   if name=="twin-onlyfans-api-1":health()
  time.sleep(4)
  for name,identity in created.items():
   x=inspect(name);assert x["Id"]==identity and x["State"]["Running"] and not x["State"]["Restarting"],name+" unhealthy"
  assert json.loads(db(READ))["version"]=="0100_manual_free_media"
  print(db("import os;from sqlalchemy import create_engine;from db.schema import check_schema;e=create_engine(os.environ['DATABASE_URL']);c=e.connect();check_schema(c);print('Live schema verified');c.close()").strip(),flush=True)
  save_compose(compose,{r:x["candidate"] for r,x in candidates.items()})
  report={"source":SOURCE,"schema":"0100_manual_free_media","containers":{n:{"image":inspect(n)["Image"],"running":True} for n in roles},"apiHealth":True}
  (ROOT/"deployed.json").write_text(json.dumps(report,indent=2))
  print(json.dumps(report),flush=True)
 except Exception:
  if migrated:
   for name in roles:replace(baseline[name],name,candidates[roles[name]]["compatible"])
   save_compose(compose,{r:x["compatible"] for r,x in candidates.items()});health()
   print("Compatible rollback restored. Migration retained; no receipt data removed.",flush=True)
  else:
   for name in stopped:docker("POST","/containers/"+baseline[name]["Id"]+"/start")
  raise
if __name__=="__main__":main("--apply" in sys.argv)

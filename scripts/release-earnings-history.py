"""Apply the reviewed earnings patch over each observed runtime image.

Prepare only builds candidates and records hashes. Execute checks the same
containers/configuration, backs up Postgres, applies the additive migration,
and recreates only the API, worker and existing FASTT supervisor. It retains
each image's unrelated code, environment, mounts, scopes and command.
"""
import argparse,copy,fcntl,hashlib,json,os,shutil,subprocess,time
from pathlib import Path

ROOT=Path("/var/tmp/of-ledger-history-20261026/release")
NAMES={"api":"twin-onlyfans-api-1","worker":"twin-onlyfans-worker-1",
       "supervisor":"twin-onlyfans-fastt-supervisor-1"}
COMMON=["service/db/schema.py","service/db/onlyfans_ledger_models.py",
 "service/db/migrations/versions/20261026_0100_ledger_history.py",
 "service/db/snapshots/0092_ledger_history.sql"]
ROLE={"api":["service/onlyfans_read/api.py","service/onlyfans_read/earnings.py"],
 "worker":["service/onlyfans_runtime/ledger.py","service/onlyfans_runtime/provider_connector.py",
 "service/onlyfans_runtime/ledger_worker.py","service/onlyfans_worker.py"],"supervisor":[]}

def call(args,*,data=None,timeout=180):
    r=subprocess.run(args,input=data,text=True,capture_output=True,timeout=timeout)
    if r.returncode:
        raise RuntimeError("Command failed: "+args[0]+" "+args[1]+"; private output retained by caller")
    return r.stdout.strip()

def inspect(name):return json.loads(call(["docker","inspect",name]))[0]
def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def private(path,text):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,"w") as f:f.write(text)
def snapshot():
    code='''import os,json,hashlib
from sqlalchemy import create_engine,text
e=create_engine(os.environ["DATABASE_URL"])
with e.connect() as c:
 c.execute(text("SET TRANSACTION READ ONLY"))
 c.execute(text("SET LOCAL statement_timeout='10000ms'"))
 tables=["transactions","of_ledger_evidence","of_creator_bindings","of_runtime","of_delivery_policy"]
 counts={t:c.execute(text("select count(*) from "+t)).scalar() for t in tables}
 money=c.execute(text("select count(*),coalesce(sum(amount_cents),0),coalesce(sum(net_cents),0) from transactions")).one()
 authority={}
 for table,fields in [("of_creator_bindings","id,tenant_id,creator_id,account_id,revoked_at"),("of_runtime","account_id,binding_id,enabled"),("of_delivery_policy","account_id,enabled")]:
  authority[table]=[dict(r) for r in c.execute(text("select "+fields+" from "+table+" order by 1")).mappings()]
 print(json.dumps({"schema":c.execute(text("select version_num from alembic_version")).scalar(),"counts":counts,"money":list(money),"authorityHash":hashlib.sha256(json.dumps(authority,sort_keys=True,default=str).encode()).hexdigest()}))
e.dispose()
'''
    return json.loads(call(["docker","exec","-i","-w","/app/service",NAMES["api"],"python","-"],data=code))

def prepare(revision):
    ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
    plan={"revision":revision,"containers":{},"configs":{}}
    for role,name in NAMES.items():
        item=inspect(name)
        if not item["State"]["Running"]:raise RuntimeError("Observed service is not running")
        cfg=item["Config"]["Labels"].get("com.docker.compose.project.config_files")
        if not cfg or "," in cfg:raise RuntimeError("Unexpected compose owner")
        if cfg not in plan["configs"]:
            raw=Path(cfg).read_text()
            plan["configs"][cfg]={"hash":digest(cfg),"before":raw,"after":json.loads(raw)}
        context=ROOT/role
        if context.exists():shutil.rmtree(context)
        context.mkdir()
        for file in COMMON+ROLE[role]:
            target=context/file;target.parent.mkdir(parents=True,exist_ok=True)
            # New files are supplied by /dev/null additions in the exact patch.
            r=subprocess.run(["docker","cp",name+":/app/"+file,str(target)],capture_output=True)
            if r.returncode and file not in {
              "service/db/migrations/versions/20261026_0100_ledger_history.py",
              "service/db/snapshots/0092_ledger_history.sql",
              "service/onlyfans_runtime/ledger_worker.py"}:
                raise RuntimeError("Runtime source missing")
        for file in COMMON+ROLE[role]:
            target=context/file
            if target.exists():target.write_bytes(target.read_bytes().replace(b"\r\n",b"\n"))
        for patch in ["common.patch"]+([role+".patch"] if ROLE[role] else []):
            call(["patch","--batch","--forward","--fuzz=0","-p1","-d",str(context),"-i",str(ROOT/patch)])
        if role=="worker" and "_process_import_sync" not in (context/"service/onlyfans_worker.py").read_text():
            raise RuntimeError("Automatic import behavior was not retained")
        tag="twin-onlyfans:earnings-history-"+revision[:7]+"-"+role
        (context/"Dockerfile").write_text("FROM "+item["Image"]+"\nCOPY service/ /app/service/\nLABEL com.twin.earnings-revision="+revision+"\n")
        call(["docker","build","-q","-t",tag,str(context)],timeout=300)
        image=call(["docker","image","inspect",tag,"--format","{{.Id}}"])
        code="import py_compile; files="+repr(COMMON[:2]+ROLE[role])+"; [py_compile.compile('/app/'+p,doraise=True) for p in files]; print('compiled')"
        call(["docker","run","--rm","--network","none","--entrypoint","python",image,"-c",code])
        service=item["Config"]["Labels"]["com.docker.compose.service"]
        plan["configs"][cfg]["after"]["services"][service]["image"]=image
        if "migrate" in plan["configs"][cfg]["after"]["services"]:
            plan["configs"][cfg]["after"]["services"]["migrate"]["image"]=image
        plan["containers"][role]={"name":name,"id":item["Id"],"image":item["Image"],"candidate":image,
            "tag":tag,"config":cfg,"service":service}
    plan["before"]=snapshot()
    if plan["before"]["schema"]!="0091_launch_controls":raise RuntimeError("Unexpected schema")
    private(ROOT/"plan.json",json.dumps(plan))
    print(json.dumps({"state":"prepared","schemaBefore":plan["before"]["schema"],
        "schemaAfter":"0092_ledger_history","services":list(NAMES),"revision":revision}))

def execute():
    lock=open("/var/lock/twin-onlyfans-native-release.lock","a")
    fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    plan=json.loads((ROOT/"plan.json").read_text())
    for role,state in plan["containers"].items():
        current=inspect(state["name"])
        if current["Id"]!=state["id"] or current["Image"]!=state["image"] or not current["State"]["Running"]:
            raise RuntimeError("Runtime changed after prepare")
    for cfg,state in plan["configs"].items():
        if digest(cfg)!=state["hash"]:raise RuntimeError("Compose changed after prepare")
    # A consistent logical backup stays private on this host.
    backup=Path("/var/backups/twin-onlyfans/earnings-history-"+plan["revision"][:7])
    backup.mkdir(mode=0o700,parents=True,exist_ok=False)
    dump=backup/"before.dump"
    fd=os.open(dump,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"wb") as stream:
        r=subprocess.run(["docker","exec","twin-onlyfans-database-1","pg_dump","-U","twin_onlyfans","-d","twin_onlyfans","-Fc"],stdout=stream,stderr=subprocess.PIPE,timeout=300)
    if r.returncode or dump.stat().st_size<1000:raise RuntimeError("Database backup failed")
    with dump.open("rb") as stream:
        r=subprocess.run(["docker","exec","-i","twin-onlyfans-database-1","pg_restore","--list"],stdin=stream,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=120)
    if r.returncode:raise RuntimeError("Backup catalog verification failed")
    before=snapshot()
    private(backup/"proof.json",json.dumps({"sha256":digest(dump),"bytes":dump.stat().st_size,"snapshot":before}))
    api=inspect(NAMES["api"])
    environment=api["Config"]["Env"]
    if any("\n" in v or "\r" in v for v in environment):raise RuntimeError("Unsupported environment serialization")
    private(ROOT/"migrate.env","\n".join(environment)+"\n")
    # Graceful stops retain existing durable invocation/dispatch markers.
    for role in ("supervisor","worker"):
        call(["docker","stop","-t","90",NAMES[role]],timeout=120)
    before=snapshot()
    # API remains running for before/after read-only checks; additive DDL leaves
    # its existing readers valid until it is recreated immediately below.
    migration=call(["docker","run","--rm","--network",api["HostConfig"]["NetworkMode"],
        "--env-file",str(ROOT/"migrate.env"),"-w","/app/service","--entrypoint","python",
        plan["containers"]["api"]["candidate"],"-m","db.schema","upgrade","--url-env","DATABASE_URL"],timeout=120)
    after=snapshot()
    if after["schema"]!="0092_ledger_history" or any(after[k]!=before[k] for k in ("counts","money","authorityHash")):
        raise RuntimeError("Post-migration financial/scope verification failed; no old binary restart")
    for cfg,state in plan["configs"].items():
        private(backup/(Path(cfg).parent.name+"-"+Path(cfg).name),state["before"])
        private(cfg,json.dumps(state["after"]))
    for role in ("api","worker","supervisor"):
        state=plan["containers"][role]
        args=["docker","compose","-p","twin-onlyfans","-f",state["config"]]
        if role=="supervisor":args+=["--profile","fastt-supervisor"]
        call(args+["up","-d","--no-deps","--no-build","--pull","never",state["service"]],timeout=180)
        item=inspect(state["name"])
        if not item["State"]["Running"] or item["Image"]!=state["candidate"]:
            raise RuntimeError("Candidate failed to start")
    os.unlink(ROOT/"migrate.env")
    private(ROOT/"deployed.json",json.dumps({"revision":plan["revision"],"backupSha256":digest(dump),
        "schema":"0092_ledger_history","after":after,"services":{r:inspect(n)["Image"] for r,n in NAMES.items()}}))
    print(json.dumps({"state":"deployed","revision":plan["revision"],"schema":"0092_ledger_history",
        "financialRecordsPreserved":True,"scopePreserved":True,"services":list(NAMES)}))

if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("mode",choices=["prepare","execute"]);p.add_argument("--revision")
    a=p.parse_args()
    prepare(a.revision) if a.mode=="prepare" else execute()

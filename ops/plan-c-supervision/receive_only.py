"""Existing isolated receiver entrypoint: durable intake, never model/send construction."""
import os
from sqlalchemy.orm import Session
from model_transport.pilot_supervised import IntakeOnlyPilot, _NAME, _secret
from conversation_engine.pilot_scope import require
from onlyfans_runtime.store import Store, Refused
from plan_c_worker import create_app


def main():
    env=os.environ
    tenant,account=env.get('TWIN_PLAN_C_TENANT',''),env.get('TWIN_PLAN_C_ACCOUNT','')
    if (env.get('ONLYFANS_PROCESS_ROLE')!='worker' or env.get('TWIN_PLAN_C_SUPERVISED')!='1'
            or not env.get('DATABASE_URL') or not _NAME.fullmatch(tenant) or not _NAME.fullmatch(account)):
        raise Refused('explicit isolated receiver worker, database, tenant and account required')
    value=env.get('TWIN_PLAN_C_LISTEN_PORT','55462')
    if not value.isascii() or not value.isdecimal() or not 1024<=int(value)<=65535:
        raise Refused('invalid receiver loopback port')
    store=Store(env['DATABASE_URL'])
    try:
        with Session(store.engine) as s,s.begin():
            runtime=store._runtime(s,tenant,account)
            require(s,runtime)
        pilot=IntakeOnlyPilot(store,tenant,account,_secret(env.get('TWIN_PLAN_C_WEBHOOK_SECRET_FILE','')))
        import uvicorn
        uvicorn.run(create_app(pilot),host='127.0.0.1',port=int(value),access_log=False)
    finally:
        store.close()


if __name__=='__main__':main()

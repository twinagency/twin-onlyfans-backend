"""Private proxy broker. Explicit schema init; no account activation or provider writes."""
import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import threading
import time

from .provider import IPRoyal, ProxyError
from .store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--init-schema",action="store_true")
    args = parser.parse_args()
    token = os.environ.get("ONLYFANS_MANAGED_PROXY_TOKEN", "")
    tenant = os.environ.get("ONLYFANS_MANAGED_PROXY_TENANT", "")
    if len(token)<32 or not re.fullmatch(r"[A-Za-z0-9._:-]{1,64}",tenant) or not os.environ.get("IPROYAL_API_TOKEN"):
        raise SystemExit("Proxy service configuration is incomplete")
    store = Store(os.environ["ONLYFANS_MANAGED_PROXY_DATABASE_URL"],os.environ["ONLYFANS_CONNECTION_KEY"])
    if args.init_schema:
        store.initialize();store.close();print("Proxy assignment schema ready");return
    store.check()
    provider = IPRoyal(os.environ["IPROYAL_API_TOKEN"])
    status = {"lastSync":0.0,"error":"inventory_checking"}

    def sync_loop():
        while True:
            try:
                candidates=provider.inventory()
                store.sync(candidates)
                status.update(lastSync=time.monotonic(),error=None)
            except ProxyError as error:
                status["error"]=str(error)
            except Exception:
                status["error"]="inventory_unavailable"
            time.sleep(60)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def reply(self,code,body):
            raw=json.dumps(body,separators=(",",":")).encode()
            self.send_response(code)
            self.send_header("Content-Type","application/json")
            self.send_header("Cache-Control","private, no-store")
            self.send_header("Content-Length",str(len(raw)))
            self.end_headers();self.wfile.write(raw)

        def authorized(self):
            return hmac.compare_digest(self.headers.get("Authorization","").encode(),("Bearer "+token).encode())

        def ready(self):
            return status["lastSync"]>0 and time.monotonic()-status["lastSync"]<300 and status["error"] is None

        def do_GET(self):
            if self.path=="/health":
                return self.reply(200,{"ok":True,"inventoryReady":self.ready()})
            if not self.authorized():
                return self.reply(401,{"ok":False,"code":"unauthorized"})
            if self.path!="/v1/stock":
                return self.reply(404,{"ok":False,"code":"not_found"})
            if not self.ready():
                return self.reply(503,{"ok":False,"code":status["error"] or "inventory_stale"})
            try:
                return self.reply(200,{"ok":True,"provider":"iproyal","type":"isp_dedicated","countries":store.stock()})
            except Exception:
                return self.reply(503,{"ok":False,"code":"inventory_unavailable"})

        def do_POST(self):
            if not self.authorized():
                return self.reply(401,{"ok":False,"code":"unauthorized"})
            if self.path!="/v1/assign":
                return self.reply(404,{"ok":False,"code":"not_found"})
            if not self.ready():
                return self.reply(503,{"ok":False,"code":status["error"] or "inventory_stale"})
            try:
                size=int(self.headers.get("Content-Length","0"))
                if not 0<size<=4096 or self.headers.get("Content-Type","").split(";")[0]!="application/json":
                    raise ValueError()
                body=json.loads(self.rfile.read(size))
                if not isinstance(body,dict) or set(body)!={"creatorId","handle","countryCode","actor"}:
                    raise ValueError()
                if any(not isinstance(value,str) for value in body.values()):
                    raise ValueError()
                url=store.assign(tenant,body["creatorId"],body["handle"],body["countryCode"],body["actor"])
                # Private server-to-server response. The dashboard never forwards this secret.
                return self.reply(200,{"ok":True,"proxyUrl":url})
            except (ValueError,TypeError,UnicodeError):
                return self.reply(400,{"ok":False,"code":"invalid_assignment"})
            except ProxyError as error:
                return self.reply(409,{"ok":False,"code":str(error)})
            except Exception:
                return self.reply(503,{"ok":False,"code":"assignment_unavailable"})

    threading.Thread(target=sync_loop,daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0",8791),Handler).serve_forever()


if __name__=="__main__":
    main()

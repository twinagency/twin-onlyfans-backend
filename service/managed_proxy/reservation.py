from urllib.parse import urlsplit
from sqlalchemy import text
from .provider import address_hash,ProxyError
from .store import open_proxy,owner_key,canonical
import re

def reserve_network(session,tenant,creator,proxy_url,key,actor,*,required=False,captured_handle=None):
    """Same inventory-row first-use fence for durable credential writers."""
    if not session.scalar(text("SELECT to_regclass('onlyfans_proxy.inventory')")):
        if required:raise ProxyError("reservation_unavailable")
        return False
    try:digest=address_hash(urlsplit(proxy_url).hostname)
    except Exception:
        if required:raise ProxyError("reservation_unavailable") from None
        return False
    row=session.execute(text("SELECT * FROM onlyfans_proxy.inventory WHERE address_hash=:a FOR UPDATE"),{"a":digest}).mappings().first()
    if row is None:
        if required:raise ProxyError("reservation_unavailable")
        return False
    if row["state"]!="assigned" or row["tenant_id"]!=tenant or row["creator_id"]!=creator or open_proxy(key,row)!=proxy_url:
        raise ProxyError("reservation_changed")
    if captured_handle is not None and not row["pending_handle"] and row["handle"].lower()!=captured_handle.lower():
        raise ProxyError("reservation_changed")
    if row["pending_handle"] and captured_handle is not None:
        handle=captured_handle.lower()
        if not re.fullmatch(r"[a-z0-9_.-]{1,80}",handle):raise ProxyError("reservation_changed")
        resolved=owner_key(tenant,creator,handle)
        session.execute(text("UPDATE onlyfans_proxy.inventory SET handle=:h,owner_key=:k,pending_handle=false WHERE address_hash=:a"),
            {"h":handle,"k":resolved,"a":digest})
        session.execute(text("INSERT INTO onlyfans_proxy.audit(address_hash,tenant_id,creator_id,handle,actor,action) VALUES (:a,:t,:c,:h,:u,'resolve_owner')"),
            {"a":digest,"t":tenant,"c":creator,"h":handle,"u":actor})
    if row["used_at"] is None:
        session.execute(text("UPDATE onlyfans_proxy.inventory SET used_at=CURRENT_TIMESTAMP WHERE address_hash=:a"),{"a":digest})
        session.execute(text("INSERT INTO onlyfans_proxy.audit(address_hash,tenant_id,creator_id,handle,actor,action) VALUES (:a,:t,:c,:h,:u,'begin_use')"),
            {"a":digest,"t":tenant,"c":creator,"h":row["handle"],"u":actor})
    return True

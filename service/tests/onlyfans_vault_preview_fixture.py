"""Neutral image + canonical native provenance for real local browser acceptance."""
from datetime import datetime, timedelta, timezone
from io import BytesIO
import uuid

from PIL import Image
from sqlalchemy.orm import Session
from db.models import VaultItem
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_vault_models import OfVaultSyncJob, OfVaultObservation
from onlyfans_runtime.native_context import canonical
from onlyfans_runtime.vault_management import digest
from onlyfans_runtime.vault_previews import warm_page
import vault_still_store as images


def seed_preview(engine, root, monkeypatch):
    now=datetime.now(timezone.utc).replace(tzinfo=None)
    media={"id":11,"type":"photo","createdAt":now.isoformat()+"Z","canView":True,
           "files":{"preview":{"url":"https://cdn2.onlyfans.com/synthetic/preview.png"}}}
    # No isReady/full video proof is supplied: this picture cannot make the
    # existing unverified cached catalogue media eligible for a new sale.
    with Session(engine) as s,s.begin():
        s.add(OfProviderConnection(account_id="acc-a",binding_id="binding-acc-a",owner_id=88001,revision=1,
            sealed_config="fixture-not-a-credential",expires_at=now+timedelta(hours=1),status="paused",verified_at=now,updated_at=now))
        row=s.get(VaultItem,("acc-a",11));row.kind="photo";row.raw_json=canonical(media)
        job_id=str(uuid.uuid4())
        s.add(OfVaultSyncJob(account_id="acc-a",job_id=job_id,binding_id="binding-acc-a",command_key="e"*64,
            request_hash="f"*64,actor="op-session:"+"a"*64,connection_revision=1,owner_id=88001,max_pages=200,
            status="complete",page_offset=1,pages_read=1,items_seen=1,requested_at=now,updated_at=now,completed_at=now))
        s.flush()
        s.add(OfVaultObservation(account_id="acc-a",media_id=11,job_id=job_id,raw_hash=digest(row.raw_json),observed_at=now))
    picture=BytesIO();Image.new("RGB",(96,128),"#87bde8").save(picture,format="PNG")
    class Transport:
        account_id="acc-a"
        def still_bytes(self,url):return picture.getvalue()
    monkeypatch.setattr(images,"IMAGE_DIR",root)
    with monkeypatch.context() as worker:
        worker.setenv("ONLYFANS_PROCESS_ROLE","worker")
        assert warm_page(Transport(),[media],binding_id="binding-acc-a",owner_id=88001)==1
    return picture.getvalue()

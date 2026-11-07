"""Scoped adapters over existing VaultItem, VaultFolder and VaultFolderItem.

No provider write, file upload, folder publication, analysis or message send.
The immutable table records commands, not a second metadata/folder owner.
"""
import json
import re
from sqlalchemy import select,func,delete
from sqlalchemy.orm import Session
from db.models import VaultItem,VaultFolder,VaultFolderItem
from db.onlyfans_vault_edit_models import OfVaultEdit
from vault_ai_effective import effective_value,project_operator_text
from .configuration import object_value
from .native_context import canonical
from .store import Refused,utcnow
from .vault_management import InvalidVault,_identity,media_reference,digest


def asset_fingerprint(row):
    return digest({key:getattr(row,key) for key in ("account_id","media_id","kind","content_hash","content_hash_kind","description","tags",
        "operator_overrides_json","locked_fields_json","ai_fields_json","removed_at")})


def asset_view(row):
    description=effective_value(row,"description")
    tags=effective_value(row,"tags",default=[])
    return {"mediaId":str(row.media_id),"fingerprint":asset_fingerprint(row),
        "description":description if description is None or type(description) is str and len(description.encode())<=2000 else None,
        "tags":tags if type(tags) is list and len(tags)<=12 and all(type(t) is str and len(t.encode())<=80 for t in tags) else None}


def _asset(s,account,media):
    row=s.scalar(select(VaultItem).where(VaultItem.account_id==account,VaultItem.media_id==media).with_for_update())
    if row is None:raise Refused("vault media unavailable")
    return row


def get_asset(store,tenant,account,media_id):
    media=media_reference(media_id)
    with Session(store.engine) as s,s.begin():
        store._runtime(s,tenant,account)
        return asset_view(_asset(s,account,media))


def folder_reference(value):
    fid=media_reference(value)
    if fid>=2**31:raise InvalidVault("Use a supported internal-folder reference.")
    return fid


def _text(value,maximum):
    try:return type(value) is str and "\0" not in value and len(value.encode())<=maximum
    except UnicodeError:return False


def _folder(s,account,fid):
    row=s.scalar(select(VaultFolder).where(VaultFolder.account_id==account,VaultFolder.id==fid,
        VaultFolder.deleted_at.is_(None)).with_for_update())
    if row is None:raise Refused("vault folder unavailable")
    return row


def folder_view(s,row):
    ids=list(s.scalars(select(VaultFolderItem.media_id).where(VaultFolderItem.account_id==row.account_id,
        VaultFolderItem.folder_id==row.id).order_by(VaultFolderItem.media_id).limit(5001)))
    if len(ids)>5000:raise Refused("vault folder exceeds editor budget")
    return {"folderId":str(row.id),"name":row.name,"count":len(ids),"archived":row.deleted_at is not None,
        "fingerprint":digest([row.account_id,row.id,row.name,row.deleted_at,ids])}


def get_folders(store,tenant,account,folder_id=None,*,include_covers=True):
    if type(include_covers) is not bool:
        raise InvalidVault("Use an explicit folder cover option.")
    with Session(store.engine) as s,s.begin():
        store._runtime(s,tenant,account)
        if folder_id is not None:return folder_view(s,_folder(s,account,folder_reference(folder_id)))
        rows=list(s.scalars(select(VaultFolder).where(VaultFolder.account_id==account,VaultFolder.deleted_at.is_(None))
            .order_by(VaultFolder.id).limit(201)))
        if len(rows)>200:raise Refused("vault folder list exceeds editor budget")
        counts=dict(s.execute(select(VaultFolderItem.folder_id,func.count()).where(VaultFolderItem.account_id==account,
            VaultFolderItem.folder_id.in_([r.id for r in rows])).group_by(VaultFolderItem.folder_id)).all())
        if not include_covers:
            return [{"folderId":str(r.id),"name":r.name,"count":counts.get(r.id,0)} for r in rows]
        covers=dict(s.execute(select(VaultFolderItem.folder_id,func.min(VaultFolderItem.media_id))
            .join(VaultItem,(VaultItem.account_id==VaultFolderItem.account_id)&(VaultItem.media_id==VaultFolderItem.media_id))
            .where(VaultFolderItem.account_id==account,VaultFolderItem.folder_id.in_([r.id for r in rows]),
                VaultItem.removed_at.is_(None),VaultItem.kind.in_(("photo","video","gif")))
            .group_by(VaultFolderItem.folder_id)).all())
        kinds=dict(s.execute(select(VaultItem.media_id,VaultItem.kind).where(VaultItem.account_id==account,
            VaultItem.media_id.in_(list(covers.values())))).all()) if covers else {}
        return [{"folderId":str(r.id),"name":r.name,"count":counts.get(r.id,0),
            "coverMediaId":str(covers[r.id]) if r.id in covers else None,
            "coverKind":kinds.get(covers.get(r.id)),
            "automatic":bool(r.created_by and r.created_by.startswith("auto-category:"))} for r in rows]


def _prior(s,runtime,key,request_hash,actor):
    row=s.get(OfVaultEdit,(runtime.account_id,key))
    if row is None:return None
    if (row.binding_id,row.request_hash,row.actor)!=(runtime.binding_id,request_hash,actor):raise Refused("vault edit idempotency conflict")
    return json.loads(row.result_json)


def _record(s,runtime,key,request_hash,actor,operation,target,before,after):
    result={"targetId":str(target),"fingerprint":after["fingerprint"]}
    s.add(OfVaultEdit(account_id=runtime.account_id,command_key=key,binding_id=runtime.binding_id,request_hash=request_hash,
        actor=actor,operation=operation,target_id=str(target),before_json=canonical(before),after_json=canonical(after),
        result_json=canonical(result),created_at=utcnow()))
    return result


def _fingerprint(value):
    if type(value) is not str or not re.fullmatch(r"[a-f0-9]{64}",value):raise InvalidVault("Refresh the current asset or folder before editing.")


def edit_asset(store,tenant,account,media_id,*,patch,expected_fingerprint,command_key,actor):
    _identity(command_key,actor);_fingerprint(expected_fingerprint);media=media_reference(media_id)
    if type(patch) is not dict or not patch or not set(patch)<={"description","tags"}:raise InvalidVault("Edit only the asset description and tags.")
    if "description" in patch and not (patch["description"] is None or _text(patch["description"],2000)):raise InvalidVault("Use a description of at most 2000 bytes.")
    if "tags" in patch and (type(patch["tags"]) is not list or len(patch["tags"])>12 or
        any(not _text(t,80) or not t.strip() for t in patch["tags"])):raise InvalidVault("Use up to 12 short text tags.")
    request_hash=digest(["asset",media,patch,expected_fingerprint])
    with Session(store.engine) as s,s.begin():
        runtime=store._runtime(s,tenant,account)
        prior=_prior(s,runtime,command_key,request_hash,actor)
        if prior is not None:return prior
        row=_asset(s,account,media);before=asset_view(row)
        if before["fingerprint"]!=expected_fingerprint:raise Refused("vault metadata changed")
        try:
            overrides=object_value(row.operator_overrides_json)
            locked=json.loads(row.locked_fields_json) if row.locked_fields_json else []
            if type(locked) is not list or any(type(k) is not str for k in locked):raise ValueError()
        except (ValueError,TypeError):raise InvalidVault("Saved metadata needs review before it can be merged.") from None
        locked=set(locked);columns={}
        for field,value in patch.items():project_operator_text(field,value,overrides,locked,columns,item=row)
        for field,value in columns.items():setattr(row,field,value)
        row.operator_overrides_json=canonical(overrides);row.locked_fields_json=canonical(sorted(locked))
        after=asset_view(row)
        if after["fingerprint"]==before["fingerprint"]:raise InvalidVault("No asset change was needed.")
        return _record(s,runtime,command_key,request_hash,actor,"asset_edit",media,before,after)


def edit_folder(store,tenant,account,folder_id,*,action,name,media_ids,expected_fingerprint,command_key,actor):
    _identity(command_key,actor)
    if type(action) is not str or action not in {"create","rename","archive","add","remove"}:raise InvalidVault("Choose a supported internal-folder action.")
    if action in {"create","rename"}:
        if not _text(name,480) or not name.strip() or len(name)>120 or any(ord(c)<32 for c in name):raise InvalidVault("Use a folder name of at most 120 characters.")
        name=name.strip()
    elif name is not None:raise InvalidVault("This action does not rename a folder.")
    if action in {"add","remove"}:
        if type(media_ids) is not list or not 1<=len(media_ids)<=40:raise InvalidVault("Choose between 1 and 40 asset references.")
        ids=[media_reference(v) for v in media_ids]
        if len(set(ids))!=len(ids):raise InvalidVault("Choose distinct asset references.")
    elif media_ids is not None:raise InvalidVault("This action does not edit folder membership.")
    if action=="create":
        if folder_id is not None or expected_fingerprint is not None:raise InvalidVault("New folders do not use an existing folder version.")
        fid=None
    else:fid=folder_reference(folder_id);_fingerprint(expected_fingerprint)
    request_hash=digest(["folder",fid,action,name,media_ids,expected_fingerprint])
    with Session(store.engine) as s,s.begin():
        runtime=store._runtime(s,tenant,account)
        prior=_prior(s,runtime,command_key,request_hash,actor)
        if prior is not None:return prior
        if action=="create":
            count=s.scalar(select(func.count()).select_from(VaultFolder).where(VaultFolder.account_id==account,VaultFolder.deleted_at.is_(None)))
            if count>=200:raise InvalidVault("Archive an unused folder before adding another.")
            row=VaultFolder(account_id=account,name=name,created_by=actor);s.add(row);s.flush();before={}
        else:
            row=_folder(s,account,fid);before=folder_view(s,row)
            if before["fingerprint"]!=expected_fingerprint:raise Refused("vault folder changed")
            if action=="rename":row.name=name
            elif action=="archive":row.deleted_at=utcnow()
            elif action=="remove":
                s.execute(delete(VaultFolderItem).where(VaultFolderItem.account_id==account,VaultFolderItem.folder_id==fid,VaultFolderItem.media_id.in_(ids)))
            else:
                known=set(s.scalars(select(VaultItem.media_id).where(VaultItem.account_id==account,VaultItem.media_id.in_(ids),VaultItem.removed_at.is_(None))))
                if known!=set(ids):raise Refused("vault media unavailable")
                existing=set(s.scalars(select(VaultFolderItem.media_id).where(VaultFolderItem.account_id==account,VaultFolderItem.folder_id==fid)))
                if len(existing|set(ids))>5000:raise InvalidVault("This folder exceeds the editor budget.")
                for mid in set(ids)-existing:s.add(VaultFolderItem(account_id=account,folder_id=fid,media_id=mid))
        s.flush();after=folder_view(s,row)
        if before and before["fingerprint"]==after["fingerprint"]:raise InvalidVault("No folder change was needed.")
        return _record(s,runtime,command_key,request_hash,actor,"folder_"+action,row.id,before,after)


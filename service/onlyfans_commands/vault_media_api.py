"""Content-purpose admission for native uploads and private video playback."""
import asyncio
import os
import re

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse
from .content_api import PREFIX, response, refuse, HEADER, SCHEMA
from .vault_api import authenticate, body, failure
from onlyfans_runtime.vault_management import InvalidVault
from onlyfans_runtime.store import Refused
from onlyfans_runtime import vault_media as uploads
from onlyfans_runtime import vault_video as videos

router = APIRouter(prefix=PREFIX, tags=["onlyfans-vault-media"])
_chunks = asyncio.Semaphore(2)


def enabled():
    return os.environ.get("ONLYFANS_VAULT_MEDIA_ENABLED") == "1"


def unavailable():
    return refuse("vault_media_unavailable", 503, "Uploads and video preparation are not enabled on this service.")


@router.get("/accounts/{account_id}/vault/uploads")
async def list_uploads(request: Request, account_id: str):
    context = authenticate(request, account_id)
    if isinstance(context, JSONResponse):
        return context
    try:
        if request.query_params:
            raise InvalidVault("Upload status does not accept query parameters.")
        value = await asyncio.to_thread(uploads.list_uploads, *context, account_id)
        return response({"ok": True, "accountId": account_id, "uploads": value})
    except Exception as exc:
        return failure(exc)


@router.post("/accounts/{account_id}/vault/uploads")
async def create_upload(request: Request, account_id: str):
    context = authenticate(request, account_id)
    if isinstance(context, JSONResponse):
        return context
    if not enabled():
        return unavailable()
    try:
        value = await body(request, {"files", "expectedConnectionRevision", "commandKey", "actor"})
        if isinstance(value, JSONResponse):
            return value
        job = await asyncio.to_thread(uploads.request_upload, *context, account_id,
            files=value["files"], expected_connection_revision=value["expectedConnectionRevision"],
            command_key=value["commandKey"], actor=value["actor"])
        return response({"ok": True, "accountId": account_id, "upload": job})
    except Exception as exc:
        return failure(exc)


@router.post("/accounts/{account_id}/vault/uploads/{job_id}/files/{file_id}")
async def chunk(request: Request, account_id: str, job_id: str, file_id: str):
    context = authenticate(request, account_id)
    if isinstance(context, JSONResponse):
        return context
    if not enabled():
        return unavailable()
    try:
        if (list(request.query_params.keys()) != ["offset"] or len(request.query_params.getlist("offset")) != 1
                or not re.fullmatch(r"[0-9]{1,12}", request.query_params["offset"])
                or request.headers.get("content-type", "").split(";")[0] != "application/octet-stream"):
            raise InvalidVault("That chunk request is invalid.")
        if _chunks.locked():
            return refuse("upload_busy", 429, "Wait for the current file transfers to finish.")
        async with _chunks:
            data = bytearray()
            async for part in request.stream():
                if len(data) + len(part) > uploads.CHUNK_BYTES:
                    return refuse("request_too_large", 413, "That upload chunk is too large.")
                data.extend(part)
            value = await asyncio.to_thread(uploads.write_chunk, *context, account_id, job_id, file_id,
                offset=int(request.query_params["offset"]), digest=request.headers.get("x-upload-sha256"),
                data=bytes(data), actor=request.headers.get("x-onlyfans-actor"))
        return response({"ok": True, "accountId": account_id, "jobId": job_id, "fileId": file_id, **value})
    except Exception as exc:
        return failure(exc)


@router.post("/accounts/{account_id}/vault/uploads/{job_id}/{action}")
async def finish(request: Request, account_id: str, job_id: str, action: str):
    context = authenticate(request, account_id)
    if isinstance(context, JSONResponse):
        return context
    if not enabled():
        return unavailable()
    try:
        if action not in {"finish", "cancel"} or request.query_params:
            raise InvalidVault("That upload action is invalid.")
        value = await body(request, {"commandKey", "actor"})
        if isinstance(value, JSONResponse):
            return value
        uploads._identity(value["commandKey"], value["actor"])
        operation = uploads.finish_upload if action == "finish" else uploads.cancel_upload
        job = await asyncio.to_thread(operation, *context, account_id, job_id, actor=value["actor"])
        return response({"ok": True, "accountId": account_id, "upload": job})
    except Exception as exc:
        return failure(exc)


@router.get("/accounts/{account_id}/vault/videos/{media_id}")
async def video_status(request: Request, account_id: str, media_id: str):
    context = authenticate(request, account_id)
    if isinstance(context, JSONResponse):
        return context
    try:
        if request.query_params:
            raise InvalidVault("Video status does not accept query parameters.")
        value = await asyncio.to_thread(videos.video_status, *context, account_id, media_id)
        return response({"ok": True, "accountId": account_id, "video": value})
    except Exception as exc:
        return failure(exc)


@router.post("/accounts/{account_id}/vault/videos/{media_id}")
async def prepare_video(request: Request, account_id: str, media_id: str):
    context = authenticate(request, account_id)
    if isinstance(context, JSONResponse):
        return context
    if not enabled():
        return unavailable()
    try:
        if request.query_params:
            raise InvalidVault("Video preparation does not accept query parameters.")
        value = await body(request, {"commandKey", "actor"})
        if isinstance(value, JSONResponse):
            return value
        video = await asyncio.to_thread(videos.request_video, *context, account_id, media_id,
            command_key=value["commandKey"], actor=value["actor"])
        return response({"ok": True, "accountId": account_id, "video": video})
    except Exception as exc:
        return failure(exc)


@router.api_route("/accounts/{account_id}/vault/videos/{media_id}/stream", methods=["GET", "HEAD"])
async def stream_video(request: Request, account_id: str, media_id: str):
    context = authenticate(request, account_id)
    if isinstance(context, JSONResponse):
        return context
    try:
        if request.query_params:
            raise InvalidVault("Video reads do not accept URLs or query parameters.")
        path, proof = await asyncio.to_thread(videos.read_video, *context, account_id, media_id)
        byte_range = request.headers.get("range")
        if byte_range is not None and (len(byte_range) > 100 or not re.fullmatch(r"bytes=(?:[0-9]+-[0-9]*|-[0-9]+)", byte_range)):
            return JSONResponse({}, status_code=416, headers={"Content-Range": "bytes */" + str(proof["bytes"])})
        return FileResponse(path, media_type=proof["mime"], headers={"Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff", HEADER: SCHEMA, "X-OnlyFans-Video": videos.SCHEMA,
            "X-OnlyFans-Account-Id": account_id, "X-OnlyFans-Media-Id": media_id,
            "Content-Security-Policy": "default-src 'none'; sandbox"})
    except Refused:
        return refuse("video_unavailable", 404, "Prepare this video before playing it.")
    except Exception as exc:
        return failure(exc)

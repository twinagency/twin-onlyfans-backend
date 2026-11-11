"""One bounded automatic caption batch. Started only by the native worker.

The existing FASTT frame collector, caption writer, daily cap and category
assignment remain authoritative. A PostgreSQL advisory lock prevents two
automatic workers captioning one account; no account row lock spans vision.
"""
import argparse
import asyncio
import hashlib
import json
import os
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace
import sys

from sqlalchemy import case, select

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def scoped_caption_key(account_id, model):
    """Optional account-bound credential resolver for this isolated vision child.

    The caller supplies only its verified, decrypted connection model. Secrets
    remain in process memory; this grants no other account, provider or purpose.
    Unselected accounts retain their existing deployment-key configuration.
    """
    from native_pilot.scope import AccountScope
    from onlyfans_runtime.store import Refused
    accounts = {a.strip() for a in os.environ.get(
        'ONLYFANS_VAULT_DESCRIPTION_SCOPED_KEYS', '').split(',') if a.strip()}
    if any(not AccountScope.valid(a) or not a.startswith('of-') for a in accounts):
        raise Refused('invalid scoped caption account configuration')
    if account_id not in accounts:
        return None
    from llm_client import _resolve
    from onlyfans_runtime.provider_connector import _header
    try:
        if type(model) is not dict or set(model) != {'modelId', 'apiKey'}:
            raise ValueError()
        _, provider = _resolve(model['modelId'])
        if provider.name != 'openrouter':
            raise ValueError()
        key = _header(model['apiKey'], 'model credential', 4096)
    except Exception:
        raise Refused('scoped caption credential unavailable') from None

    async def resolve(request_account, provider, purpose=''):
        if (request_account != account_id or getattr(provider, 'name', None) != 'openrouter'
                or purpose != 'describe_media'):
            raise Refused('caption credential scope unavailable')
        return key
    return resolve


def select_batch(session, account, limit):
    """Photos first (cheap, fast) but never to the exclusion of videos: whenever both
    kinds are pending, ONE video rides in every batch, so a long photo backlog cannot
    starve clips. A video-only or photo-only backlog fills the batch as before (a
    video batch stays at one clip: it is the long, bounded unit of work)."""
    from db.models import VaultItem
    from onlyfans_runtime.store import utcnow
    from onlyfans_runtime.vault_description_worker import pending
    def pick(kind, n):
        query = select(VaultItem.media_id).where(
            VaultItem.account_id == account, VaultItem.kind == kind, pending(utcnow()))
        if kind == "photo":
            query = query.order_by(case((VaultItem.describe_status.is_(None), 0), else_=1))
        # Eligible video retries keep their place among newly imported clips.
        from vault_import_policy import enabled
        order = (VaultItem.created_at.desc(), VaultItem.media_id.desc()) if enabled() else (VaultItem.media_id,)
        return list(session.scalars(query.order_by(*order).limit(n)))
    video = pick("video", 1)
    if limit == 1:
        return sorted(pick("photo", 1) + video)[:1]
    return pick("photo", limit - 1 if video else limit) + video


async def run(args):
    if os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker":
        raise RuntimeError("worker role required")
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from db.engine import get_session
    from db.models import AccountAiConfig, OfCreatorBinding, VaultItem
    from db.onlyfans_models import OfRuntime
    from db.onlyfans_connection_models import OfProviderConnection
    from db.onlyfans_vault_models import OfVaultObservation, OfVaultSyncJob
    from onlyfans_runtime.store import Store, Refused, utcnow
    from onlyfans_runtime.provider_read_budget import ReadDeferred
    from onlyfans_runtime.vault_management import _connection, _provenance
    from onlyfans_runtime.connections import _open
    from onlyfans_runtime.vault_description_worker import pending
    import vault_describe_state as vds
    from vault_ai_effective import effective_value, locked_fields
    from native_pilot.scope import AccountScope
    from native_pilot.connection import build_client
    from native_pilot.tools import caption_vault

    scope = AccountScope.parse(args.account)
    if not scope.twin_form or args.tenant != os.environ.get("ONLYFANS_WORKER_TENANT"):
        raise Refused("caption scope unavailable")
    store = Store(os.environ["DATABASE_URL"])
    summary = {"schemaVersion": "of-vault-description.v1", "account": args.account,
               "state": "complete", "described": 0, "failed": 0, "unchanged": 0, "skipped": 0,
               "failures": {}, "costMillicents": 0}
    client = None
    try:
        with store.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as lease:
            if lease.dialect.name != "postgresql":
                raise Refused("caption lock unavailable")
            key = int.from_bytes(hashlib.sha256(("vault-description.v1:" + args.tenant + ":" + args.account).encode()).digest()[:8], "big", signed=True)
            if not lease.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}):
                summary["state"] = "busy"
                return summary
            try:
                with Session(store.engine) as session, session.begin():
                    runtime = store._runtime(session, args.tenant, args.account)
                    connection = _connection(session, runtime)
                    if runtime.binding_id != args.binding or connection.revision != args.revision:
                        raise Refused("caption generation changed")
                    clear = _open(connection, args.tenant)
                    # Captioning needs the verified media session and a scoped
                    # vision credential, independently of enabling fan replies.
                    snapshot = SimpleNamespace(scope=scope, expires_at=connection.expires_at.replace(tzinfo=timezone.utc),
                        capture=clear["capture"], x_hash=clear["xHash"], proxy_url=clear["proxyUrl"])
                    ids = select_batch(session, args.account, args.limit)
                metadata = {}

                async def guard(session, media_id, *, phase="read"):
                    # A lost advisory connection ends this batch. Rebinding,
                    # credential replacement or removal also fences every write.
                    lease.execute(text("SELECT 1"))
                    runtime = await session.get(OfRuntime, args.account, with_for_update=True)
                    binding = await session.get(OfCreatorBinding, args.binding)
                    connection = await session.get(OfProviderConnection, args.account, with_for_update=True)
                    config = await session.get(AccountAiConfig, args.account)
                    item = await session.get(VaultItem, (args.account, media_id), with_for_update=True)
                    observation = await session.get(OfVaultObservation, (args.account, media_id))
                    job = await session.get(OfVaultSyncJob, (args.account, observation.job_id)) if observation else None
                    from onlyfans_runtime.provider_read_budget import native_family_clause
                    selected_native=await session.scalar(select(native_family_clause(args.account)))
                    if (not selected_native or runtime is None or binding is None or connection is None or item is None
                        or runtime.binding_id != args.binding or binding.tenant_id != args.tenant
                        or binding.revoked_at is not None or binding.account_id != args.account
                        or binding.channel != "onlyfans" or connection.revision != args.revision
                        or connection.verified_at is None or connection.last_error is not None
                        or str(connection.owner_id) != scope.provider_id
                        or connection.status != "verified" or connection.expires_at <= utcnow()
                        or item.removed_at is not None
                        or not _provenance(item, observation, job, connection, args.binding, current_revision=False)):
                        raise Refused("caption generation changed")
                    from vault_import_policy import automatic_window, enabled
                    if enabled() and not await session.scalar(select(VaultItem.media_id).where(
                            VaultItem.account_id == args.account, VaultItem.media_id == media_id,
                            automatic_window())):
                        raise Refused("caption item left recent window")
                    settings = json.loads(config.vault_ai_config_json or "{}") if config else {}
                    if not isinstance(settings, dict) or settings.get("auto_describe_on_import") is False:
                        raise Refused("caption analysis paused")
                    fingerprint = (item.description, item.video_description, item.tags,
                        item.operator_overrides_json, item.locked_fields_json, item.describe_status)
                    if phase != "categorise":
                        if media_id in metadata and metadata[media_id] != fingerprint:
                            raise Refused("caption metadata changed")
                        metadata[media_id] = fingerprint

                client = await asyncio.to_thread(build_client, snapshot)
                caption_vault.install_client_adapter(client, args.account)
                caption_vault.install_llm_adapter()  # isolated child only; parent keeps its worker role
                import vault_ai_api as api
                api._VISION_PROVIDER = "openrouter"
                import llm_client
                account_key = scoped_caption_key(args.account, clear.get('model'))
                if account_key is not None:
                    llm_client._tenant_api_key = account_key
                legacy_chat = llm_client.chat
                os.environ["ONLYFANS_PROCESS_ROLE"] = "worker"

                async def caption_chat(**kwargs):
                    if (kwargs.get("account_id") != args.account or kwargs.get("purpose") != "describe_media"
                        or kwargs.get("model") not in {caption_vault.DEFAULT_MODEL, "qwen3-vl-235b"}):
                        raise Refused("caption model scope unavailable")
                    # The established caption CLI's legacy bridge is confined
                    # to this one serial, metadata-only child and purpose.
                    if account_key is not None and kwargs.get('model') != caption_vault.DEFAULT_MODEL:
                        raise Refused('caption fallback provider unavailable')
                    from onlyfans_runtime.provider_read_budget import native_family_clause
                    async with get_session() as session:
                        if not await session.scalar(select(native_family_clause(args.account))):
                            raise Refused("native provider family is not selected")
                    token=llm_client._vault_caption_account.set(args.account)
                    try:
                        return await legacy_chat(**kwargs)
                    finally:
                        llm_client._vault_caption_account.reset(token)

                llm_client.chat = caption_chat
                from onlyfans_runtime.provider_read_budget import acquire, current, release
                http_call = client._http_call

                def bounded_read(method, url, **kwargs):
                    if method != "GET":
                        raise Refused("caption provider write unavailable")
                    claim = acquire(store, args.tenant, args.account,
                        connection_revision=args.revision, kind="vault")
                    failure = None
                    try:
                        kwargs["proxy_tries"] = 1
                        kwargs["allow_redirects"] = False
                        result = http_call(method, url, **kwargs)
                        current(store, args.tenant, claim)
                        if result.status_code == 429:
                            try:
                                delay = min(3600, max(1, int(result.headers.get("Retry-After", "60"))))
                            except (ValueError, TypeError):
                                delay = 60
                            failure = SimpleNamespace(retry_after=delay)
                        return result
                    except Exception as exc:
                        failure = exc
                        raise
                    finally:
                        release(store, args.tenant, claim, retry_after=getattr(failure, "retry_after", None))

                # Pace each API fetch, not ffmpeg/vision processing. Long video
                # processing must not expire an otherwise successful read lease.
                client._http_call = bounded_read
                # Legacy re-signing rewrites raw_json without the canonical
                # observation hash. Keep fresh URLs in memory instead, and warm
                # native stills with the existing binding/owner receipt writer.
                import vault_stills
                from onlyfans_runtime import vault_previews

                async def fresh_media(account_id, media_id, passed_client, *, mark_gone=True):
                    if account_id != args.account or passed_client is not client:
                        raise Refused("caption media scope unavailable")
                    async with get_session() as session:
                        await guard(session, media_id)
                        item = await session.get(VaultItem, (args.account, media_id))
                        kind = item.kind
                    try:
                        media = await asyncio.to_thread(client.vault_media_by_id, media_id)
                    except ReadDeferred:
                        raise
                    except Exception:
                        return vault_stills.Fresh(None, "fetch_failed")
                    if (not isinstance(media, dict) or media.get("id") != media_id
                        or media.get("type") != kind or media.get("isDeleted") is True
                        or media.get("isHidden") is True or media.get("canView") is False):
                        return vault_stills.Fresh(None, "fetch_failed")
                    return vault_stills.Fresh(media, "ok")

                vault_stills.resolve_fresh = fresh_media

                async def warm_preview(media_id):
                    if vault_previews.promote_cached(args.account, args.binding, int(scope.provider_id), media_id):
                        return True
                    fresh = await fresh_media(args.account, media_id, client)
                    if fresh.media is not None:
                        transport = SimpleNamespace(account_id=args.account,
                            still_bytes=lambda url: vault_stills.fetch_bytes_sync(client, url))
                        await asyncio.to_thread(vault_previews.warm_page, transport, [fresh.media],
                            binding_id=args.binding, owner_id=int(scope.provider_id))
                    return vault_previews._read(args.account, args.binding, int(scope.provider_id), media_id) is not None

                budget = await api._vision_budget(args.account)
                if budget["capped"]:
                    summary["state"] = "budget_paused"
                    return summary
                for media_id in ids:
                    async with get_session() as session:
                        await guard(session, media_id)
                        item = await session.get(VaultItem, (args.account, media_id))
                        if ("description" in locked_fields(item) or "video_description" in locked_fields(item)
                            or (vds.has_description(item) and not vds.content_changed(item))):
                            summary["skipped"] += 1
                            continue
                    preview_ready = await warm_preview(media_id)
                    if item.kind == "photo" and not preview_ready:
                        async with get_session() as session:
                            await guard(session, media_id, phase="write")
                        await api._record_failure(args.account, media_id, "fetch_failed", "fetching", scope_guard=guard)
                        summary["failed"] += 1
                        summary["failures"]["fetching"] = summary["failures"].get("fetching", 0) + 1
                        continue
                    result = await api._describe_one(args.account, media_id, model=caption_vault.DEFAULT_MODEL,
                        scope_guard=guard, skip_unchanged=True, escalate=account_key is None)
                    summary["costMillicents"] += int(result.get("cost_millicents") or 0)
                    if result.get("ok"):
                        summary["unchanged" if result.get("skipped") else "described"] += 1
                    else:
                        category = result.get("category") or "busy"
                        summary["failed"] += 1
                        summary["failures"][category] = summary["failures"].get(category, 0) + 1
                    if result.get("category") in ("budget", "auth"):
                        # Account-level: stop the batch, surface it, spend nothing more.
                        summary["state"] = "budget_paused" if result["category"] == "budget" else "retry_pending"
                        summary["reason"] = result["category"]
                        break
                return summary
            except ReadDeferred:
                summary["state"] = "retry_pending"
                summary["reason"] = "provider_read_deferred"
                return summary
            finally:
                lease.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
    finally:
        if client is not None:
            await asyncio.to_thread(client.http.close)
        store.close()


_ERROR_TYPES = frozenset({
    "Refused", "ReadDeferred", "RuntimeError", "ValueError", "TypeError",
    "KeyError", "AttributeError", "ImportError", "ModuleNotFoundError",
    "TimeoutError", "OSError", "FileNotFoundError", "OperationalError",
    "ProgrammingError", "IntegrityError", "DBAPIError", "InvalidTag",
    "LLMError", "LLMConfigError", "LLMHTTPError", "LLMCapExceeded",
})
_ERROR_STAGES = {
    "native_pilot/tools/describe_imported_vault.py": {
        "main": "entrypoint", "run": "batch", "guard": "scope_guard",
        "fresh_media": "metadata_refresh", "warm_preview": "preview",
        "bounded_read": "provider_read", "caption_chat": "model_authority",
        "describe_item": "item", "scoped_caption_key": "model_credential",
    },
    "native_pilot/connection.py": {"*": "client_setup"},
    "onlyfans_runtime/connections.py": {"*": "connection_open"},
    "onlyfans_runtime/store.py": {"*": "account_scope"},
    "onlyfans_runtime/provider_read_budget.py": {"*": "provider_budget"},
    "onlyfans_runtime/vault_previews.py": {"*": "preview"},
    "vault_frames.py": {"*": "frame_collection"},
    "vault_storyboard.py": {"*": "storyboard"},
    "vault_stills.py": {"*": "media_fetch"},
    "vault_ai_api.py": {"*": "description"},
    "llm_client.py": {"*": "model"},
    "db/engine.py": {"*": "database"},
}


def safe_error_fields(exc):
    """Only fixed labels from exception type and trusted source locations.

    Never inspect exception text, args, locals, context, URLs or provider data.
    Third-party and unknown source paths cannot introduce output strings.
    """
    name = type(exc).__name__
    error_type = name if name in _ERROR_TYPES else "OtherError"
    stage = "unknown"
    root = Path(__file__).resolve().parents[2]
    trace = exc.__traceback__
    while trace is not None:
        try:
            path = Path(trace.tb_frame.f_code.co_filename).resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            path = ""
        stages = _ERROR_STAGES.get(path, {})
        stage = stages.get(trace.tb_frame.f_code.co_name, stages.get("*", stage))
        trace = trace.tb_next
    reason = "error"
    if error_type in {"LLMConfigError", "InvalidTag"} or stage == "model_credential":
        reason = "auth"
    elif error_type == "LLMCapExceeded":
        reason = "budget"
    elif error_type == "ReadDeferred":
        reason = "provider_read_deferred"
    elif error_type == "Refused":
        reason = "changed" if stage == "scope_guard" else "scope"
    return {"reason": reason, "errorType": error_type, "errorStage": stage}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--binding", required=True)
    parser.add_argument("--revision", required=True, type=int)
    parser.add_argument("--limit", type=int, default=4)
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 4 or args.revision < 1:
        parser.error("Use a current revision and a batch of 1..4")
    try:
        result = asyncio.run(run(args))
    except Exception as exc:
        print(json.dumps({"schemaVersion": "of-vault-description.v1", "account": args.account,
                          "state": "retry_pending", **safe_error_fields(exc)}), flush=True)
        return 1
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Credential readiness projection for the personality review.

Metadata only: reads the existing connection row and opens it through the
existing connection authority (``connections._open``). It makes no provider
request, builds no client and has no environment key fallback. Key material is
never returned or logged.
"""
from __future__ import annotations
from onlyfans_read.contract import ID_PATTERN
from db.models import OfCreatorBinding
from db.onlyfans_models import OfRuntime
from db.onlyfans_connection_models import OfProviderConnection
from .connections import _open
from .store import Refused, utcnow


def _model_id(model):
    """The saved model id when the encrypted selection is complete and known."""
    if type(model) is not dict or set(model) != {"modelId", "apiKey"}:
        return None
    key, model_id = model["apiKey"], model["modelId"]
    if (type(key) is not str or not 0 < len(key) <= 4096 or not key.isascii() or not key.isprintable()
            or type(model_id) is not str):
        return None
    from llm_client import LLMConfigError, _resolve
    try:
        _resolve(model_id)
    except LLMConfigError:
        return None
    return model_id


def personality_credential_status(session, account_id, binding_id):
    """``(session, account_id, binding_id) -> {accountId, modelId, configured, expiresAt} | None``.

    ``None`` means readiness cannot be verified (no or foreign connection,
    revoked binding, or credentials the connection authority cannot open).
    ``configured`` is true only for a verified, unexpired connection owned by
    this binding whose encrypted selection names a known model with a key.
    """
    if type(account_id) is not str or type(binding_id) is not str or not ID_PATTERN.fullmatch(account_id):
        return None
    binding = session.get(OfCreatorBinding, binding_id)
    runtime = session.get(OfRuntime, account_id)
    row = session.get(OfProviderConnection, account_id)
    if (binding is None or runtime is None or row is None or binding.account_id != account_id
            or binding.channel != "onlyfans" or binding.revoked_at is not None
            or runtime.binding_id != binding_id or row.binding_id != binding_id):
        return None
    try:
        sealed = _open(row, binding.tenant_id)
    except Refused:
        return None
    model_id = _model_id(sealed.get("model"))
    configured = bool(model_id is not None and row.status == "verified" and row.verified_at is not None
        and row.expires_at > utcnow())
    return {"accountId": account_id, "modelId": model_id or "", "configured": configured,
        "expiresAt": row.expires_at.isoformat() + "Z"}

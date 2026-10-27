"""Keep dedicated OnlyFans roles separate from captured-session transports.

Dedicated roles currently have no approved live provider transport. Check at
construction and at each egress attempt so a previously cached client cannot
continue after role selection. The unset-role legacy application is unchanged.

One narrow, explicit exception exists for the native pilot subprocess: a process
running as the ``worker`` role may call ``allow_native_pilot_provider_access``
once, naming ONE decimal provider account. Afterwards the guard passes for that
account only (and only for callers that state their account), in that process
only. It is never set by import, environment or configuration, the ``api`` role
can never use it, and there is no way to widen or change it later.
"""
import os

_PILOT_PROVIDER_ID = None


def allow_native_pilot_provider_access(provider_id: str) -> None:
    global _PILOT_PROVIDER_ID
    if type(provider_id) is not str or not provider_id.isascii() or not provider_id.isdecimal():
        raise ValueError("a decimal provider account is required")
    role = os.environ.get("ONLYFANS_PROCESS_ROLE")
    if role is not None and role != "worker":
        raise RuntimeError("Native pilot provider access requires the worker role or no role")
    if _PILOT_PROVIDER_ID is not None and _PILOT_PROVIDER_ID != provider_id:
        raise RuntimeError("Native pilot provider access is already bound to another account")
    _PILOT_PROVIDER_ID = provider_id


def require_legacy_provider_role(account_id=None) -> None:
    if "ONLYFANS_PROCESS_ROLE" in os.environ:
        if (_PILOT_PROVIDER_ID is not None and os.environ["ONLYFANS_PROCESS_ROLE"] == "worker"
                and (account_id is None or str(account_id) == _PILOT_PROVIDER_ID)):
            return
        raise RuntimeError(
            "Live OFClient is unavailable in dedicated OnlyFans process roles"
        )

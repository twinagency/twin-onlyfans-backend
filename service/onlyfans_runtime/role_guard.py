"""Keep dedicated OnlyFans roles separate from captured-session transports.

Dedicated roles currently have no approved live provider transport. Check at
construction and at each egress attempt so a previously cached client cannot
continue after role selection. The unset-role legacy application is unchanged.
"""
import os


def require_legacy_provider_role() -> None:
    if "ONLYFANS_PROCESS_ROLE" in os.environ:
        raise RuntimeError(
            "Live OFClient is unavailable in dedicated OnlyFans process roles"
        )

"""The scoped capabilities object FASTT's ai_chatter runs against inside the runner.

`telegram_runtime.capabilities` (the pilot's missing dependency) never existed as a
module; the original pilot swapped these seams with unittest.mock. This is the
deliberate, minimal, process-local replacement. Entering the context:

  * client factory  -> the FasttSendGate (exact account + fan only, durable admission per send)
  * model           -> routed by the llm_client scoped-gateway seam (registered by the runner)
  * config          -> the account's saved ai_chatter config with pilot overrides on top
  * disabled lanes  -> stickers, typing WebSocket, teasers, day-log, job enqueues, profile writers
  * bookkeeping     -> ai_chatter's own outbound attribution / offer / vault / quote writers become
                       no-ops, because TextAuthority.materialize owns those rows exactly once

Nothing here changes a selling rule, a prompt, a price or a persona. It is a no-op outside the
`with` block and restores every original on exit.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging

from .fastt_errors import FasttHalt

log = logging.getLogger("fastt_runner")

# Lanes the pilot does not grant. Everything else in the saved config (selling rules, price band,
# offer pacing, prompt_sell_catalog ...) is left exactly as the account has it.
DISABLED_LANES = {
    "pack_send_enabled": False, "pack_on_ask_enabled": False, "hook_upsell_enabled": False,
    "hook_upsell_early_ask": False, "life_tip_ask_enabled": False, "life_tip_big_enabled": False,
    "profile_openers_enabled": False, "cadence_enabled": False, "nudge_enabled": False,
    "daily_quota_enabled": False, "rhythm_enabled": False, "engage_old_fans": False,
    "post_buy_rung_enabled": False, "gift_enabled": False, "filming_stall_enabled": False,
    "unsend_expired_offer": False,
}
FORCED_ON = {"enabled": True, "mode": "always", "payers_only": False}


@dataclass(frozen=True)
class Scope:
    tenant_id: str
    account_id: str
    fan_id: int


async def _noop(*args, **kwargs):
    return None


class ScopedCapabilities:
    def __init__(self, scope: Scope, gate, *, config_overrides=None, dry_run=False, pinned_model=None):
        self.scope, self.gate, self.dry_run, self.pinned_model = scope, gate, dry_run, pinned_model
        self.config_overrides = {**DISABLED_LANES, **(config_overrides or {}), **FORCED_ON}
        self.suppressed_jobs: list[tuple] = []
        self._saved: list[tuple] = []

    # -- patch helpers ---------------------------------------------------------------------------------
    def _set(self, obj, name, value):
        self._saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def __enter__(self):
        import automation_executor as ax
        from automations import ai_chatter, _daylog, teaser_select

        scope, gate = self.scope, self.gate

        def make_client(account_id):
            if str(account_id) != scope.account_id:
                raise FasttHalt("scope_violation")
            return gate
        self._set(ax, "_make_client", make_client)

        real_load = ai_chatter._load_config
        overrides = self.config_overrides

        async def scoped_config(account_id):
            if str(account_id) != scope.account_id:
                raise FasttHalt("scope_violation")
            return {**(await real_load(account_id)), **overrides}
        self._set(ai_chatter, "_load_config", scoped_config)

        async def off(*a, **k):
            return False

        async def none(*a, **k):
            return None

        async def no_day(*a, **k):
            return _daylog.NO_DAY

        jobs = self.suppressed_jobs

        async def suppress_job(account_id, kind, *a, **k):
            jobs.append((kind, k.get("payload")))
            return None

        if self.pinned_model is not None:
            pinned = self.pinned_model

            async def pinned_model(account_id, purpose, override=None):
                # The sealed credential's model IS the account's saved choice: no house default, no override.
                return pinned
            self._set(ai_chatter, "resolve_model", pinned_model)
        self._set(ai_chatter, "load_cat_stickers_flag", off)
        self._set(ai_chatter, "load_typing_indicator", off)
        self._set(teaser_select, "hot_teaser_config", none)
        self._set(teaser_select, "convo_teaser_config", none)
        self._set(_daylog, "load_day", no_day)
        self._set(ax, "enqueue_job", suppress_job)
        # One bookkeeping writer: TextAuthority.materialize / media_delivery.project (see module doc).
        for name in ("write_outbound_attribution", "_record_vault_sends", "_record_offer", "_record_quote"):
            self._set(ai_chatter, name, _noop)
        return self

    def __exit__(self, *exc):
        while self._saved:
            obj, name, value = self._saved.pop()
            setattr(obj, name, value)
        return False

"""Explicit, fail-closed startup for the Plan C *draft-only* worker.

This constructs the existing engine, intake and DraftOnlyTransport. It grants
no delivery capability, does not enable a policy, and makes no provider/model
request during startup. A separate supervised live-dispatch runtime is not
implemented here.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat

from sqlalchemy.orm import Session

from conversation_engine.draft_only import DraftOnlyTransport
from conversation_engine.journey import journey_config
from conversation_engine.model_config import ModelConfig
from conversation_engine.real_worker import RealConversationWorker
from conversation_engine.writer_context import JsonFileCreatorProfiles
from db.models import CatalogItem
from fans_api_adapter.events import FansEventIntake
from fans_api_adapter.real_transport import require_allowed_chats
from onlyfans_runtime.store import Refused
from .wiring import build_engine_service

_NAME = re.compile(r"[A-Za-z0-9._:-]{1,64}\Z")


def _number(value, *, minimum=1, maximum=2**63-1):
    if (type(value) is not str or len(value) > 19 or not value.isascii() or not value.isdecimal()
            or str(int(value)) != value or not minimum <= int(value) <= maximum):
        raise Refused('invalid draft pilot scope')
    return int(value)


def _ids(value, *, allow_empty=False):
    if type(value) is not str:
        raise Refused('invalid draft pilot media scope')
    parts = value.split(',') if value else []
    if (not allow_empty and not parts) or len(parts) > 100:
        raise Refused('invalid draft pilot media scope')
    ids = tuple(str(_number(part)) for part in parts)
    if len(ids) != len(set(ids)):
        raise Refused('duplicate draft pilot media')
    return ids


def _secret(path):
    if type(path) is not str or not path:
        raise Refused('draft webhook secret file is required')
    file = Path(path).resolve()
    try:
        info = file.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid()
                or any((parent / '.git').exists() for parent in file.parents)):
            raise Refused('draft webhook secret file must be private and outside a worktree')
        data = file.read_bytes()
    except OSError:
        raise Refused('draft webhook secret is unavailable') from None
    if not 1 <= len(data) <= 512 or any(c in data for c in (0, 10, 13)):
        raise Refused('draft webhook secret is invalid')
    return data


@dataclass(frozen=True)
class DraftPilot:
    worker: RealConversationWorker
    tenant: str
    account: str
    secret: bytes
    item_id: int
    price_cents: int
    media: tuple
    previews: tuple
    profiles: JsonFileCreatorProfiles
    allowed_chats: frozenset
    model_config: ModelConfig

    def __repr__(self):
        return f'DraftPilot(account={self.account!r}, secret=<redacted>)'

    def validate(self):
        # Repeat before every event; a restart or another process cannot turn
        # this worker into a sender, and a changed product cannot be drafted.
        self.worker._gate(self.tenant, self.account)
        cfg = self.worker.engine.model.config
        journey = journey_config()
        profile = self.profiles.get(self.account)
        if (cfg != self.model_config or not cfg.enabled or cfg.fallback_enabled
                or cfg.enabled_accounts != frozenset({self.account})
                or cfg.allowed_models != (cfg.primary_model,)
                or self.worker.transport.allowed_chats != self.allowed_chats
                or journey.temporary_pause_reopen_hours is not None or journey.auto_backfill
                or profile is None or profile.missing_inputs() or not profile.offers_permitted
                or profile.approved_item_ids != (str(self.item_id),)):
            raise Refused('draft pilot configuration changed')
        with Session(self.worker.engine.store.engine) as session:
            item = session.get(CatalogItem, self.item_id)
            try:
                raw_media, raw_previews = json.loads(item.media_ids), json.loads(item.preview_media_ids)
                if type(raw_media) is not list or type(raw_previews) is not list:
                    raise ValueError('invalid media shape')
                media, previews = tuple(str(x) for x in raw_media), tuple(str(x) for x in raw_previews)
            except (TypeError, ValueError, AttributeError):
                raise Refused('draft pilot product is unavailable') from None
            if (item.account_id != self.account or item.enabled is not True or item.script_id is not None
                    or item.price_cents != self.price_cents or media != self.media or previews != self.previews):
                raise Refused('draft pilot product changed')

    def handle(self, raw, signature, event_key):
        self.validate()
        result = self.worker.handle(self.tenant, self.account, raw_body=raw, signature=signature,
                                    secret=self.secret, event_key=event_key)
        # If operator-controlled scope changed while planning, do not acknowledge
        # this as successfully processed under the previously checked scope.
        self.validate()
        return result


def refuse_commercial(scope_config):
    """The env-pinned draft pilot (fans, item, price from the environment) belongs to the legacy trial pilot only."""
    if scope_config is not None and scope_config.get('contract_version') == 'commercial.v1':
        raise Refused('the draft pilot supports only the legacy trial scope')


def build_draft_pilot(store, environ=None, *, model_transport=None):
    """Return None by default; enabled startup is explicit and remains no-send."""
    env = os.environ if environ is None else environ
    flag = env.get('TWIN_PLAN_C_DRAFT_ONLY', '')
    if flag in ('', '0'):
        return None
    if flag != '1' or store is None:
        raise Refused('invalid draft pilot mode')
    tenant, account = env.get('TWIN_PLAN_C_TENANT', ''), env.get('TWIN_PLAN_C_ACCOUNT', '')
    if not _NAME.fullmatch(tenant) or not _NAME.fullmatch(account):
        raise Refused('explicit draft pilot tenant and account are required')
    fans_raw = env.get('TWIN_PLAN_C_FANS', '')
    fans = fans_raw.split(',') if fans_raw else []
    if len(fans) != len(set(fans)):
        raise Refused('duplicate draft pilot fans')
    allowed = require_allowed_chats(frozenset(fans))
    item = _number(env.get('TWIN_PLAN_C_ITEM_ID', ''), maximum=2**31-1)
    price = _number(env.get('TWIN_PLAN_C_PRICE_CENTS', ''), minimum=300, maximum=20000)
    media = _ids(env.get('TWIN_PLAN_C_MEDIA_IDS', ''))
    previews = _ids(env.get('TWIN_PLAN_C_PREVIEW_IDS', ''), allow_empty=True)
    if not set(previews).issubset(media):
        raise Refused('draft pilot previews must belong to the product')
    cfg = ModelConfig.from_env(env)
    if (not cfg.enabled or cfg.fallback_enabled or not cfg.upstream_provider or not cfg.upstream_reported_name
            or cfg.enabled_accounts != frozenset({account}) or cfg.allowed_models != (cfg.primary_model,)):
        raise Refused('draft pilot needs one configured model and upstream without fallback')
    secret = _secret(env.get('TWIN_PLAN_C_WEBHOOK_SECRET_FILE', ''))
    profiles = JsonFileCreatorProfiles(env.get('ENGINE_MODEL_PROFILES_FILE', ''))
    # Validate shared policy before loading a model credential or constructing a client.
    transport = DraftOnlyTransport(store, tenant, account, allowed_chats=allowed)
    intake = FansEventIntake(store)
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    with Session(store.engine) as session:
        store._runtime(session, tenant, account)
        policy = session.get(OfDeliveryPolicy, account)
        if policy is None or policy.transport_kind != 'fansapi' or policy.enabled is not False:
            raise Refused('draft-only requires the shared delivery policy to be disabled')
    engine = build_engine_service(store, env, transport=model_transport, draft_only=True)
    if engine is None:
        raise Refused('draft model runtime is unavailable')
    from conversation_engine.pilot_scope import current as current_pilot
    with Session(store.engine) as session, session.begin():
        runtime = store._runtime(session, tenant, account)
        pilot_scope, scope_config = current_pilot(session, runtime)
        refuse_commercial(scope_config)
        if pilot_scope is not None:
            engine.model.ledger.expected_pilot_scope = (pilot_scope.revision, pilot_scope.fingerprint)
    worker = RealConversationWorker(engine, intake, transport)
    pilot = DraftPilot(worker, tenant, account, secret, item, price, media, previews, profiles, allowed, cfg)
    pilot.validate()
    return pilot

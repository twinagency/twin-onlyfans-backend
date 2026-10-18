"""Bounded reads of Twin's existing admin API and one OF fact projection.

Twin typed facts remain authoritative. This cache cannot write Twin, compile a
persona, change a model, create fan memory or send. Explicit OF overrides win;
freshness is checked again with the saved context before native egress.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import os
import re
import uuid
from urllib.parse import urlencode, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import AccountAiConfig, OfCreatorBinding
from db.onlyfans_creator_facts_models import OfCreatorFactsSource
from .native_context import canonical
from .native_protocol import load
from .store import Refused, utcnow

SOURCE_COLUMNS = ("id", "updated_at", "date_of_birth", "hometown", "current_city", "cover_story",
    "occupation", "relationship_canon", "tattoos", "tattoo_details", "turn_ons", "hard_limits", "hard_limits_extra")
FACT_KEYS = ("age", "job", "home_city", "relationship", "kids", "family", "tattoos", "kinks", "limits")
REFRESH_SECONDS = 30
MAX_AGE_SECONDS = 120


def object_value(raw):
    if raw is None:
        return {}
    from .configuration import object_value as existing
    return existing(raw)


def map_facts(source, *, today=None):
    """Only explicit typed data. Unknown is not converted into a negative fact."""
    today = today or datetime.now(timezone.utc).date()
    result = {}
    birth = source.get("date_of_birth")
    if birth:
        try:
            born = date.fromisoformat(birth)
            age = today.year-born.year-((today.month, today.day) < (born.month, born.day))
            if not 18 <= age <= 120:
                raise ValueError()
            result["age"] = str(age)
        except (ValueError, TypeError):
            raise Refused("shared creator date of birth needs review") from None
    job = source.get("cover_story") or source.get("occupation")
    if job:
        result["job"] = job
    locations = [label + ": " + source[key] for key, label in (("hometown", "Hometown"), ("current_city", "Current city")) if source.get(key)]
    if locations:
        result["home_city"] = "; ".join(locations)
    relationship = source.get("relationship_canon")
    if relationship in {"single", "taken", "complicated"}:
        result["relationship"] = {"single":"Single", "taken":"In a relationship", "complicated":"Complicated"}[relationship]
    if source.get("tattoos") is False:
        result["tattoos"] = "No tattoos"
    elif source.get("tattoos") is True:
        result["tattoos"] = "; ".join(["Has tattoos", *source.get("tattoo_details", [])])
    if source.get("turn_ons"):
        result["kinks"] = "; ".join(source["turn_ons"])
    limits = [*source.get("hard_limits", []), source.get("hard_limits_extra") or ""]
    if any(limits):
        result["limits"] = "; ".join(value for value in limits if value)
    # Kids/family are not inferred from unrelated intake or relationship data.
    return result


def validate_source(value, creator_id):
    if type(value) is not dict or set(value) != set(SOURCE_COLUMNS) or value["id"] != creator_id:
        raise Refused("shared creator response scope unavailable")
    for key in SOURCE_COLUMNS:
        item = value[key]
        if key in {"tattoo_details", "turn_ons", "hard_limits"}:
            if item is None:
                value[key] = []
            elif type(item) is not list or len(item) > 50 or any(type(part) is not str or len(part) > 240 or "\x00" in part for part in item):
                raise Refused("unsupported shared creator list")
        elif key == "tattoos":
            if item is not None and type(item) is not bool:
                raise Refused("unsupported shared creator boolean")
        elif item is not None and (type(item) is not str or len(item) > 2000 or "\x00" in item):
            raise Refused("unsupported shared creator text")
    if value["updated_at"] is None:
        raise Refused("shared creator version unavailable")
    from .text_delivery import _stamp
    stamp = _stamp(value["updated_at"])
    map_facts(value)
    return value, stamp


@dataclass(frozen=True)
class TwinFactsReader:
    base_url: str
    token: str = field(repr=False)

    def __post_init__(self):
        target = urlsplit(self.base_url)
        local = target.hostname in {"localhost", "127.0.0.1", "::1"}
        try: local = local or ipaddress.ip_address(target.hostname) in ipaddress.ip_network("100.64.0.0/10")
        except (ValueError, TypeError):pass
        if (target.scheme not in {"http", "https"} or not target.hostname or target.username or target.password
                or target.query or target.fragment or target.path not in {"", "/"}
                or target.scheme == "http" and not local
                or type(self.token) is not str or not 32 <= len(self.token) <= 4096
                or not self.token.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in self.token)):
            raise Refused("explicit private Twin source configuration required")

    def read(self, creator_id):
        from onlyfans_read.contract import ID_PATTERN
        if os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker" or type(creator_id) is not str or not ID_PATTERN.fullmatch(creator_id):
            raise Refused("invalid shared creator read scope")
        # Twin flags a result as truncated when rows == limit. Fetch two
        # slots for this unique ID so its single owned row is unambiguous.
        query = urlencode({"table":"clones", "select":",".join(SOURCE_COLUMNS), "filter":"id:eq:"+creator_id, "limit":"2"})
        url = self.base_url.rstrip("/") + "/api/admin/v1/query?" + query
        try:
            raw = self._get_once(url)
            body = load(raw)
            if (type(body.get("rowCount")) is not int or body.get("ok") is not True or body.get("version") != "v1" or body.get("table") != "clones"
                    or body.get("rowCount") != 1 or body.get("truncated") is not False
                    or type(body.get("rows")) is not list or len(body["rows"]) != 1):
                raise ValueError()
            return validate_source(body["rows"][0], creator_id)
        except Exception:
            raise Refused("shared creator source unavailable") from None

    def _get_once(self, url):
        import httpx
        raw = bytearray()
        with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
            with client.stream("GET", url, headers={"Authorization":"Bearer "+self.token, "Accept":"application/json", "Accept-Encoding":"identity"}) as response:
                if response.status_code != 200 or response.headers.get("content-type", "").split(";")[0].strip() != "application/json":
                    raise Refused("shared creator source unavailable")
                for chunk in response.iter_bytes():
                    if len(raw)+len(chunk) > 65536:
                        raise Refused("shared creator response exceeds its budget")
                    raw.extend(chunk)
        return bytes(raw)


def effective(session, row, *, now=None):
    """The same effective facts and state feed Settings, planning and egress."""
    now = now or utcnow()
    source = session.get(OfCreatorFactsSource, row.account_id)
    from db.onlyfans_models import OfRuntime
    runtime = session.get(OfRuntime, row.account_id)
    if source is not None and (runtime is None or source.binding_id != runtime.binding_id):
        raise Refused("shared creator binding changed")
    required = source is not None or os.environ.get("ONLYFANS_TWIN_FACTS_ENABLED") == "1"
    if source is None:
        return object_value(row.persona_facts_json), None if not required else {"state":"unavailable", "sourceHash":None}
    current = (source.checked_at is not None and source.source_json is not None
        and timedelta(0) <= now-source.checked_at <= timedelta(seconds=MAX_AGE_SECONDS))
    values = map_facts(object_value(source.source_json), today=now.date()) if source.source_json else {}
    overrides = object_value(source.overrides_json)
    values.update(overrides)
    return {key:value for key,value in values.items() if value is not None}, {
        "state":"current" if current else "stale" if source.checked_at else "unavailable", "sourceHash":source.source_hash,
        "overrides":overrides, "bindingId":source.binding_id}


def source_view(session, row):
    facts, state = effective(session, row)
    source = session.get(OfCreatorFactsSource, row.account_id)
    return facts, {"state":state["state"] if state else "unconfigured",
        "checkedAt":source.checked_at.isoformat()+"Z" if source and source.checked_at else None,
        "sourceUpdatedAt":source.source_updated_at.isoformat()+"Z" if source and source.source_updated_at else None,
        "overrideKeys":[key for key in FACT_KEYS if key in object_value(source.overrides_json if source else row.persona_facts_json)]}


def refresh(store, tenant, account, reader):
    """One leased, account-bound source GET outside the database transaction."""
    if type(reader) is not TwinFactsReader or os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker":
        raise Refused("explicit shared creator reader required")
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        binding = s.get(OfCreatorBinding, runtime.binding_id)
        row, now = s.get(AccountAiConfig, account), utcnow()
        if row is None or binding is None:raise Refused("existing creator configuration required")
        source = s.get(OfCreatorFactsSource, account)
        if source is not None and source.binding_id != runtime.binding_id:
            raise Refused("shared creator binding changed")
        if source is not None and (source.refresh_until and source.refresh_until > now
                or source.attempted_at and now-source.attempted_at < timedelta(seconds=REFRESH_SECONDS)):
            return
        if source is None:
            source = OfCreatorFactsSource(account_id=account, binding_id=runtime.binding_id,
                overrides_json=canonical(object_value(row.persona_facts_json)))
            s.add(source)
        token = str(uuid.uuid4())
        source.refresh_token, source.refresh_until, source.attempted_at = token, now+timedelta(seconds=15), now
        creator = binding.creator_id
    error = None
    try:
        value, version = reader.read(creator)
    except Exception:
        error = "shared_creator_unavailable"
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        source, row = s.get(OfCreatorFactsSource, account), s.get(AccountAiConfig, account)
        if source is None or source.refresh_token != token or source.binding_id != runtime.binding_id:
            return
        source.refresh_token = source.refresh_until = None
        if error:
            source.last_error = error
            return
        if source.source_updated_at is not None and version < source.source_updated_at:
            source.last_error = "shared_creator_version_regressed"
            return
        # Version timestamps describe ordering; unrelated Twin edits must not
        # invalidate a native plan when the actual facts have not changed.
        body = {key:value[key] for key in SOURCE_COLUMNS if key != "updated_at"}
        digest = hashlib.sha256(canonical(body).encode()).hexdigest()
        from .configuration import fingerprint
        from db.onlyfans_models import OfConfigurationChange
        before, before_fingerprint = row.persona_facts_json, fingerprint(row)
        previous_revision = runtime.configuration_revision
        source.source_json, source.source_hash, source.source_updated_at = canonical(body), digest, version
        source.checked_at, source.last_error = utcnow(), None
        facts, _ = effective(s, row)
        row.persona_facts_json = canonical(facts) if facts else None
        if before != row.persona_facts_json:
            runtime.configuration_revision += 1
            row.updated_at = utcnow()
            s.flush()
            key = hashlib.sha256(canonical(["twin-source", runtime.binding_id, digest, previous_revision]).encode()).hexdigest()
            s.add(OfConfigurationChange(account_id=account, command_key=key, previous_revision=previous_revision,
                revision=runtime.configuration_revision, fingerprint_before=before_fingerprint,
                fingerprint_after=fingerprint(row), request_hash=digest, actor="twin-source:"+creator,
                before_json=canonical({"creatorFacts":object_value(before)}),
                after_json=canonical({"creatorFacts":facts,"sourceHash":digest}), created_at=utcnow()))

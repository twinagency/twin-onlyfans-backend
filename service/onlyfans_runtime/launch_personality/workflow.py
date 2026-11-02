"""Durable import/review/approval over the existing configuration authority.

Approval applies configuration in the same database transaction as its evidence.
It does not enable an account, create outgoing work or construct a provider.
All account access uses Store._runtime and its current binding/agency fence.
"""
import copy
import json
import re
from types import SimpleNamespace

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from db.models import AccountAiConfig, Message, OfCreatorBinding
from db.onlyfans_models import OfIngress
from db.onlyfans_creator_facts_models import OfCreatorFactsSource
from onlyfans_read.agency_scope import actor_allowed
from onlyfans_runtime.configuration import (
    InvalidConfiguration, digest, fact_fields, fingerprint, update_configuration, validate_patch,
)
from onlyfans_runtime.creator_facts import effective
from onlyfans_runtime.native_context import canonical
from onlyfans_runtime.store import Refused, utcnow
from .importer import build_draft
from .models import PersonalityHead, PersonalityRevision
from .policy import conflicts, readiness

VERSION = "of-personality.v1"
REVIEW_FIELDS = {"persona", "creatorFacts", "defaultModel", "replyModelOverride", "onboarding"}
ACKS = {"mixedAuthorship", "runtimeRules", "factsVerified"}


def _identity(expected, command, actor):
    if (type(expected) is not int or not 0 <= expected < 2**31-1
            or type(command) is not str or re.fullmatch(r"[a-f0-9]{64}", command) is None
            or not actor_allowed(actor)):
        raise InvalidConfiguration("A valid review revision, command and managing actor are required.")


def _review(value):
    if type(value) is not dict or not value or not set(value) <= REVIEW_FIELDS:
        raise InvalidConfiguration("Review only personality, supported facts and existing model choices.")
    result = validate_patch({k:v for k,v in value.items() if k != "onboarding"})
    if "onboarding" in value:
        from onlyfans_runtime.creator_onboarding import validate
        result["onboarding"] = validate(value["onboarding"])
    return result


def _annotate(draft):
    for instruction in draft["operationalInstructions"]:
        instruction["conflicts"] = conflicts(instruction["text"])
        instruction["includedInConfiguration"] = False
    return draft


class PersonalityWorkflow:
    def __init__(self, store, *, credential_status_reader=None):
        self.store = store
        self.credential_status_reader = credential_status_reader

    def _scope(self, s, tenant, account):
        runtime = self.store._runtime(s, tenant, account)
        config = s.scalar(select(AccountAiConfig).where(AccountAiConfig.account_id == account).with_for_update())
        if config is None:
            raise Refused("configuration unavailable")
        return runtime, config

    @staticmethod
    def _unverified_fact_keys(s, runtime, config):
        """Account-level legacy facts do not establish a different creator's biography."""
        binding = s.get(OfCreatorBinding, runtime.binding_id)
        prior_creator = s.scalar(select(OfCreatorBinding.id).where(
            OfCreatorBinding.account_id == runtime.account_id,
            OfCreatorBinding.id != runtime.binding_id,
            OfCreatorBinding.creator_id != binding.creator_id).limit(1))
        source = s.get(OfCreatorFactsSource, runtime.account_id)
        if prior_creator is None or source is not None and source.binding_id == runtime.binding_id:
            return []
        approved = s.scalar(select(PersonalityRevision.revision).where(
            PersonalityRevision.account_id == runtime.account_id,
            PersonalityRevision.binding_id == runtime.binding_id,
            PersonalityRevision.state == "approved",
            PersonalityRevision.configuration_fingerprint == fingerprint(config)).limit(1))
        if approved is not None:
            return []
        from onlyfans_runtime.configuration import object_value
        return sorted(set(object_value(config.persona_facts_json)) & set(dict(fact_fields())))

    @staticmethod
    def _head(s, runtime):
        head = s.get(PersonalityHead, runtime.account_id)
        if head is not None and head.binding_id != runtime.binding_id:
            # An explicit new import may replace the head binding. Reads of the
            # old owner's draft and attempts to approve it remain unavailable.
            return head, None
        row = s.get(PersonalityRevision, (runtime.account_id, head.revision)) if head else None
        return head, row

    @staticmethod
    def _configuration_drift(s, row, runtime, config):
        """Read immutable settings changes; a broken chain stays explicitly unverified."""
        from db.onlyfans_models import OfConfigurationChange
        changes = s.scalars(select(OfConfigurationChange).where(
            OfConfigurationChange.account_id == runtime.account_id,
            OfConfigurationChange.revision > row.configuration_revision,
            OfConfigurationChange.revision <= runtime.configuration_revision)
            .order_by(OfConfigurationChange.revision).limit(201)).all()
        revision, fingerprint_value = row.configuration_revision, row.configuration_fingerprint
        complete, result = True, []
        for change in changes[:200]:
            if change.previous_revision != revision or change.fingerprint_before != fingerprint_value:
                complete = False
            revision, fingerprint_value = change.revision, change.fingerprint_after
            result.append({"revision": change.revision, "actor": change.actor,
                "recordedAt": change.created_at.isoformat() + "Z",
                "before": json.loads(change.before_json), "after": json.loads(change.after_json)})
        complete = bool(complete and len(changes) <= 200 and revision == runtime.configuration_revision and fingerprint_value == fingerprint(config))
        return {"fromRevision": row.configuration_revision, "toRevision": runtime.configuration_revision,
                "auditComplete": complete, "changes": result}

    def _view(self, s, row, runtime, config, credentials=None):
        document = json.loads(row.document_json)
        from onlyfans_runtime.creator_onboarding import issues
        onboarding = (document.get("review") or {}).get("onboarding")
        document["onboardingReviewIssues"] = issues(onboarding) if onboarding else []
        current_facts, source = effective(s, config)
        unverified_keys = self._unverified_fact_keys(s, runtime, config)
        if unverified_keys:
            current_facts = {}
        proposed = document.get("review", {})
        preview = {c.name: getattr(config, c.name) for c in config.__table__.columns}
        if "persona" in proposed:
            preview["persona"] = proposed["persona"]
        if "defaultModel" in proposed:
            preview["model"] = proposed["defaultModel"]
        if "replyModelOverride" in proposed:
            from onlyfans_runtime.configuration import object_value
            purpose = object_value(config.model_by_purpose)
            if proposed["replyModelOverride"] is None:
                purpose.pop("ai_chatter", None)
            else:
                purpose["ai_chatter"] = proposed["replyModelOverride"]
            preview["model_by_purpose"] = canonical(purpose)
        if credentials is None and self.credential_status_reader is not None:
            credentials = self.credential_status_reader(s, runtime.account_id, runtime.binding_id)
        checks = readiness(preview, credentials)
        checks["credential"]["metadataAvailable"] = credentials is not None
        if source and source["state"] != "current":
            checks["ready"] = False
            checks["blockers"].append("creator_facts_" + source["state"])
        if row.state != "approved":
            checks["ready"] = False
            checks["blockers"].append("personality_not_approved")
        elif runtime.configuration_revision != row.configuration_revision or fingerprint(config) != row.configuration_fingerprint:
            checks["ready"] = False
            checks["blockers"].append("approved_configuration_changed")
        return {"version": VERSION, "revision": row.revision, "state": row.state,
            "bindingId": row.binding_id, "document": document,
            "configurationRevision": row.configuration_revision,
            "configurationFingerprint": row.configuration_fingerprint,
            "configurationDrift": self._configuration_drift(s, row, runtime, config),
            "configurationCurrent": (runtime.configuration_revision == row.configuration_revision
                and fingerprint(config) == row.configuration_fingerprint),
            "conflicts": conflicts(proposed.get("persona", ""), proposed.get("creatorFacts")),
            "factComparisons": [{"key": key, "current": current_facts.get(key), "proposed": value,
                "changed": current_facts.get(key) != value} for key, value in proposed.get("creatorFacts", {}).items()],
            "requiresFactResolution": unverified_keys,
            "readiness": checks, "createdAt": row.created_at.isoformat() + "Z",
            "actor": row.actor, "commandKey": row.command_key}

    def get(self, tenant, account, *, revision=None, credential_status=None):
        if revision is not None and (type(revision) is not int or revision <= 0):
            raise InvalidConfiguration("A positive personality revision is required.")
        with Session(self.store.engine) as s, s.begin():
            runtime, config = self._scope(s, tenant, account)
            _, row = self._head(s, runtime)
            if revision is not None:
                row = s.get(PersonalityRevision, (account, revision))
            if row is None or row.binding_id != runtime.binding_id:
                if revision is not None:
                    raise Refused("personality unavailable")
                head = s.get(PersonalityHead, account)
                return {"version": VERSION, "revision": head.revision if head else 0,
                    "state": "empty", "bindingId": runtime.binding_id, "document": None}
            return self._view(s, row, runtime, config, credential_status)

    def history(self, tenant, account, *, before=None):
        if before is not None and (type(before) is not int or before <= 0):
            raise InvalidConfiguration("A positive history cursor is required.")
        with Session(self.store.engine) as s, s.begin():
            runtime, _ = self._scope(s, tenant, account)
            query = select(PersonalityRevision).where(PersonalityRevision.account_id == account,
                PersonalityRevision.binding_id == runtime.binding_id)
            if before is not None:
                query = query.where(PersonalityRevision.revision < before)
            rows = s.scalars(query.order_by(PersonalityRevision.revision.desc()).limit(21)).all()
            return {"revisions": [{"revision": r.revision, "state": r.state, "actor": r.actor,
                "createdAt": r.created_at.isoformat() + "Z"} for r in rows[:20]],
                "nextBefore": rows[19].revision if len(rows) > 20 else None}

    def _change(self, tenant, account, expected_revision, command_key, actor, request, build, *, expected_binding_id=None):
        _identity(expected_revision, command_key, actor)
        if expected_binding_id is not None:
            if type(expected_binding_id) is not str or not 1 <= len(expected_binding_id) <= 128:
                raise InvalidConfiguration("A valid displayed binding is required.")
            request = {**request, "expectedBindingId": expected_binding_id}
        request_hash = digest({"expectedRevision": expected_revision, **request})
        with Session(self.store.engine) as s, s.begin():
            runtime, config = self._scope(s, tenant, account)
            # Compare after the existing runtime/binding lock, before replay,
            # source reads or configuration writes. A same-agency rebind must
            # not turn an old browser command into a command for a new creator.
            if expected_binding_id is not None and runtime.binding_id != expected_binding_id:
                raise Refused("personality binding conflict")
            prior = s.scalar(select(PersonalityRevision).where(PersonalityRevision.account_id == account,
                PersonalityRevision.command_key == command_key))
            if prior is not None:
                if (prior.binding_id != runtime.binding_id or prior.actor != actor or prior.request_hash != request_hash):
                    raise Refused("personality idempotency conflict")
                return self._view(s, prior, runtime, config)
            head, previous = self._head(s, runtime)
            if (head.revision if head else 0) != expected_revision:
                raise Refused("personality revision conflict")
            document, state = build(s, runtime, config, previous)
            if len(canonical(document).encode()) > 262144:
                raise InvalidConfiguration("The personality review exceeds its evidence budget.")
            revision = expected_revision + 1
            if head is None:
                head = PersonalityHead(account_id=account, binding_id=runtime.binding_id, revision=revision)
                s.add(head)
            else:
                head.binding_id, head.revision = runtime.binding_id, revision
            s.flush()
            # A nested configuration Session may have updated the same row.
            s.expire(config)
            s.expire(runtime)
            row = PersonalityRevision(account_id=account, revision=revision, previous_revision=expected_revision,
                binding_id=runtime.binding_id, state=state, command_key=command_key,
                request_hash=request_hash, actor=actor, document_json=canonical(document),
                configuration_revision=runtime.configuration_revision, configuration_fingerprint=fingerprint(config),
                created_at=utcnow())
            s.add(row)
            s.flush()
            return self._view(s, row, runtime, config)

    def import_draft(self, tenant, account, *, profile, messages, expected_revision, command_key, actor):
        """Trusted offline export seam; not an HTTP arbitrary-profile endpoint."""
        draft = _annotate(build_draft(profile, messages))
        def build(s, runtime, config, previous):
            binding = s.get(OfCreatorBinding, runtime.binding_id)
            if (profile["accountId"], profile["bindingId"], profile["creatorId"]) != (account, runtime.binding_id, binding.creator_id):
                raise Refused("personality source scope unavailable")
            return {"draft": draft, "review": {}, "source": "authorized_export"}, "draft"
        return self._change(tenant, account, expected_revision, command_key, actor,
            {"action": "import", "profile": profile, "messages": messages}, build)

    def import_existing(self, tenant, account, *, expected_revision, command_key, actor, expected_binding_id=None):
        """Read bounded canonical history, never poll a live account.

        Only messages with applied ingress evidence under the current binding
        qualify. No unverified legacy row is relabelled as creator-authored.
        """
        def build(s, runtime, config, previous):
            binding = s.get(OfCreatorBinding, runtime.binding_id)
            facts, source = effective(s, config)
            unverified_keys = self._unverified_fact_keys(s, runtime, config)
            if unverified_keys:
                facts = {}
            fields = {key: value for key, value in facts.items() if key in dict(fact_fields())}
            profile = {"accountId": account, "bindingId": runtime.binding_id, "creatorId": binding.creator_id,
                "sourceId": "saved-facts:" + fingerprint(config), "fields": fields}
            # Bound provenance first using the existing account/status/time
            # ingress index, then join message primary keys. A correlated
            # per-message ingress scan would scale badly without a shared index.
            proof = select(OfIngress.fan_id, OfIngress.message_id).where(
                OfIngress.account_id == account, OfIngress.binding_id == runtime.binding_id,
                OfIngress.status == "applied").order_by(
                    OfIngress.received_at.desc(), OfIngress.event_key.desc()).limit(10000).cte("personality_evidence")
            from onlyfans_runtime.history_policy import policy, approved_onboarding
            recent_policy = policy(s, account, runtime.binding_id)
            sample_limit = (recent_policy.writing_sample_limit if recent_policy else 5001)
            approved = approved_onboarding(s, account, runtime.binding_id) if recent_policy else None
            if approved and approved.get("writingStyle", "").strip(): sample_limit = 0
            if not 0 <= sample_limit <= 100: sample_limit = 100 if recent_policy else 5001
            rows = s.execute(select(Message.fan_id, Message.message_id, Message.body, Message.created_at,
                Message.automation_kind, Message.sent_by_employee_id).join(proof,
                    and_(proof.c.fan_id == Message.fan_id, proof.c.message_id == Message.message_id))
                .where(Message.account_id == account, Message.direction == "out",
                    Message.is_unsent.is_(False), Message.body != "").distinct()
                .order_by(Message.created_at.desc(), Message.message_id.desc(), Message.fan_id.desc()).limit(sample_limit)).all()
            messages, skipped, omitted, used_bytes = [], 0, 0, 0
            for m in rows[:5000]:
                if len(m.body.encode()) > 8000 or not m.body.strip():
                    skipped += 1
                    continue
                candidate = {"accountId": account, "bindingId": runtime.binding_id,
                    "sourceId": "message:" + digest([account, runtime.binding_id, m.fan_id, m.message_id]),
                    "direction": "out", "body": m.body,
                    "author": "automation" if m.automation_kind else "agency" if m.sent_by_employee_id else "unknown",
                    "fanKey": digest([account, runtime.binding_id, m.fan_id]),
                    "occurredAt": m.created_at.isoformat() + "Z"}
                size = len(canonical(candidate).encode())
                if used_bytes + size > 3 * 1024 * 1024:
                    omitted += 1
                    continue
                messages.append(candidate)
                used_bytes += size
            draft = _annotate(build_draft(profile, messages))
            draft["coverage"].update({"historyWindow": "explicit_bounded_style_sample" if recent_policy else "up_to_5000_outgoing_from_latest_10000_current_binding_ingress",
                "configuredWritingSampleLimit":sample_limit,
                "ingressWindowLimit": 10000, "fullHistoryCoverage": False,
                "olderRowsNotScanned": len(rows) > 5000, "excludedOversizeOrBlank": skipped,
                "omittedForByteBudget": omitted,
                "unverifiedPriorCreatorFactKeys": unverified_keys,
                "profileSource": "effective_saved_creator_facts", "sharedFactsState": source["state"] if source else "unconfigured"})
            return {"draft": draft, "review": {}, "source": "canonical_history"}, "draft"
        return self._change(tenant, account, expected_revision, command_key, actor, {"action": "import_existing"}, build,
                            expected_binding_id=expected_binding_id)

    def edit(self, tenant, account, *, review, expected_revision, command_key, actor, expected_binding_id=None):
        review = _review(review)
        def build(s, runtime, config, previous):
            if previous is None:
                raise Refused("personality unavailable")
            if previous.state != "approved" and (previous.configuration_revision != runtime.configuration_revision
                    or previous.configuration_fingerprint != fingerprint(config)):
                raise Refused("configuration revision conflict")
            document = copy.deepcopy(json.loads(previous.document_json))
            # Full review replacement, avoiding ambiguous partial clear semantics.
            document["review"] = review
            document.pop("approval", None)
            return document, "draft"
        return self._change(tenant, account, expected_revision, command_key, actor,
            {"action": "edit", "review": review}, build, expected_binding_id=expected_binding_id)

    def approve(self, tenant, account, *, acknowledgements, expected_revision, command_key, actor, expected_binding_id=None):
        if type(acknowledgements) is not dict or set(acknowledgements) != ACKS or any(v is not True for v in acknowledgements.values()):
            raise InvalidConfiguration("Review authorship, creator facts and runtime rules before approval.")
        def build(s, runtime, config, previous):
            if previous is None or previous.state != "draft":
                raise Refused("a reviewed draft is required")
            if (previous.configuration_revision != runtime.configuration_revision
                    or previous.configuration_fingerprint != fingerprint(config)):
                raise Refused("configuration revision conflict")
            document = copy.deepcopy(json.loads(previous.document_json))
            patch = _review(document["review"])
            if "persona" not in patch:
                raise InvalidConfiguration("Explicitly review and save the complete personality before approval.")
            if conflicts(patch["persona"], patch.get("creatorFacts")):
                raise InvalidConfiguration("Resolve the imported instruction conflicts before approval.")
            if not set(self._unverified_fact_keys(s, runtime, config)) <= set(patch.get("creatorFacts", {})):
                raise InvalidConfiguration("Explicitly replace or clear prior-creator fact fields before approval.")
            # Join the existing writer to THIS transaction. Session.commit in
            # update_configuration cannot commit an externally-owned Connection.
            # This preserves its checks, merges, audit and invalidation exactly.
            onboarding = patch.pop("onboarding", None)
            if onboarding is not None:
                from onlyfans_runtime.creator_onboarding import issues
                if issues(onboarding):
                    raise InvalidConfiguration("Resolve onboarding placeholders and content-permission conflicts before approval.")
                document["approvedOnboarding"] = onboarding
            config_key = digest([VERSION, account, runtime.binding_id, command_key])
            try:
                # A savepoint isolates the existing writer's no-change refusal
                # while preserving the single outer approval transaction.
                with s.begin_nested():
                    bound_store = SimpleNamespace(engine=s.connection(), _runtime=self.store._runtime)
                    result = update_configuration(bound_store, tenant, account, patch=patch,
                        expected_revision=previous.configuration_revision,
                        expected_fingerprint=previous.configuration_fingerprint,
                        command_key=config_key, actor=actor)
            except InvalidConfiguration as exc:
                if str(exc) != "No configuration change was needed.":
                    raise
                result = (previous.configuration_revision, previous.configuration_fingerprint)
            document["approval"] = {"acknowledgements": acknowledgements,
                "configurationCommandKey": config_key, "appliedConfigurationRevision": result[0]}
            return document, "approved"
        return self._change(tenant, account, expected_revision, command_key, actor,
            {"action": "approve", "acknowledgements": acknowledgements}, build, expected_binding_id=expected_binding_id)

"""Bounded operator edits to AccountAiConfig, with immutable change evidence."""
import hashlib
import json
import re

from onlyfans_read.agency_scope import actor_allowed
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import AccountAiConfig
from db.onlyfans_models import OfConfigurationChange
from llm_models import MODELS
from .native_context import canonical,approved_persona
from .store import Refused,utcnow
from .activation import activation_view
from .contextual_mode import FIELD as CONTEXTUAL_FIELD, KEY as CONTEXTUAL_KEY, MODES, mode as contextual_mode
from .safety_mode import FIELD as SAFETY_FIELD, KEY as SAFETY_KEY, MODES as SAFETY_MODES, mode as safety_mode
from .reply_settings import SWITCHES as REPLY_SWITCHES, enabled as reply_enabled

def fact_fields():
    from automations._persona_contract import PERSONA_FACT_FIELDS
    return PERSONA_FACT_FIELDS


class InvalidConfiguration(ValueError):
    pass


SALES_FLAGS = ("pack_send_enabled", "pack_on_ask_enabled", "qualification_gate_enabled")
# FASTT runner switches, same JSON blob; default OFF, only a literal boolean true is on.
FASTT_SWITCHES = {"fasttDiscountResend": "fastt_discount_resend", "fasttPostBuyUpsell": "fastt_post_buy_upsell", **REPLY_SWITCHES}


def sales_settings(row):
    """Project only the existing native on-request controls, never private JSON."""
    try:
        raw = object_value(row.ai_chatter_config_json)
        if any(key in raw and type(raw[key]) is not bool for key in SALES_FLAGS):
            raise ValueError("invalid sales switch")
        return raw, True
    except (ValueError, TypeError, RecursionError):
        return {}, False


def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()


def on_request_sales_enabled(sales, *, valid):
    """The editor and active adapter share the explicit saved permission."""
    return valid and all(sales.get(key) is True for key in SALES_FLAGS)


def fingerprint(row):
    value={column.name:getattr(row,column.name) for column in row.__table__.columns}
    encoded=canonical(value).encode()
    if len(encoded)>262144:raise InvalidConfiguration("Saved configuration is too large for this editor.")
    return hashlib.sha256(encoded).hexdigest()


def object_value(raw):
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError("duplicate key")
            result[key]=value
        return result
    if not raw:return {}
    if type(raw) is not str or len(raw.encode())>65536:raise ValueError("configuration too large")
    value=json.loads(raw,object_pairs_hook=unique)
    if type(value) is not dict:raise ValueError("configuration object required")
    return value


def runtime_model_options(account_id):
    """Constrain only accounts already owned by the existing FASTT authority."""
    from . import fastt_scope
    from .runtime_stop import to_fastt_account_id
    account=to_fastt_account_id(account_id)
    active=fastt_scope.platform_scope_enabled()
    if not active:
        pairs,whole=fastt_scope.owned_scope()
        active=account in whole or any(owner==account for owner,_ in pairs)
    if not active:return None
    from native_pilot.connection import supports_reply_model
    return sorted(model for model in MODELS if supports_reply_model(model))


def editable(row,runtime,session,*,include_activation=False):
    """Only approved fields and catalogue IDs; no arbitrary config or credentials."""
    from automations._voice import fact_labels
    from model_selection import explicit_model
    labels=fact_labels(row.voice)
    facts_valid=True;models_valid=True
    from .creator_facts import source_view
    source={"state":"unavailable","checkedAt":None,"sourceUpdatedAt":None,"overrideKeys":[]}
    try:facts,source=source_view(session,row)
    except Refused:raise
    except (ValueError,TypeError,RecursionError):facts={};facts_valid=False
    try:
        purpose=object_value(row.model_by_purpose)
        override=purpose.get("ai_chatter")
        if override is not None and (type(override) is not str or len(override)>160):raise ValueError()
    except (ValueError,TypeError,RecursionError):override=None;models_valid=False
    purpose_valid=models_valid
    values={};readonly=[]
    for key,_ in fact_fields():
        value=facts.get(key)
        if value is not None and (type(value) is not str or len(value)>16000):value=None;readonly.append(key)
        if type(value) is str and len(value)>240 and key not in readonly:readonly.append(key)
        values[key]=value
    persona=row.persona or ""
    try:resolved=explicit_model(row,"ai_chatter",MODELS)
    except (ValueError,RecursionError):resolved=None
    options=runtime_model_options(row.account_id)
    if options is not None:
        from .connections import personality_credential_status
        credential=personality_credential_status(session,row.account_id,runtime.binding_id)
        models_valid=models_valid and resolved in options and row.reasoning_effort is None and bool(
            credential and credential.get("configured") and credential.get("modelId")==resolved)
        if not models_valid:resolved=None
    sales, sales_valid = sales_settings(row)
    return {"configurationRevision":runtime.configuration_revision,"fingerprint":fingerprint(row),
        "persona":persona if len(persona)<=4096 else None,"personaTruncated":len(persona)>4096,
        "creatorFacts":values,"factFields":[{"key":key,"label":labels.get(key,label)} for key,label in fact_fields()],
        "factsValid":facts_valid,"readOnlyFacts":readonly,"modelsValid":models_valid,"sharedFacts":source,
        "defaultModel":row.model,"replyModelOverride":override,"replyOverridePresent":purpose_valid and "ai_chatter" in purpose,"resolvedReplyModel":resolved,
        "modelOptions":sorted(MODELS) if options is None else options,
        "dailyCostCapCents":row.daily_cost_cap_cents, "reasoningEffort":row.reasoning_effort,
        "reasoningOptions":({key:list(model.effort_choices) for key,model in MODELS.items()} if options is None else {key:[] for key in options}),
        "salesConfigurationValid":sales_valid,
        CONTEXTUAL_FIELD:contextual_mode(sales) if sales_valid else "off",
        SAFETY_FIELD:safety_mode(sales) if sales_valid else "shadow",
        "onRequestSalesEnabled":on_request_sales_enabled(sales, valid=sales_valid),
        **{name:sales_valid and reply_enabled(sales,key) for name,key in FASTT_SWITCHES.items()},
        **({"activation": activation_view(session,row.account_id,sales,binding_id=runtime.binding_id,model_id=resolved)} if include_activation else {})}


def get_configuration(store,tenant_id,account_id,*,include_activation=False):
    with Session(store.engine) as s,s.begin():
        runtime=store._runtime(s,tenant_id,account_id)
        row=s.scalar(select(AccountAiConfig).where(AccountAiConfig.account_id==account_id).with_for_update(read=True))
        if row is None:raise Refused("configuration unavailable")
        return editable(row,runtime,s,include_activation=include_activation)


def validate_patch(patch):
    if type(patch) is not dict or not patch or not set(patch)<= {"persona","creatorFacts","inheritCreatorFacts","defaultModel","replyModelOverride","onRequestSalesEnabled","dailyCostCapCents","reasoningEffort","fasttScope",CONTEXTUAL_FIELD,SAFETY_FIELD,*FASTT_SWITCHES}:
        raise InvalidConfiguration("Only creator persona, facts, model, budget, reasoning and on-request sales settings may be edited here.")
    result={}
    if CONTEXTUAL_FIELD in patch:
        value = patch[CONTEXTUAL_FIELD]
        if type(value) is not str or value not in MODES:
            raise InvalidConfiguration("Contextual offers must be off, shadow, or live.")
        result[CONTEXTUAL_FIELD] = value
    if SAFETY_FIELD in patch:
        value = patch[SAFETY_FIELD]
        if type(value) is not str or value not in SAFETY_MODES:
            raise InvalidConfiguration("Conversation safety detectors must be off, shadow, or live.")
        result[SAFETY_FIELD] = value
    if "fasttScope" in patch:
        from .fastt_scope import normalize
        try: result["fasttScope"] = normalize(patch["fasttScope"])
        except ValueError as exc: raise InvalidConfiguration(str(exc)) from exc
    if "dailyCostCapCents" in patch:
        value=patch["dailyCostCapCents"]
        if type(value) is not int or not 0<=value<=1000000:
            raise InvalidConfiguration("Use a whole daily budget between 0 and 1000000 cents.")
        result["dailyCostCapCents"]=value
    if "reasoningEffort" in patch:
        value=patch["reasoningEffort"]
        if value is not None and (type(value) is not str or not value or len(value)>32):
            raise InvalidConfiguration("Choose a supported reasoning effort or the model default.")
        result["reasoningEffort"]=value
    if "onRequestSalesEnabled" in patch:
        if type(patch["onRequestSalesEnabled"]) is not bool:
            raise InvalidConfiguration("The on-request sales setting must be an explicit boolean.")
        result["onRequestSalesEnabled"]=patch["onRequestSalesEnabled"]
    for name in FASTT_SWITCHES:
        if name in patch:
            if type(patch[name]) is not bool:
                raise InvalidConfiguration("FASTT switches must be an explicit boolean.")
            result[name]=patch[name]
    if "inheritCreatorFacts" in patch:
        keys=patch["inheritCreatorFacts"]
        if (type(keys) is not list or not keys
                or any(type(key) is not str or key not in dict(fact_fields()) for key in keys)
                or len(set(keys))!=len(keys)
                or type(patch.get("creatorFacts")) is dict and set(keys)&set(patch["creatorFacts"])):
            raise InvalidConfiguration("Choose distinct supported facts to inherit from Twin.")
        result["inheritCreatorFacts"]=keys
    if "persona" in patch:
        persona=patch["persona"]
        if type(persona) is not str or len(persona)>4096 or not approved_persona(persona):
            raise InvalidConfiguration("Use a nonempty creator persona of at most 4096 characters that preserves honest AI disclosure.")
        result["persona"]=persona.strip()
    if "creatorFacts" in patch:
        facts=patch["creatorFacts"]
        if type(facts) is not dict or not facts or not set(facts)<=set(dict(fact_fields())):
            raise InvalidConfiguration("Choose a supported creator fact.")
        clean={}
        for key,value in facts.items():
            if value is not None:
                if type(value) is not str or not value.strip() or len(value.strip())>240:
                    raise InvalidConfiguration("Each creator fact must be nonempty text of at most 240 characters, or explicitly cleared.")
                value=value.strip()
                if key=="age" and (not re.fullmatch(r"[0-9]{2,3}",value) or not 18<=int(value)<=120):
                    raise InvalidConfiguration("Creator age must be an adult age between 18 and 120.")
            clean[key]=value
        result["creatorFacts"]=clean
    for field in ("defaultModel","replyModelOverride"):
        if field not in patch:continue
        value=patch[field]
        if value is None and field=="replyModelOverride":result[field]=None;continue
        if type(value) is not str or value not in MODELS:
            raise InvalidConfiguration("Choose a model from the saved native model catalogue.")
        result[field]=value
    return result


def update_configuration(store,tenant_id,account_id,*,patch,expected_revision,expected_fingerprint,command_key,actor):
    patch=validate_patch(patch)
    if (type(expected_revision) is not int or not 0<=expected_revision<2**31-1 or
        type(expected_fingerprint) is not str or not re.fullmatch(r"[a-f0-9]{64}",expected_fingerprint) or
        type(command_key) is not str or not re.fullmatch(r"[a-f0-9]{64}",command_key) or
        not actor_allowed(actor)):
        raise InvalidConfiguration("The configuration version or command identity is invalid.")
    request_hash=digest(patch)
    with Session(store.engine) as s,s.begin():
        runtime=store._runtime(s,tenant_id,account_id)
        prior=s.get(OfConfigurationChange,(account_id,command_key))
        if prior:
            if (prior.previous_revision,prior.fingerprint_before,prior.request_hash,prior.actor)!=(expected_revision,expected_fingerprint,request_hash,actor):
                raise Refused("configuration idempotency conflict")
            return prior.revision,prior.fingerprint_after
        row=s.scalar(select(AccountAiConfig).where(AccountAiConfig.account_id==account_id).with_for_update())
        if row is None:raise Refused("configuration unavailable")
        if runtime.configuration_revision!=expected_revision or fingerprint(row)!=expected_fingerprint:
            raise Refused("configuration revision conflict")
        before={};after={};changed=False
        if "fasttScope" in patch:
            from .activation import validate_selection
            try: selection = validate_selection(account_id, patch["fasttScope"])
            except ValueError as exc: raise InvalidConfiguration(str(exc)) from exc
            try: raw = object_value(row.ai_chatter_config_json)
            except (ValueError, TypeError, RecursionError) as exc: raise InvalidConfiguration("Saved automation settings need review.") from exc
            before["fasttScope"] = raw.get("fastt_scope")
            after["fasttScope"] = selection
            changed = raw.get("fastt_scope") != selection
            raw["fastt_scope"] = selection
            row.ai_chatter_config_json = canonical(raw)
        if "onRequestSalesEnabled" in patch:
            sales, valid = sales_settings(row)
            if not valid:
                raise InvalidConfiguration("Saved sales configuration needs review before it can be edited.")
            before["salesSettings"]={key:{"present":key in sales,"value":sales.get(key)} for key in SALES_FLAGS}
            previous_sales=canonical(sales)
            # Enable the existing request/qualification lane explicitly. Turning
            # it off stops offers without disabling qualification or other brakes.
            sales["pack_send_enabled"]=patch["onRequestSalesEnabled"]
            if patch["onRequestSalesEnabled"]:
                sales.update({key:True for key in SALES_FLAGS})
            after["salesSettings"]={key:{"present":key in sales,"value":sales.get(key)} for key in SALES_FLAGS}
            changed=changed or previous_sales!=canonical(sales)
            if previous_sales!=canonical(sales):
                row.ai_chatter_config_json=canonical(sales)
        if any(name in patch for name in FASTT_SWITCHES):
            sales, valid = sales_settings(row)
            if not valid:
                raise InvalidConfiguration("Saved sales configuration needs review before it can be edited.")
            before.setdefault("fasttSwitches",{});after.setdefault("fasttSwitches",{})
            previous_switches=canonical(sales)
            for name,key in FASTT_SWITCHES.items():
                if name in patch:
                    before["fasttSwitches"][key]={"present":key in sales,"value":sales.get(key)}
                    sales[key]=patch[name]
                    after["fasttSwitches"][key]={"present":True,"value":patch[name]}
                    if name == "nativeReplyPacingEnabled" and before["fasttSwitches"][key]["value"] != patch[name]:
                        from native_pilot.pacing import invalidate_settings
                        invalidate_settings(s, account_id)
            if previous_switches!=canonical(sales):
                changed=True
                row.ai_chatter_config_json=canonical(sales)
        if CONTEXTUAL_FIELD in patch:
            sales, valid = sales_settings(row)
            if not valid:
                raise InvalidConfiguration("Saved sales configuration needs review before it can be edited.")
            previous = {"present":CONTEXTUAL_KEY in sales,"value":sales.get(CONTEXTUAL_KEY)}
            before[CONTEXTUAL_FIELD] = previous
            sales[CONTEXTUAL_KEY] = patch[CONTEXTUAL_FIELD]
            after[CONTEXTUAL_FIELD] = patch[CONTEXTUAL_FIELD]
            changed = changed or previous["value"] != patch[CONTEXTUAL_FIELD]
            row.ai_chatter_config_json = canonical(sales)
        if SAFETY_FIELD in patch:
            sales, valid = sales_settings(row)
            if not valid:
                raise InvalidConfiguration("Saved sales configuration needs review before it can be edited.")
            previous = {"present":SAFETY_KEY in sales,"value":sales.get(SAFETY_KEY)}
            before[SAFETY_FIELD] = previous
            sales[SAFETY_KEY] = patch[SAFETY_FIELD]
            after[SAFETY_FIELD] = patch[SAFETY_FIELD]
            changed = changed or previous["value"] != patch[SAFETY_FIELD]
            row.ai_chatter_config_json = canonical(sales)
        if "persona" in patch:
            changed=changed or row.persona!=patch["persona"]
            before["persona"]=row.persona;after["persona"]=patch["persona"];row.persona=patch["persona"]
        if "creatorFacts" in patch or "inheritCreatorFacts" in patch:
            try:facts=object_value(row.persona_facts_json)
            except (ValueError,TypeError,RecursionError) as exc:raise InvalidConfiguration("Saved creator facts need review before they can be merged.") from exc
            previous_facts=canonical(facts)
            from db.onlyfans_creator_facts_models import OfCreatorFactsSource
            from .creator_facts import effective
            source=s.get(OfCreatorFactsSource,account_id)
            edits=patch.get("creatorFacts",{})
            inherits=patch.get("inheritCreatorFacts",[])
            before["creatorFacts"]={key:{"present":key in facts,"value":facts.get(key)} for key in [*edits,*inherits]}
            if source is not None:
                overrides=object_value(source.overrides_json)
                old_overrides=canonical(overrides)
                for key,value in edits.items():overrides[key]=value
                for key in inherits:overrides.pop(key,None)
                source.overrides_json=canonical(overrides)
                facts,_=effective(s,row)
                changed=changed or old_overrides!=source.overrides_json
                before["overrides"]=object_value(old_overrides)
                after["overrides"]=overrides
            else:
                if inherits:raise InvalidConfiguration("Synchronize Twin facts before inheriting them.")
                for key,value in edits.items():
                    if value is None:facts.pop(key,None)
                    else:facts[key]=value
            after["creatorFacts"]={key:{"present":key in facts,"value":facts.get(key)} for key in [*edits,*inherits]}
            row.persona_facts_json=canonical(facts) if facts else None
            changed=changed or canonical(facts)!=previous_facts
        if "defaultModel" in patch:
            changed=changed or row.model!=patch["defaultModel"]
            before["defaultModel"]=row.model;after["defaultModel"]=patch["defaultModel"];row.model=patch["defaultModel"]
        if "replyModelOverride" in patch:
            try:purpose=object_value(row.model_by_purpose)
            except (ValueError,TypeError,RecursionError) as exc:raise InvalidConfiguration("Saved model-purpose configuration needs review before it can be merged.") from exc
            previous_purpose=canonical(purpose)
            before["replyModelOverride"]={"present":"ai_chatter" in purpose,"value":purpose.get("ai_chatter")}
            after["replyModelOverride"]={"present":patch["replyModelOverride"] is not None,"value":patch["replyModelOverride"]}
            if patch["replyModelOverride"] is None:purpose.pop("ai_chatter",None)
            else:purpose["ai_chatter"]=patch["replyModelOverride"]
            row.model_by_purpose=canonical(purpose) if purpose else None
            changed=changed or canonical(purpose)!=previous_purpose
        if "dailyCostCapCents" in patch:
            value=patch["dailyCostCapCents"]
            before["dailyCostCapCents"]=row.daily_cost_cap_cents
            after["dailyCostCapCents"]=value
            changed=changed or row.daily_cost_cap_cents!=value
            row.daily_cost_cap_cents=value
        if "reasoningEffort" in patch:
            value=patch["reasoningEffort"]
            if value is not None:
                from model_selection import explicit_model
                try:model=MODELS[explicit_model(row,"ai_chatter",MODELS)]
                except (ValueError,RecursionError) as exc:
                    raise InvalidConfiguration("Choose a supported model before its reasoning effort.") from exc
                if value not in model.effort_choices:
                    raise InvalidConfiguration("That reasoning effort is not supported by the selected model.")
            before["reasoningEffort"]=row.reasoning_effort
            after["reasoningEffort"]=value
            changed=changed or row.reasoning_effort!=value
            row.reasoning_effort=value
        if "defaultModel" in patch or "replyModelOverride" in patch:
            from model_selection import explicit_model
            try:explicit_model(row,"ai_chatter",MODELS)
            except (ValueError,RecursionError) as exc:raise InvalidConfiguration("The resulting saved reply model must resolve to a supported explicit choice.") from exc
        options=runtime_model_options(account_id)
        if options is not None:
            if patch.get("reasoningEffort") is not None:
                raise InvalidConfiguration("This account’s reply runtime requires reasoning to remain off.")
            if "defaultModel" in patch or "replyModelOverride" in patch:
                from model_selection import explicit_model
                from .connections import personality_credential_status
                resolved=explicit_model(row,"ai_chatter",MODELS)
                if any(patch.get(field) is not None and patch[field] not in options for field in ("defaultModel","replyModelOverride")) or resolved not in options:
                    raise InvalidConfiguration("Choose a reply model supported by this account’s active runtime.")
                credential=personality_credential_status(s,account_id,runtime.binding_id)
                if not credential or not credential.get("configured") or credential.get("modelId")!=resolved:
                    raise InvalidConfiguration("A verified model credential matching this reply model is required before saving it.")
        if not changed:raise InvalidConfiguration("No configuration change was needed.")
        row.updated_at=utcnow()
        s.flush()
        after_fingerprint=fingerprint(row)
        runtime.configuration_revision+=1
        s.add(OfConfigurationChange(account_id=account_id,command_key=command_key,
            previous_revision=expected_revision,revision=runtime.configuration_revision,
            fingerprint_before=expected_fingerprint,fingerprint_after=after_fingerprint,request_hash=request_hash,
            actor=actor,before_json=canonical(before),after_json=canonical(after),created_at=utcnow()))
        return runtime.configuration_revision,after_fingerprint

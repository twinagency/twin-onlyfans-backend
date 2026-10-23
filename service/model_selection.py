"""Pure explicit saved-model selection shared by native runtime and automation."""
import json


def explicit_model(cfg, purpose, models, *, override=None):
    if override is not None or cfg is None:
        raise ValueError("an explicit saved model is required")
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError("duplicate model-purpose key")
            result[key]=value
        return result
    try:
        by_purpose=json.loads(cfg.model_by_purpose,object_pairs_hook=unique) if cfg.model_by_purpose else {}
        if type(by_purpose) is not dict:raise ValueError("model-purpose configuration must be an object")
        chosen=by_purpose[purpose] if purpose in by_purpose else cfg.model
    except (ValueError,TypeError) as exc:
        raise ValueError("invalid saved model configuration") from exc
    if type(chosen) is not str or not chosen.strip() or chosen not in models:
        raise ValueError("saved model is missing or unsupported")
    return chosen

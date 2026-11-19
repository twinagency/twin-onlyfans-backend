"""Search text for vault media after operator corrections."""

import json

from sqlalchemy import case, cast, func, literal
from sqlalchemy.dialects.postgresql import JSONB

from jsonsafe import load_json
from vault_ai_effective import normalize_tags


TAXONOMY_FIELDS = (
    "acts", "body_focus", "toys", "clothing_items", "beats", "clothing_state",
    "penetration", "position", "setting", "camera", "primary_folder",
)


def build_search_text(item, changes):
    """Rebuild the cache from the values that will be saved on this row."""
    def value(field):
        return changes.get(field, getattr(item, field, None))

    fields = load_json(value("ai_fields_json"), {}) or {}
    if not isinstance(fields, dict):
        fields = {}
    metadata = load_json(value("operator_overrides_json"), {}) or {}
    tags = normalize_tags(load_json(value("tags"), []))
    descriptions = [value("description")]
    if "description" not in metadata:
        descriptions.append(value("video_description"))
    parts = [*descriptions, *tags, value("notes")]
    parts.extend(str(fields[key]) for key in TAXONOMY_FIELDS if fields.get(key))
    parts.extend(load_json(value("of_terms"), []) or [])
    return " ".join(str(part) for part in parts if part).lower()


def corrected_search_text(model, dialect, fallback):
    """Read corrected rows from their projected fields, including older edits.

    The cached blob and full raw AI response can both contain superseded text.
    Preserve independent taxonomy without reading the raw description or tags.
    """
    metadata = func.coalesce(model.operator_overrides_json, "")
    tags_corrected = metadata.contains('"tags"')
    description_corrected = metadata.contains('"description"')
    ai = model.ai_fields_json
    if dialect == "postgresql":
        terms = [cast(ai, JSONB)[key].astext for key in TAXONOMY_FIELDS]
    else:
        valid_ai = case((func.json_valid(ai) == 1, ai), else_="{}")
        terms = [func.json_extract(valid_ai, f"$.{key}") for key in TAXONOMY_FIELDS]

    parts = [
        model.description,
        case((description_corrected, ""), else_=model.video_description),
        model.tags,
        model.notes,
        model.of_terms,
        *terms,
    ]
    corrected = literal("")
    for part in parts:
        corrected = corrected + " " + func.coalesce(cast(part, model.search_text.type), "")
    return case((tags_corrected | description_corrected, corrected), else_=fallback)


def exact_tag_filter(model, tag):
    """Use an entire serialized tag, never a substring of search text."""
    return model.tags.contains(json.dumps(tag.strip().lower()), autoescape=True)

from types import SimpleNamespace as NS
from automations.product_readiness import item_readiness, meaningful_description, ready_ids, item_price_cents

A = "acct-x"


def item(**kw):
    base = dict(account_id=A, media_id=11, kind="photo", removed_at=None, updated_at_of="2026-10-18T10:00:00Z",
                content_hash=None, search_text="photo", description="Blonde woman in a red dress on a balcony",
                video_description=None, default_price_cents=1500, suggested_price_cents=None, review_state=None,
                describe_status="done", tags="[]", ai_fields_json=None, duration_seconds=None)
    base.update(kw)
    return NS(**base)


def ready(it, **kw):
    kw.setdefault("price_cents", item_price_cents(it))
    kw.setdefault("currency", "USD")
    return item_readiness(it, account_id=A, **kw)


def test_described_priced_item_is_ready_and_states_only_known_facts():
    r = ready(item())
    assert r.ready and r.facts["classification"] == "prerecorded" and r.facts["currency"] == "USD"
    assert {"material_exclusions", "preview_policy", "custom_or_live_availability"} <= set(r.unknown)
    assert "duration_seconds" not in r.facts


def test_bare_kind_word_is_not_a_description():
    for text in ("photo", "video", "audio", "", None, "photo video", "pic"):
        assert not meaningful_description(text)
    r = ready(item(description=None, search_text="photo"))
    assert not r.ready and "no_meaningful_description" in r.missing and r.facts == {}


def test_undescribed_with_failed_describe_is_flagged():
    r = ready(item(description=None, search_text="video", kind="video", describe_status="failed"))
    assert not r.ready and "description_unavailable" in r.missing


def test_wrong_or_unknown_version_removed_other_account_not_ready():
    assert "version_changed" in ready(item(), expected_version="old").missing
    assert "version_unknown" in ready(item(updated_at_of=None)).missing
    from datetime import datetime
    assert "removed" in ready(item(removed_at=datetime(2026, 1, 1))).missing
    assert "not_account_owned" in item_readiness(item(account_id="other"), account_id=A, price_cents=1, currency="USD").missing


def test_creator_boundary_excludes_item():
    r = ready(item(description="woman wearing a latex outfit posing"), limits="latex; feet")
    assert not r.ready and "creator_boundary" in r.missing


def test_missing_price_or_currency_not_ready():
    assert "price_missing" in ready(item(default_price_cents=None)).missing
    assert "currency_missing" in ready(item(), currency=None).missing
    assert "currency_missing" in ready(item(), currency="US").missing
    assert ready(item(default_price_cents=None), price_source="pack_ladder").ready


def test_owned_offered_and_rejected_excluded():
    assert "already_owned" in ready(item(), bought={11}).missing
    assert "already_offered" in ready(item(), offered={11}).missing
    assert "operator_rejected" in ready(item(review_state="rejected")).missing


def test_video_duration_unknown_is_not_advertised():
    r = ready(item(kind="video", description="Short clip of her dancing in a kitchen"))
    assert r.ready and "duration" in r.unknown and "duration_seconds" not in r.facts
    assert ready(item(kind="video", duration_seconds=62, description="Short clip of her dancing")).facts["duration_seconds"] == 62


def test_ready_ids_filters_the_pool():
    items = [item(media_id=1), item(media_id=2, description=None, search_text="photo")]
    assert ready_ids(items, account_id=A) == {1}

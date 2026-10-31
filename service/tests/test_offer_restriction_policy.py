"""Different-item eligibility is native-OnlyFans scoped; generic caps stay intact."""
from automations.sell_lane import (
    R_CAPS,
    R_MAX_OPEN,
    R_SPEND_CAP_7D,
    SellVerdict,
    money_brake,
)


def test_native_different_items_ignore_open_and_daily_business_caps():
    cfg = {"fastt_offer_fan_caps_enabled": False,
           "max_open_offers": 2, "max_offers_per_fan_per_day": 2}
    assert money_brake(cfg, paid7=0, open_count=20, unlocked=False, caps_ok=False) == SellVerdict(True, "ok")


def test_generic_path_keeps_open_and_daily_caps():
    cfg = {"max_open_offers": 2, "max_offers_per_fan_per_day": 2}
    assert money_brake(cfg, 0, 2, False, True) == SellVerdict(False, R_MAX_OPEN)
    assert money_brake(cfg, 0, 0, False, False) == SellVerdict(False, R_CAPS)


def test_spend_ceiling_still_applies_to_native_different_items():
    cfg = {"fastt_offer_fan_caps_enabled": False,
           "spend_velocity_cap_7d_cents": 500}
    assert money_brake(cfg, 500, 20, False, False) == SellVerdict(False, R_SPEND_CAP_7D)

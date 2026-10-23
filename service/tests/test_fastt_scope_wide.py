"""Account-wide FASTT ownership (``<account>:*``) and the multi-item content plan. No DB, no network."""
import json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest


@pytest.fixture
def scope(monkeypatch):
    from onlyfans_runtime import fastt_manual as m
    def setv(v):
        monkeypatch.setenv(m.SCOPE_ENV, v)
        return m
    return setv


def test_scope_forms(scope):
    m = scope("of-176774528:*, 555:42 ,bad:x,:*,of-9:")
    assert m.fastt_owned("of-176774528", 1) and m.fastt_owned("176774528", 10**9)
    assert m.fastt_account_owned("of-176774528") and not m.fastt_account_owned("555")
    assert m.fastt_owned("555", 42) and not m.fastt_owned("555", 43)     # per-fan form unchanged
    assert not m.fastt_owned("of-1", 1) and not m.fastt_owned("of-9", 1)
    assert not scope("").fastt_owned("of-176774528", 1)


def _plan(tmp_path, items, fan="*", **extra):
    from native_pilot.content import ContentPlan
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"account_id": "of-7", "fan_id": fan, "items": items, **extra}))
    return ContentPlan(p, "of-7", 99)


def _it(cid, media, price=500, previews=()):
    return {"catalog_id": cid, "kind": "video", "price_cents": price,
            "media": [{"id": i, "type": "video"} for i in media], "preview_ids": list(previews)}


def test_multi_item_plan(tmp_path):
    from native_pilot.ledger import Refused
    plan = _plan(tmp_path, [_it(1, [11, 12], 500, [11]), _it(2, [21], 1500)], price_range_cents=[300, 2000])
    assert plan.catalog_ids == {1, 2}
    a = plan.terms({"media_files": [11, 12], "price": 5, "locked_text": False, "previews": [11]})
    b = plan.terms({"media_files": [21], "price": 15.0, "previews": []})
    assert (a["catalog_id"], b["catalog_id"]) == (1, 2)
    for bad in ({"media_files": [11, 12], "price": 15, "previews": [11]},     # price of another item
                {"media_files": [21], "price": 5, "previews": []},
                {"media_files": [11], "price": 5, "previews": [11]},          # partial set
                {"media_files": [11, 12], "price": 5, "previews": []},        # wrong previews
                {"media_files": [31], "price": 5, "previews": []}):           # unlisted item
        with pytest.raises(Refused):
            plan.terms(bad)
    for items, extra in (([_it(1, [11]), _it(1, [12])], {}),                  # dup id
                         ([_it(1, [11]), _it(2, [11])], {}),                  # dup media set
                         ([_it(1, [11], 250)], {}), ([_it(1, [11], 600)], {}),   # default range is exactly 300
                         ([_it(1, [11], 2500)], {"price_range_cents": [300, 2000]}),
                         ([_it(1, [11], 500)], {"price_range_cents": [100, 2000]}),
                         ([_it(1, [11], 500)], {"price_range_cents": [300, 10001]})):
        with pytest.raises(Refused):
            _plan(tmp_path, items, **extra)


def test_legacy_single_item_and_fan_scope(tmp_path):
    from native_pilot.ledger import Refused
    plan = _plan(tmp_path, [_it(1, [11], 300)], fan=99)
    assert plan.item["catalog_id"] == 1 and plan.paid is plan.item
    with pytest.raises(Refused):
        _plan(tmp_path, [_it(1, [11], 300)], fan=100)


def test_check_local_accepts_any_listed_open_offer(tmp_path):
    import sqlite3
    from native_pilot import runner
    from native_pilot.ledger import Refused
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.executescript("""
      CREATE TABLE accounts(id); CREATE TABLE automation_rules(account_id,is_enabled);
      CREATE TABLE scheduled_jobs(account_id,status,kind); CREATE TABLE account_ai_config(account_id,persona);
      CREATE TABLE content_offers(account_id,fan_id,status,mode,item_id);
      CREATE TABLE pending_offer(account_id,fan_id);
      CREATE TABLE twin_bridge_send_attempts(account_id,fan_id,state);
      CREATE TABLE twin_bridge_turns(account_id,state);
      INSERT INTO accounts VALUES('a');
      INSERT INTO content_offers VALUES('a',5,'open','ppv',2);""")
    con.commit(); con.close()
    for approved, ok in ((None, False), (1, False), (2, True), ({1, 2}, True), ([3], False)):
        if ok:
            runner.check_local(str(db), "a", 5, approved)
        else:
            with pytest.raises(Refused):
                runner.check_local(str(db), "a", 5, approved)


def test_check_catalog_multi(tmp_path):
    import sqlite3
    from native_pilot.ledger import Refused
    plan = _plan(tmp_path, [_it(1, [11, 12], 500, [11]), _it(2, [21], 1500)], price_range_cents=[300, 2000])
    con = sqlite3.connect(":memory:"); con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE catalog_items(id,account_id,kind,media_ids,preview_media_ids,price_cents,is_free_teaser,enabled,pricing_mode)")
    rows = [(1, "of-7", "video", "[11, 12]", "[11]", 500, 0, 1, "fixed"), (2, "of-7", "video", "[21]", "[]", 1500, 0, 1, "fixed")]
    con.executemany("INSERT INTO catalog_items VALUES(?,?,?,?,?,?,?,?,?)", rows)
    plan.check_catalog(con, "of-7", require_enabled=True)
    con.execute("INSERT INTO catalog_items VALUES(3,'of-7','video','[31]','[]',500,0,1,'fixed')")   # unlisted + enabled
    with pytest.raises(Refused):
        plan.check_catalog(con, "of-7")
    con.execute("UPDATE catalog_items SET enabled=0 WHERE id=3"); plan.check_catalog(con, "of-7")
    con.execute("UPDATE catalog_items SET price_cents=600 WHERE id=2")
    with pytest.raises(Refused):
        plan.check_catalog(con, "of-7")

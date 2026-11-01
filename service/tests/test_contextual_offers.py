"""Synthetic decisions, real durable guards and migration. No provider sockets."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from alembic.migration import MigrationContext
from alembic.operations import Operations

from native_pilot.contextual import (
    ContextualAuthority,
    ignored_contextual,
    purchase_fresh,
    purchase_freshness_detail,
    refuse_repeat_media,
)
from native_pilot.ledger import Refused
from onlyfans_runtime.contextual_mode import KEY, mode
from onlyfans_runtime.configuration import validate_patch, InvalidConfiguration, SALES_FLAGS
from onlyfans_runtime.ledger import _coverage_receipt
from twin_bridge import store

ACCOUNT, FAN = "of-900001", 900002
NOW = datetime(2026, 10, 30, 4, 0)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "context.db"
    with sqlite3.connect(path) as c:
        c.executescript("""
        CREATE TABLE accounts(id TEXT PRIMARY KEY);
        CREATE TABLE account_ai_config(account_id TEXT,ai_chatter_config_json TEXT);
        CREATE TABLE of_runtime(account_id TEXT,binding_id TEXT,configuration_revision INTEGER,enabled BOOLEAN);
        CREATE TABLE of_provider_connection(account_id TEXT,binding_id TEXT,revision INTEGER,owner_id INTEGER,status TEXT,expires_at TEXT,last_error TEXT);
        CREATE TABLE of_ledger_scan(account_id TEXT,binding_id TEXT,connection_revision INTEGER,owner_id INTEGER,status TEXT,last_error TEXT,last_complete_at TEXT,last_full_at TEXT,window_end TEXT,coverage_json TEXT);
        CREATE TABLE content_offers(account_id TEXT,fan_id INTEGER,offer_message_id INTEGER,status TEXT,resolved_by TEXT);
        CREATE TABLE messages(account_id TEXT,fan_id INTEGER,message_id INTEGER,is_paid BOOLEAN);
        CREATE TABLE vault_sends(account_id TEXT,fan_id INTEGER,media_id INTEGER,message_id INTEGER,price_cents INTEGER,was_purchased BOOLEAN,sent_at TEXT);
        CREATE TABLE vault_items(account_id TEXT,media_id INTEGER,kind TEXT,removed_at TEXT,content_hash TEXT,updated_at_of TEXT,description TEXT,video_description TEXT,ai_fields_json TEXT,operator_overrides_json TEXT,locked_fields_json TEXT);
        """)
        settings = {k:True for k in SALES_FLAGS}
        settings.update({KEY:"live", "fastt_post_buy_upsell":True, "max_offers_per_fan_per_day":7})
        c.execute("INSERT INTO accounts VALUES(?)",(ACCOUNT,))
        c.execute("INSERT INTO account_ai_config VALUES(?,?)",(ACCOUNT,json.dumps(settings)))
        c.execute("INSERT INTO of_runtime VALUES(?,?,?,?)",(ACCOUNT,"binding",5,True))
        c.execute("INSERT INTO of_provider_connection VALUES(?,?,?,?,?,?,?)",(ACCOUNT,"binding",2,900001,"verified",str(NOW+timedelta(hours=1)),None))
        scan = SimpleNamespace(account_id=ACCOUNT,binding_id="binding",connection_revision=2,owner_id=900001,
                               last_complete_at=NOW-timedelta(seconds=30),last_full_at=None)
        c.execute("INSERT INTO of_ledger_scan VALUES(?,?,?,?,?,?,?,?,?,?)",(ACCOUNT,"binding",2,900001,"idle",None,
                  str(scan.last_complete_at),None,str(scan.last_complete_at),_coverage_receipt(scan)))
        for mid in (101,102,103):
            c.execute("INSERT INTO vault_items VALUES(?,?,?,?,?,?,?,?,?,?,?)",(ACCOUNT,mid,"photo",None,"hash",None,"gym photo",None,None,None,None))
    eng=create_engine("sqlite:///"+str(path))
    from db.onlyfans_contextual_models import OfContextualOffer
    with eng.begin() as c:
        OfContextualOffer.__table__.create(c)
    eng.dispose()
    return path


def authority(db, mid=10):
    return ContextualAuthority(db,ACCOUNT,FAN,mid)


def delivery(media=(101,102), price=500, account=ACCOUNT):
    return SimpleNamespace(plan=SimpleNamespace(account_id=account,fan_id=FAN,media=list(media),attached=list(media),previews=[],price_cents=price))


def terms(media=(101,102), price=500):
    return {"media_ids":list(media),"preview_ids":[],"price_cents":price}


@pytest.fixture(autouse=True)
def clock():
    with patch("native_pilot.runner.now",return_value=NOW):
        yield


@pytest.mark.parametrize("value",[None,True,False,0,[],{},"on","preview","LIVE"])
def test_invalid_writes_are_rejected_and_corrupt_saved_values_are_off(value,caplog):
    with pytest.raises(InvalidConfiguration):validate_patch({"fasttContextualSalesMode":value})
    assert mode({KEY:value}) == "off"
    assert "invalid_contextual_sales_mode" in caplog.text


def test_missing_and_valid_modes():
    assert mode({})=="off"
    for value in ("off","shadow","live"):
        assert mode({KEY:value})==value
        assert validate_patch({"fasttContextualSalesMode":value})=={"fasttContextualSalesMode":value}


@pytest.mark.parametrize("change",[
    "UPDATE of_runtime SET enabled=0", "UPDATE of_provider_connection SET status='paused'",
    "UPDATE of_provider_connection SET binding_id='different'", "UPDATE of_provider_connection SET revision=3",
    "UPDATE of_provider_connection SET expires_at='2025-01-01'", "UPDATE of_ledger_scan SET last_complete_at=NULL",
    "UPDATE of_ledger_scan SET status='held'",
    "UPDATE of_ledger_scan SET coverage_json='{}'", "UPDATE of_ledger_scan SET window_end='2026-10-29'",
    "UPDATE of_ledger_scan SET window_end='2026-10-30 05:00'",
])
def test_ineligible_or_stale_purchase_state_refuses_only_contextual(db,change):
    a=authority(db)
    assert a.check()["mode"]=="live"
    with sqlite3.connect(db) as c:c.execute(change)
    with pytest.raises(Refused):a.check()


def test_existing_sales_permission_still_required_and_hold_checked(db):
    a=authority(db)
    a.hold_check=lambda:"human_takeover"
    with pytest.raises(Refused,match="contextual-fan-ineligible"):a.check()
    a.hold_check=None
    with sqlite3.connect(db) as c:
        c.execute("UPDATE account_ai_config SET ai_chatter_config_json=?",(json.dumps({KEY:"live"}),))
    with pytest.raises(Refused,match="contextual-sales-disabled"):a.check()


def test_recent_read_deferral_does_not_invalidate_fresh_coverage(db):
    a=authority(db)
    assert a.check()["mode"]=="live"
    with sqlite3.connect(db) as c:
        c.execute("UPDATE of_ledger_scan SET last_error='provider_read_deferred:provider-backoff'")
    assert a.check()["mode"]=="live"
    scan={"binding_id":"binding","connection_revision":2,"owner_id":900001,
          "last_error":"provider_read_deferred:provider-backoff",
          "next_poll_at":NOW+timedelta(seconds=2),"window_end":NOW-timedelta(seconds=30),
          "last_complete_at":NOW-timedelta(seconds=30)}
    connection={"binding_id":"binding","connection_revision":2,"owner_id":900001}
    detail=purchase_freshness_detail(scan,connection,NOW)
    assert detail["cause"]=="provider-deferred"
    assert detail["defer_reason"]=="provider-backoff"


def test_reservation_survives_caption_price_change_replay_and_unknown(db):
    a=authority(db);permission=a.check()
    with a.arm(delivery(),permission,"gym conversation",(10,)):
        metadata=a.reserve(terms(),1)
        a.before_post(metadata)
    a.outcome("unknown")
    for mid in (10,11):
        b=authority(db,mid)
        with pytest.raises(Refused,match="contextual-offer-unresolved"):b.check()
        with b.arm(delivery(price=600),permission,"changed caption",(mid,)):
            with pytest.raises(Refused):b.reserve(terms(price=600),2)
    with store.readonly(db) as c:
        assert c.execute("SELECT count(*) FROM of_contextual_offer").fetchone()[0]==1


def test_ignored_card_not_cleared_by_expiry_chatter_or_offer_send(db):
    a=authority(db);p=a.check()
    with a.arm(delivery(),p,"gym",(10,)):a.reserve(terms(),1)
    a.outcome("sent",500)
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO content_offers VALUES(?,?,?,?,?)",(ACCOUNT,FAN,500,"expired",None))
        c.execute("INSERT INTO messages VALUES(?,?,?,?)",(ACCOUNT,FAN,500,False))
    with store.readonly(db) as c:assert ignored_contextual(c,ACCOUNT,FAN)
    assert authority(db,11).check()["mode"] == "live"
    with sqlite3.connect(db) as c:
        c.execute("UPDATE content_offers SET status='delivered',resolved_by='ppv_ledger'")
    with store.readonly(db) as c:assert ignored_contextual(c,ACCOUNT,FAN) # still no confirmed payment flag
    with sqlite3.connect(db) as c:c.execute("UPDATE messages SET is_paid=1")
    b=authority(db,11);p=b.check()
    with b.arm(delivery((103,)),p,"new gym",(11,)):b.reserve(terms((103,)),2)
    with store.readonly(db) as c:
        assert c.execute("SELECT state FROM of_contextual_offer WHERE source_message_id=10").fetchone()[0]=="purchased"


@pytest.mark.parametrize("change,reason",[
    ("UPDATE account_ai_config SET ai_chatter_config_json='{}'","contextual-mode-changed"),
    ("UPDATE of_runtime SET configuration_revision=6","contextual-revision-changed"),
    ("UPDATE vault_items SET removed_at='2026-10-30' WHERE media_id=101","contextual-content-unavailable"),
    ("UPDATE vault_items SET description='different claim' WHERE media_id=101","contextual-content-changed"),
])
def test_changes_between_inference_and_transport_cancel(db,change,reason):
    a=authority(db);p=a.check()
    with a.arm(delivery(),p,"gym",(10,)):
        metadata=a.reserve(terms(),1)
        with sqlite3.connect(db) as c:c.execute(change)
        with pytest.raises(Refused,match=reason):a.before_post(metadata)


def test_parallel_workers_reserve_at_most_one_card(db):
    p=authority(db).check()
    def attempt(mid):
        a=authority(db,mid)
        with a.arm(delivery(),p,"gym",(mid,)):
            try:a.reserve(terms(),mid);return True
            except Refused:return False
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(attempt,range(10,14)))
    assert sum(results)==1


def test_different_contextual_card_allowed_after_confirmed_unpaid_card(db):
    a=authority(db);permission=a.check()
    with a.arm(delivery(),permission,"gym",(10,)):a.reserve(terms(),1)
    a.outcome("sent",500)
    b=authority(db,11);permission=b.check()
    with b.arm(delivery((103,)),permission,"different relevant content",(11,)):
        b.reserve(terms((103,)),2)
    b.outcome("sent",501)
    with store.readonly(db) as c:
        assert c.execute("SELECT count(*) FROM of_contextual_offer WHERE state='sent'").fetchone()[0] == 2
        assert ignored_contextual(c,ACCOUNT,FAN) # no fake purchase or cancellation
        with pytest.raises(Refused,match="item-already-pending"):
            refuse_repeat_media(c,ACCOUNT,FAN,terms((101,),800))
    with pytest.raises(Refused,match="contextual-turn-already-decided"):
        authority(db,11).check()


def test_unknown_delivery_still_blocks_even_different_media(db):
    a=authority(db);permission=a.check()
    with a.arm(delivery(),permission,"gym",(10,)):a.reserve(terms(),1)
    a.outcome("unknown")
    b=authority(db,11)
    with pytest.raises(Refused,match="contextual-offer-unresolved"):b.check()
    with b.arm(delivery((103,)),permission,"different",(11,)):
        with pytest.raises(Refused,match="contextual-offer-unresolved"):
            b.reserve(terms((103,)),2)


def test_same_or_owned_media_blocked_different_explicit_item_allowed(db):
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO vault_sends VALUES(?,?,?,?,?,?,?)",(ACCOUNT,FAN,101,500,500,False,str(NOW)))
    with store.readonly(db) as c:
        with pytest.raises(Refused,match="item-already-pending"):refuse_repeat_media(c,ACCOUNT,FAN,terms((101,),800))
        refuse_repeat_media(c,ACCOUNT,FAN,terms((103,)))
        refuse_repeat_media(c,"of-other",FAN,terms((101,)))
    with sqlite3.connect(db) as c:c.execute("UPDATE vault_sends SET was_purchased=1")
    with store.readonly(db) as c:
        with pytest.raises(Refused,match="item-already-purchased"):refuse_repeat_media(c,ACCOUNT,FAN,terms((101,)))


def test_wrong_creator_or_snapshot_price_refused(db):
    a=authority(db);p=a.check()
    with pytest.raises(Refused,match="contextual-decision-scope"):
        with a.arm(delivery(account="of-other"),p,"gym",(10,)):pass
    with a.arm(delivery(),p,"gym",(10,)):
        with pytest.raises(Refused,match="contextual-snapshot-changed"):a.reserve(terms(price=800),1)


def test_contextual_trigger_is_separate_from_ask():
    from automations.sell_lane import SellLane, Turn, SellVerdict
    lane=SellLane(ACCOUNT,"ai_chatter",{"pack_send_enabled":True,"pack_on_ask_enabled":True,KEY:"live"})
    turn=Turn(text="I am at the gym",trigger="contextual_relevance",supporting_message_ids=(10,))
    assert not lane.is_ask(turn) and not turn.model_says_ask and turn.wide_ask is None
    lane.may_sell=AsyncMock(return_value=SellVerdict(True,"ok"))
    lane.qualify=AsyncMock(return_value=(True,"ok"))
    authority=SimpleNamespace(source_message_id=10,check=lambda _:None)
    cap=SimpleNamespace(account_id=ACCOUNT,fan_id=FAN,contextual=authority)
    with patch("telegram_runtime.capabilities.active_telegram_capabilities",return_value=cap):
        assert asyncio.run(lane._gate(FAN,turn,fan=None,blocked=False,counters=None,human_ask_at=None)) is None
    # A caller cannot smuggle a made-up ask through the new trigger without the native authority.
    assert asyncio.run(lane._gate(FAN,turn,fan=None,blocked=False,counters=None,human_ask_at=None)).reason=="not_armed"
    lane.cfg[KEY]="shadow"
    assert asyncio.run(lane._gate(FAN,turn,fan=None,blocked=False,counters=None,human_ask_at=None)).reason=="not_armed"


@pytest.mark.parametrize("current_mode",["off","shadow","live"])
def test_off_shadow_and_live_action_boundaries(current_mode):
    from automations.contextual_offers import plan_contextual
    from automations.content_contract import Contract, Resolution
    from automations import pack_sender, content_prompts, content_resolver
    from automations._sell_signal import ContentDecision
    contract=Contract(subject="gym",strict=True,terms=["gym"])
    hook=SimpleNamespace(contract=contract,source="situation",what="fan is at gym",
                         action="send_ppv",reason="active gym interest",supporting_message_ids=(10,))
    a=SimpleNamespace(source_message_id=10,check=lambda _: {"mode":current_mode},emit=lambda e:events.append(e))
    lane=SimpleNamespace(cfg={KEY:current_mode},on=True,account_id=ACCOUNT,
                         may_sell=AsyncMock(return_value=True),plan=AsyncMock(return_value="plan"))
    c=SimpleNamespace(last_in_text="I just got back from the gym",last_in_mid=10,last_in_at=NOW,
                      messages=[("in","I just got back from the gym")],msg_ids=[10])
    events=[]
    @asynccontextmanager
    async def session():
        yield SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(first=lambda:(101,))))
    with patch("automations.contextual_offers.get_session",session), \
         patch.object(content_prompts,"read_hook",AsyncMock(return_value=hook)) as read, \
         patch.object(content_resolver,"resolve",AsyncMock(return_value=Resolution([101,102],contract))) as resolve, \
         patch.object(pack_sender,"_bought_media",AsyncMock(return_value=set())), \
         patch.object(pack_sender,"offered_media",AsyncMock(return_value=set())) as offered:
        result=asyncio.run(plan_contextual(lane,a,c,SimpleNamespace(fan_id=FAN)))
    if current_mode=="off":
        assert result is None and not read.called and not resolve.called
        offered.assert_not_awaited()
        assert [e["stage"] for e in events]==["skipped"]
    elif current_mode=="shadow":
        assert result is None and read.called and resolve.called and not lane.plan.called
        offered.assert_awaited_once_with(ACCOUNT,FAN)
        assert next(e for e in events if "matched_media_ids" in e)["matched_media_ids"]==[101,102]
    else:
        assert result[0]=="plan" and result[2].action=="send_ppv" and lane.plan.called
        offered.assert_awaited_once_with(ACCOUNT,FAN)


def test_unrelated_chat_does_no_inference():
    from automations.contextual_offers import plan_contextual
    lane=SimpleNamespace(cfg={KEY:"live"})
    c=SimpleNamespace(last_in_text="You are so nice")
    assert asyncio.run(plan_contextual(lane,None,c,None)) is None


def test_positive_recovery_links_origin_without_resending(db):
    from native_pilot.contextual import reconcile_origin
    a=authority(db);p=a.check()
    with a.arm(delivery(),p,"gym",(10,)):metadata=a.reserve(terms(),42)
    a.outcome("unknown")
    intent={"account_id":ACCOUNT,"content":{"contextual":metadata}}
    with store.transaction(db) as con:reconcile_origin(con,intent,42,500)
    with store.readonly(db) as con:
        row=con.execute("SELECT state,message_id FROM of_contextual_offer").fetchone()
        assert (row[0],row[1])==("sent",500)
        assert ignored_contextual(con,ACCOUNT,FAN) # receipt is not a purchase
    with store.transaction(db) as con:
        with pytest.raises(Refused,match="contextual-recovery-conflict"):reconcile_origin(con,intent,43,501)


@pytest.mark.parametrize("overrides",[
    {"source":"more"}, {"supporting_message_ids":[999]}, {"evidence_quote":"invented activity"},
    {"action":"suggest_content"}, {"hook":False},
])
def test_unsupported_model_relevance_cannot_become_an_offer(overrides):
    from automations import content_prompts as prompts
    base={"hook":True,"source":"situation","action":"send_ppv","supporting_message_ids":[10],
          "evidence_quote":"gym","bridge":"love a good workout","subject":"me at gym","terms":["gym"]}
    with patch.object(prompts,"_thread_lines",AsyncMock(return_value=["FAN: I am at the gym"])), \
         patch.object(prompts,"_sold_lines",AsyncMock(return_value=[])), \
         patch.object(prompts,"vault_lexicon",AsyncMock(return_value=["gym"])), \
         patch.object(prompts,"resolve_model",AsyncMock(return_value="fixture")), \
         patch.object(prompts.llm_client,"effort_options",return_value=[]), \
         patch.object(prompts,"runtime_chat",AsyncMock(return_value=SimpleNamespace(parsed={**base,**overrides}))):
        assert asyncio.run(prompts.read_hook(ACCOUNT,FAN,contextual_message={"id":10,"text":"I am at the gym"})) is None


def test_mode_save_uses_existing_revision_and_audit_without_changing_scope_or_budget():
    from sqlalchemy.orm import Session
    from db.models import Account,AccountAiConfig
    from db.onlyfans_models import OfConfigurationChange
    from onlyfans_runtime.configuration import update_configuration,fingerprint
    eng=create_engine("sqlite:///:memory:")
    for table in (Account.__table__,AccountAiConfig.__table__,OfConfigurationChange.__table__):table.create(eng)
    original={**{k:True for k in SALES_FLAGS},"fastt_scope":{"mode":"selected","fans":[FAN]},
              "max_offers_per_fan_per_day":7,"fastt_post_buy_upsell":True,"fastt_pack_price_cents":1234}
    with Session(eng) as s,s.begin():
        s.add(Account(id=ACCOUNT,nickname="Synthetic"))
        row=AccountAiConfig(account_id=ACCOUNT,ai_chatter_config_json=json.dumps(original),daily_cost_cap_cents=500)
        s.add(row);s.flush();before=fingerprint(row)
    runtime=SimpleNamespace(configuration_revision=5,binding_id="binding")
    target=SimpleNamespace(engine=eng,_runtime=lambda *_:runtime)
    with patch("onlyfans_runtime.configuration.runtime_model_options",return_value=None):
        revision,_=update_configuration(target,"tenant",ACCOUNT,patch={"fasttContextualSalesMode":"shadow"},
            expected_revision=5,expected_fingerprint=before,command_key="b"*64,actor="op-session:"+"a"*64)
    assert revision==6
    with Session(eng) as s:
        row=s.get(AccountAiConfig,ACCOUNT)
        assert json.loads(row.ai_chatter_config_json)=={**original,KEY:"shadow"}
        assert row.daily_cost_cap_cents==500
        event=s.get(OfConfigurationChange,(ACCOUNT,"b"*64))
        assert json.loads(event.after_json)=={"fasttContextualSalesMode":"shadow"}
    eng.dispose()


def test_migration_rollback_retains_delivery_evidence(tmp_path):
    import importlib.util
    from pathlib import Path
    from sqlalchemy import text,inspect
    source=Path(__file__).parents[1]/"db/migrations/versions/20261030_0100_contextual_offers.py"
    spec=importlib.util.spec_from_file_location("contextual_migration",source)
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    eng=create_engine("sqlite:///"+str(tmp_path/"migration.db"))
    with eng.begin() as c:
        c.execute(text("CREATE TABLE accounts(id TEXT PRIMARY KEY)"))
        context=MigrationContext.configure(c)
        with Operations.context(context):
            migration.upgrade()
            c.execute(text("INSERT INTO of_contextual_offer(account_id,decision_key,fan_id,binding_id,source_message_id,content_key,snapshot_json,state,created_at) VALUES('of-test','decision',1,'binding',2,'content','{}','unknown','2026-10-30')"))
            migration.downgrade()
            migration.upgrade()
        assert c.scalar(text("SELECT count(*) FROM of_contextual_offer WHERE state='unknown'"))==1
        indexes=inspect(c).get_indexes("of_contextual_offer")
        assert any(i["name"]=="uq_contextual_unresolved" and i["unique"] for i in indexes)
    eng.dispose()


def test_provider_lease_contention_is_not_token_exhaustion_and_never_overlaps(monkeypatch):
    from onlyfans_runtime import provider_read_budget as budget
    row=SimpleNamespace(account_id=ACCOUNT,binding_id="binding",connection_revision=2,
        tokens=10,refilled_at=NOW,lease_token=None,lease_until=None,backoff_until=None,last_kind=None)
    runtime=SimpleNamespace(binding_id="binding")
    class Session:
        def __init__(self,*args,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def begin(self):return self
        def get(self,model,*args,**kwargs):
            return row if getattr(model,"__name__","")=="OfProviderReadBudget" else None
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker")
    monkeypatch.setattr(budget,"Session",Session)
    monkeypatch.setattr(budget,"_valid",lambda *args,**kwargs:runtime)
    target=SimpleNamespace(engine=None,_runtime=lambda *args:runtime)
    recent=budget.acquire(target,"tenant",ACCOUNT,connection_revision=2,kind="recent",now=NOW)
    assert row.tokens==9
    with pytest.raises(budget.ReadDeferred) as deferred:
        budget.acquire(target,"tenant",ACCOUNT,connection_revision=2,kind="ledger",now=NOW+timedelta(seconds=1))
    assert "lease occupied" in str(deferred.value) and row.tokens==9
    assert not budget.release(target,"tenant",SimpleNamespace(**{**recent.__dict__,"token":"foreign"}),now=NOW+timedelta(seconds=2))
    assert budget.release(target,"tenant",recent,now=NOW+timedelta(seconds=2))
    ledger=budget.acquire(target,"tenant",ACCOUNT,connection_revision=2,kind="ledger",now=NOW+timedelta(seconds=2))
    assert ledger.token!=recent.token
    with pytest.raises(budget.Refused):budget.current(target,"tenant",recent,now=NOW+timedelta(seconds=3))
    assert budget.current(target,"tenant",ledger,now=NOW+timedelta(seconds=3))


def test_crash_after_receipt_retention_keeps_hold_until_matching_purchase(db):
    a=authority(db);p=a.check()
    with a.arm(delivery(),p,"gym",(10,)):metadata=a.reserve(terms(),42)
    # Simulate a crash after the existing sender retained its positive receipt,
    # before its contextual-origin projection completed.
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE native_pilots(pilot_id TEXT,account_id TEXT,fan_id INTEGER)")
        c.execute("CREATE TABLE native_pilot_receipts(attempt_id INTEGER,pilot_id TEXT,state TEXT,provider_message_id TEXT,evidence_json TEXT)")
        c.execute("INSERT INTO native_pilots VALUES('pilot',?,?)",(ACCOUNT,FAN))
        c.execute("INSERT INTO native_pilot_receipts VALUES(42,'pilot','sent','500',?)",
                  (json.dumps({"dispatch_intent":{"content":{"contextual":metadata}}}),))
    assert authority(db,11).check()["mode"] == "live" # positive receipt resolves delivery, not ownership
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO content_offers VALUES(?,?,?,?,?)",(ACCOUNT,FAN,500,"delivered","ppv_ledger"))
        c.execute("INSERT INTO messages VALUES(?,?,?,1)",(ACCOUNT,FAN,500))
    b=authority(db,11);p=b.check()
    with b.arm(delivery((103,)),p,"new gym",(11,)):b.reserve(terms((103,)),43)
    with store.readonly(db) as c:
        assert tuple(c.execute("SELECT state,message_id FROM of_contextual_offer WHERE source_message_id=10").fetchone())==("purchased",500)


def test_explicit_same_item_cannot_duplicate_a_card_after_projection_crash(db):
    a=authority(db);p=a.check()
    with a.arm(delivery(),p,"gym",(10,)):a.reserve(terms(),42)
    a.outcome("sent",500)
    # No vault_sends projection exists yet, but origin evidence already does.
    with store.readonly(db) as con:
        with pytest.raises(Refused,match="item-already-pending"):
            refuse_repeat_media(con,ACCOUNT,FAN,terms((101,),800))
        refuse_repeat_media(con,ACCOUNT,FAN,terms((103,),800))


@pytest.fixture
def multi_message_db(db):
    """Extend the durable-guard fixture with the canonical evidence columns."""
    with sqlite3.connect(db) as con:
        con.execute("ALTER TABLE messages ADD COLUMN direction TEXT")
        con.execute("ALTER TABLE messages ADD COLUMN created_at TEXT")
        con.execute("ALTER TABLE messages ADD COLUMN is_unsent BOOLEAN NOT NULL DEFAULT FALSE")
        con.executemany("INSERT INTO messages(account_id,fan_id,message_id,direction,created_at) "
                        "VALUES(?,?,?,?,?)", [
            (ACCOUNT,FAN,8,"out",str(NOW-timedelta(seconds=3))),
            (ACCOUNT,FAN,9,"in",str(NOW-timedelta(seconds=2))),
            (ACCOUNT,FAN,10,"in",str(NOW-timedelta(seconds=1))),
        ])
    return db


def test_multi_message_evidence_reserves_and_passes_before_post(multi_message_db):
    a=authority(multi_message_db);permission=a.check()
    with a.arm(delivery(),permission,"Current portrait interest",(9,10)):
        metadata=a.reserve(terms(),71)
        a.before_post(metadata)
    with store.readonly(multi_message_db) as con:
        row=con.execute("SELECT state,source_message_id,attempt_id,snapshot_json "
                        "FROM of_contextual_offer").fetchone()
        assert (row["state"],row["source_message_id"],row["attempt_id"]) == ("reserved",10,71)
        assert json.loads(row["snapshot_json"])["supporting_message_ids"] == [9,10]


def test_multi_message_evidence_keeps_purchase_freshness_gate(multi_message_db):
    a=authority(multi_message_db);permission=a.check()
    with a.arm(delivery(),permission,"Current portrait interest",(9,10)):
        metadata=a.reserve(terms(),72)
        with sqlite3.connect(multi_message_db) as con:
            con.execute("UPDATE of_ledger_scan SET window_end=?",(str(NOW-timedelta(days=1)),))
        with pytest.raises(Refused,match="contextual-purchase-state-stale"):
            a.before_post(metadata)
    with store.readonly(multi_message_db) as con:
        assert con.execute("SELECT state FROM of_contextual_offer").fetchone()[0] == "reserved"


def test_multi_message_evidence_cannot_bypass_unknown_delivery(multi_message_db):
    a=authority(multi_message_db);permission=a.check()
    with a.arm(delivery(),permission,"Current portrait interest",(9,10)):
        a.reserve(terms(),73)
    a.outcome("unknown")
    with sqlite3.connect(multi_message_db) as con:
        con.execute("INSERT INTO messages(account_id,fan_id,message_id,direction,created_at) VALUES(?,?,?,?,?)",
                    (ACCOUNT,FAN,11,"in",str(NOW)))
    b=authority(multi_message_db,11)
    with pytest.raises(Refused,match="contextual-offer-unresolved"):
        b.check()
    # Even a caller reusing earlier permission cannot reserve a second card.
    with b.arm(delivery((103,)),permission,"A different portrait",(10,11)):
        with pytest.raises(Refused,match="contextual-offer-unresolved"):
            b.reserve(terms((103,)),74)
    with store.readonly(multi_message_db) as con:
        assert con.execute("SELECT count(*) FROM of_contextual_offer").fetchone()[0] == 1
        assert con.execute("SELECT state FROM of_contextual_offer").fetchone()[0] == "unknown"



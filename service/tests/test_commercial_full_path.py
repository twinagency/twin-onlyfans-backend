"""Real NativeEngine -> SellLane -> ContextualAuthority -> Gateway -> receipt ledger.
Only the model and provider HTTP transports are synthetic. No authority is mocked.
"""
import asyncio
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
MEDIA = list(range(7101, 7108))

async def exercise(path):
    from sqlalchemy import select
    from db.schema import _metadata
    from db.engine import engine, get_session
    from db.models import Account, AccountAiConfig, Employee, Fan, Message, VaultItem, OfCreatorBinding, OfConversationState, ContentOffer
    from db.onlyfans_models import OfRuntime
    from db.onlyfans_connection_models import OfProviderConnection
    from db.onlyfans_ledger_models import OfLedgerScan
    from db.onlyfans_sales_models import OfSalesInvitation
    from onlyfans_runtime.ledger import _coverage_receipt
    from native_pilot import runner
    from native_pilot.ledger import Ledger
    from native_pilot.engine import NativeEngine
    from native_pilot.content import VaultPlan
    from native_pilot.contextual import ContextualAuthority
    from native_pilot.service import fan_hold_reason, fan_commercial_hold_reason
    from onlyfans_runtime.configuration import SALES_FLAGS
    from of_client import OFClient
    from twin_bridge import coordination
    import test_native_pilot_ledger as fixture
    from test_native_pilot_runner import FakeHTTP, FakeResponse

    metadata = _metadata()
    async with engine.begin() as c:
        # This is the retained SQLite NativeEngine integration, not the
        # PostgreSQL conversation engine. Its unused of_engine_* tables carry
        # PostgreSQL-only constraints and must not be constructed in SQLite.
        await c.run_sync(lambda connection: metadata.create_all(connection,
            tables=[table for table in metadata.sorted_tables if not table.name.startswith('of_engine_')]))
    with sqlite3.connect(path) as c:
        # The real legacy bridge tables are independent of the ORM foundation.
        for statement in fixture.SCHEMA.split(";"):
            if statement.strip() and "CREATE TABLE messages" not in statement:
                c.execute(statement)
    async with get_session() as s:
        s.add(Employee(id=1, display_name="Automation", is_active=True))
        await s.commit()
    results = []

    async def setup(n, mode="live", initiative="standard", text="I love your workout photos"):
        account, fan = str(900500+n), 910500+n
        now = datetime.utcnow()
        async with get_session() as s:
            s.add(Account(id=account, nickname="Synthetic creator")); await s.flush()
            s.add(Fan(account_id=account, fan_id=fan)); await s.flush()
            s.add(AccountAiConfig(account_id=account, ai_chatter_config_json=json.dumps({
                **{k: True for k in SALES_FLAGS}, "fastt_contextual_sales_mode": mode,
                "fastt_commercial_initiative": initiative, "language": "en"})))
            s.add(OfCreatorBinding(id="binding-"+account, tenant_id="synthetic", creator_id="creator-"+account, account_id=account))
            await s.flush()
            s.add(OfRuntime(account_id=account, binding_id="binding-"+account, enabled=True, activated_at=now, recovery_needed=False))
            s.add(OfConversationState(account_id=account, fan_id=fan, mode="automated"))
            s.add(OfProviderConnection(account_id=account, binding_id="binding-"+account, owner_id=int(account),
                revision=1, sealed_config="synthetic-unused", status="verified", expires_at=now+timedelta(hours=1), updated_at=now))
            scan = OfLedgerScan(account_id=account, binding_id="binding-"+account, connection_revision=1, owner_id=int(account),
                window_start=now-timedelta(days=1), window_end=now, page_offset=0, status="idle",
                next_poll_at=now+timedelta(seconds=30), last_complete_at=now, updated_at=now)
            scan.coverage_json = _coverage_receipt(scan)
            s.add(scan)
            s.add(Message(account_id=account, fan_id=fan, message_id=100, direction="in", body=text,
                created_at=now-timedelta(seconds=60), ingested_at=now-timedelta(seconds=59)))
            for mid in MEDIA:
                s.add(VaultItem(account_id=account, media_id=mid, kind="photo", content_hash="version-"+str(mid),
                    updated_at_of=now.isoformat(), description="Creator alone in a gym workout mirror portrait",
                    search_text="Creator alone in a gym workout mirror portrait", tags="[]", created_at=now-timedelta(days=1)))
            await s.commit()
        with sqlite3.connect(path) as c:
            c.execute("INSERT INTO twin_bridge_conversations VALUES(?,?,'active',NULL,0,NULL,NULL,1,0,0,?)",
                      (account, fan, now.isoformat()))
        ledger = Ledger(path)
        pilot = ledger.activate(pilot_id="synthetic-"+account, account_id=account, fan_id=fan,
            owner_token="synthetic-owner-"+account, cutoff=now-timedelta(minutes=2), deadline=now+timedelta(minutes=20),
            limit=20, now=now)
        return account, fan, ledger, pilot

    async def turn(case, source=100, accept=False, mutate=None, selection=None, hook_action="send_ppv", no_interest=False, chat_reply=None, before_wire=None, expected_stop=None, clarification_kind="product_question", mirror_native_pointer=False):
        account, fan, ledger, pilot = case
        current = runner.now()
        snap = ledger.snapshot(pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, now=current)
        assert snap is not None, (account, source, "source not admitted")
        key = ledger.claim_input(pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, snapshot=snap, now=current)
        sources = runner.read_sources(path, account, fan, snap.member_ids)
        history = [{"id": mid, "text": r["body"], "createdAt": r["created_at"].replace(" ", "T")+"Z",
                    "isFromMe": False, "fromUser": {"id": fan}, "withUser": {"id": fan}}
                   for mid, r in sorted(sources.items(), reverse=True)]
        class Http(FakeHTTP):
            def get(self, url, **kw):
                if "/vault/media/" in url:
                    return FakeResponse({"id": int(url.rsplit("/", 1)[-1]), "type": "photo", "canView": True})
                return super().get(url, **kw)
            def request(self, method, url, json=None, **kw):
                if mirror_native_pointer:
                    # Mirror managed PostgreSQL admission's existing pointer.
                    # The actual receipt and attempt owners still settle below.
                    with sqlite3.connect(path) as conn:
                        attempt_id = conn.execute("SELECT max(attempt_id) FROM native_pilot_receipts WHERE pilot_id=?", (pilot.pilot_id,)).fetchone()[0]
                        conn.execute("UPDATE of_conversation_state SET unresolved_attempt_id=? WHERE account_id=? AND fan_id=?",
                                     ("fastt-pilot-"+str(attempt_id), account, fan))
                response = super().request(method, url, json=json, **kw)
                response.data.update(price=json.get("price", 0), media=[{"id": m, "type": "photo"} for m in json.get("mediaFiles", [])],
                                     previews=json.get("previews", []), mediaCount=len(json.get("mediaFiles", [])))
                self.history[0] = dict(response.data)
                return response
        http = Http(history, account, fan, current.isoformat()+"Z")
        client = OFClient.__new__(OFClient)
        client.account_id, client.user_id, client.platform = account, account, "onlyfans"
        client.http, client.timeout_s, client.proxy_url, client.proxy_label = http, 1, None, None
        client._signed_headers = lambda url: {}
        content = VaultPlan(account, fan, str(path))
        gateway = runner.Gateway(ledger, pilot, client, asyncio.get_running_loop(), content)
        gateway.input_key, gateway.sources = key, sources
        gateway.dashboard_handover = True
        gateway.open_offer_blocks = False
        gateway.hold_check = lambda: fan_hold_reason(path, account, fan, True)
        gateway.commercial_hold_check = lambda: fan_commercial_hold_reason(path, account, fan, True)
        events = []
        authority = ContextualAuthority(path, account, fan, source, emit=events.append, hold_check=gateway.commercial_hold_check)
        gateway.contextual = authority
        coordination.register_owner(account, fan, pilot.owner_token, current+timedelta(minutes=20))
        calls = []
        async def model(**kw):
            purpose = kw["purpose"]; calls.append(purpose)
            if purpose == "ai_chatter":
                return SimpleNamespace(content=chat_reply or ("That is the set you picked. I like your choice." if accept else
                                       "That sounds like a good workout. I enjoyed hearing about it."), parsed=None)
            data = {
                "ai_chatter_post_purchase": {"verdict": "none"},
                "content_resolver_contract": {"is_ask": True, "same_item_requested": False, "subject": "workout",
                    "company": False, "quote": "show me", "terms": ["workout", "gym"]},
                "content_resolver_hook": {"hook": not no_interest, "source": "situation", "action": "chat" if no_interest else hook_action,
                    "clarification": clarification_kind, "missing": "Whether the fan wants the described photo set after the price explanation",
                    "what": "workout photos", "subject": "me at the gym", "media_kind": "photo",
                    "terms": ["gym", "workout", "mirror"], "bridge": ("do you mean photos or video?" if clarification_kind == "ambiguous_invitation" else "which workout photo format do you want?") if hook_action == "clarify" else "love a good workout",
                    "reason": "current workout-photo interest", "supporting_message_ids": [source],
                    "evidence": [{"id": source, "quote": sources[source]["body"]}]},
                "content_resolver_match": {"media_ids": selection or MEDIA},
                "content_resolver_verify": {"verdicts": {str(i): True for i in (selection or MEDIA)}},
                "content_resolver_action": {"phrase": "me at the gym"},
                "gather_close_answer": {"line": "love a good workout"},
            }.get(purpose)
            assert data is not None, purpose
            if mutate and purpose == "content_resolver_verify":
                mutate()
            return SimpleNamespace(content=json.dumps(data), parsed=data)
        if before_wire:
            original_hold = gateway.commercial_hold_check
            def commercial_hold():
                # Execute only after a verified invitation/offer was proposed.
                if getattr(content, "commercial_text", False) or authority.armed is not None:
                    before_wire()
                return original_hold()
            gateway.commercial_hold_check = commercial_hold
        with NativeEngine(account, fan, chat=model, send=gateway.send, content=content, contextual=authority,
                          reply_format=True, own_offers_only=True, open_offers_ok=True) as native:
            try:
                stats = await native.run(source)
            except Exception as exc:
                if expected_stop and gateway.stopped_reason == expected_stop:
                    stats = {"blocked": expected_stop}
                else:
                    raise AssertionError((str(exc), gateway.stopped_reason, events, http.posts, calls)) from exc
        ledger.finish_input(pilot_id=pilot.pilot_id, owner_token=pilot.owner_token, input_key=key,
                            now=runner.now(), outcome="complete")
        return http, calls, events, stats

    # Live progress traverses actual authority, reservation and physical dispatch.
    case = await setup(1)
    http, calls, events, stats = await turn(case)
    cards = [x for x in http.posts if x.get("price", 0) > 0]
    assert len(cards) == 1 and len(http.posts) >= 2, (http.posts, events, stats)
    assert "good workout" in http.posts[0]["text"]
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT state FROM of_contextual_offer WHERE account_id=?", (case[0],)).fetchone()[0] == "sent"
        assert c.execute("SELECT COUNT(*) FROM native_pilot_receipts WHERE pilot_id=? AND state='sent'", (case[3].pilot_id,)).fetchone()[0] >= 2
        records = [json.loads(r[0]) for r in c.execute("SELECT evidence_json FROM native_pilot_receipts WHERE pilot_id=? AND state='sent'", (case[3].pilot_id,))]
        priced = [r for r in records if r.get("canonical_quote")]
        assert len(priced) == 1 and priced[0]["quote_lifecycle"] == "provider_accepted"
        assert priced[0]["dispatch_intent"]["content"]["canonical_quote"] == priced[0]["canonical_quote"]
    results.append({"case": "proactive_with_complete_answer", "cards": len(cards), "real_authority": True})
    first_case = case

    # The same full path with off/shadow cannot issue a paid dispatch.
    for i, mode in enumerate(("off", "shadow"), 2):
        case = await setup(i, mode=mode)
        http, calls, events, stats = await turn(case)
        assert http.posts and not any(x.get("price", 0) for x in http.posts), (mode, http.posts, events)
        results.append({"case": mode, "cards": 0, "real_authority": True})

    # An item-specific invitation's short acceptance reaches the normal paid sender.
    case = await setup(4, initiative="lower")
    http, calls, events, stats = await turn(case)
    assert not any(x.get("price", 0) for x in http.posts)
    async with get_session() as s:
        inv = (await s.execute(select(OfSalesInvitation).where(OfSalesInvitation.account_id == case[0]))).scalar_one()
        assert inv.delivery_message_id and inv.offer_terms_json
        invited = json.loads(inv.media_ids_json)
        fanrow = await s.get(Fan, (case[0], case[1])); fanrow.automation_paused_until = None
        # This fixture advances its provider clock between complete fan turns.
        from sqlalchemy import update
        await s.execute(update(Message).where(Message.account_id == case[0], Message.direction == "out").values(
            created_at=datetime.utcnow()-timedelta(seconds=120), ingested_at=datetime.utcnow()-timedelta(seconds=119)))
        s.add(Message(account_id=case[0], fan_id=case[1], message_id=2000, direction="in", body="👍",
                      created_at=datetime.utcnow()-timedelta(seconds=30), ingested_at=datetime.utcnow()-timedelta(seconds=29)))
        await s.commit()
    # A new synthetic transport must allocate ids beyond the new fan input.
    original = FakeHTTP.request
    def ids_after_input(self, *a, **kw):
        result = original(self, *a, **kw)
        result.data["id"] += 2000
        self.history[0] = dict(result.data)
        return result
    with patch.object(FakeHTTP, "request", ids_after_input):
        http, calls, events, stats = await turn(case, source=2000, accept=True)
    cards = [x for x in http.posts if x.get("price", 0) > 0]
    assert len(cards) == 1 and set(cards[0]["mediaFiles"]) == set(invited), (http.posts, events, stats)
    assert "content_resolver_hook" not in calls
    results.append({"case": "short_acceptance", "cards": 1, "real_authority": True})

    case = await setup(5, text="show me your workout photos")
    http, calls, events, stats = await turn(case)
    assert len([x for x in http.posts if x.get("price", 0)]) == 1
    assert "content_resolver_hook" not in calls
    results.append({"case": "immediate_direct_request", "cards": 1, "real_authority": True})

    # These are real durable holds/inventory/freshness checks. Chat remains
    # permitted while the commercial action is refused by its own authority.
    from onlyfans_runtime.operator_cases import open_case
    for n, blocker in enumerate(("complaint", "stale_purchase_coverage", "missing_inventory"), 6):
        case = await setup(n)
        async with get_session() as s:
            if blocker == "complaint":
                await s.run_sync(lambda sync: open_case(sync, case[0], case[1], "complaint", reason="delivery_question",
                    evidence={"source_message_id": 100}, source_message_id=100, scope="commercial"))
            elif blocker == "stale_purchase_coverage":
                scan = await s.get(OfLedgerScan, case[0]); scan.window_end = datetime.utcnow()-timedelta(hours=1)
            else:
                from sqlalchemy import update
                await s.execute(update(VaultItem).where(VaultItem.account_id == case[0]).values(removed_at=datetime.utcnow()))
            await s.commit()
        http, calls, events, stats = await turn(case)
        assert http.posts and not any(x.get("price", 0) for x in http.posts), (blocker, http.posts, events)
        results.append({"case": blocker, "cards": 0, "real_authority": True})

    # Feed verified purchase facts into the existing ownership readers. A new
    # request uses remaining inventory, without waiting for the repeat window.
    from db.models import VaultSend
    other_media = list(range(7201, 7208))
    case = first_case
    async with get_session() as s:
        from sqlalchemy import update
        await s.execute(update(Message).where(Message.account_id == case[0], Message.price_cents > 0).values(
            is_paid=True, purchased_at=datetime.utcnow()))
        await s.execute(update(ContentOffer).where(ContentOffer.account_id == case[0]).values(
            status="delivered", resolved_by="ppv_ledger", lifecycle="purchased"))
        await s.execute(update(VaultSend).where(VaultSend.account_id == case[0]).values(was_purchased=True))
        await s.execute(update(Message).where(Message.account_id == case[0], Message.direction == "out").values(
            created_at=datetime.utcnow()-timedelta(seconds=120), ingested_at=datetime.utcnow()-timedelta(seconds=119)))
        fanrow = await s.get(Fan, (case[0], case[1])); fanrow.automation_paused_until = None
        for mid in other_media:
            s.add(VaultItem(account_id=case[0], media_id=mid, kind="photo", content_hash="new-version-"+str(mid),
                updated_at_of=datetime.utcnow().isoformat(), description="Creator alone in a gym workout mirror portrait",
                search_text="Creator alone in a gym workout mirror portrait", tags="[]", created_at=datetime.utcnow()-timedelta(days=1)))
        s.add(Message(account_id=case[0], fan_id=case[1], message_id=2000, direction="in", body="show me more workout photos",
            created_at=datetime.utcnow()-timedelta(seconds=30), ingested_at=datetime.utcnow()-timedelta(seconds=29)))
        await s.commit()
    with patch.object(FakeHTTP, "request", ids_after_input):
        http, calls, events, stats = await turn(case, source=2000, accept=True, selection=other_media)
    cards = [x for x in http.posts if x.get("price", 0)]
    assert len(cards) == 1 and set(cards[0]["mediaFiles"]) <= set(other_media)
    assert not set(cards[0]["mediaFiles"]) & set(MEDIA)
    results.append({"case": "purchase_then_different_requested_item", "cards": 1, "real_authority": True})
    # Seed confirmed historical output on an existing durable attempt. The
    # current source remains a fresh, real fan turn for the delivery authority.
    async def at_boundary(case, bubbles, *, upper=6, text="I love your workout photos", extension=False):
        from onlyfans_runtime.commercial_progress import start
        async with get_session() as session:
            state = await session.get(OfConversationState, (case[0], case[1]))
            a = start(100, "I love your workout photos")
            if extension:
                a.update(extension_used=True, extension_turn=100, status="clarification_pending", item_ids=MEDIA,
                         missing="Product question", clarification="product_question")
            state.commercial_state_json = json.dumps({"version": 1, "attempt": a, "completed": []})
            for n in range(bubbles):
                session.add(Message(account_id=case[0], fan_id=case[1], message_id=101+n, direction="out",
                    body="A confirmed prior reply.", automation_kind="ai_chatter",
                    created_at=datetime.utcnow()-timedelta(seconds=55-n), ingested_at=datetime.utcnow()-timedelta(seconds=54-n)))
            session.add(Message(account_id=case[0], fan_id=case[1], message_id=200, direction="in", body=text,
                created_at=datetime.utcnow()-timedelta(seconds=20), ingested_at=datetime.utcnow()-timedelta(seconds=19)))
            config = await session.get(AccountAiConfig, case[0]); cfg = json.loads(config.ai_chatter_config_json)
            cfg["fastt_first_offer_window"] = [min(2, upper), upper]
            config.ai_chatter_config_json = json.dumps(cfg)
            await session.commit()

    for n, preceding in enumerate((1, 7), 10):
        case = await setup(n)
        await at_boundary(case, preceding)
        http, calls, events, stats = await turn(case, source=200)
        assert len([x for x in http.posts if x.get("price", 0)]) == 1
        async with get_session() as session:
            state = await session.get(OfConversationState, (case[0], case[1]))
            a = json.loads(state.commercial_state_json)["attempt"]
            assert a["status"] == "offered" and a["bubbles"] >= preceding
        results.append({"case": "opportunity_before_reference" if preceding == 1 else "opportunity_after_reference", "cards": 1, "real_authority": True})

    case = await setup(12)
    await at_boundary(case, 7, text="How was your day?")
    http, calls, events, stats = await turn(case, source=200, no_interest=True)
    assert http.posts and not any(x.get("price", 0) for x in http.posts)
    async with get_session() as session:
        a = json.loads((await session.get(OfConversationState, (case[0], case[1]))).commercial_state_json)["attempt"]
        assert a["status"] == "closed_no_offer" and a["reason"] == "no_current_interest" and a["resume_when"]
    results.append({"case": "no_opportunity_beyond_reference", "cards": 0, "real_authority": True})

    # A persistent explore at the boundary becomes a specific available item
    # invitation, never a card or another resettable warming-up cycle.
    case = await setup(13)
    await at_boundary(case, 5)
    http, calls, events, stats = await turn(case, source=200, hook_action="explore")
    assert http.posts and not any(x.get("price", 0) for x in http.posts)
    async with get_session() as session:
        a = json.loads((await session.get(OfConversationState, (case[0], case[1]))).commercial_state_json)["attempt"]
        assert a["status"] == "awaiting_answer" and a["bubbles"] >= 6 and a["invitation_id"]
    results.append({"case": "boundary_explore_becomes_specific_invitation", "cards": 0, "real_authority": True})

    case = await setup(14)
    await at_boundary(case, 5, text="What does the workout photo set include?")
    http, calls, events, stats = await turn(case, source=200, hook_action="clarify")
    assert http.posts and not any(x.get("price", 0) for x in http.posts), (http.posts, calls, events, stats)
    async with get_session() as session:
        a = json.loads((await session.get(OfConversationState, (case[0], case[1]))).commercial_state_json)["attempt"]
        assert a["extension_used"] and a["status"] == "clarification_pending"
    results.append({"case": "one_necessary_clarification", "cards": 0, "real_authority": True})

    case = await setup(15)
    await at_boundary(case, 6, text="What does the workout photo set include?", extension=True)
    http, calls, events, stats = await turn(case, source=200, hook_action="clarify")
    assert not any(x.get("price", 0) for x in http.posts)
    async with get_session() as session:
        a = json.loads((await session.get(OfConversationState, (case[0], case[1]))).commercial_state_json)["attempt"]
        assert a["status"] == "closed_no_offer" and a["reason"] == "review_required"
    results.append({"case": "second_extension_refused_and_recorded_for_review", "cards": 0, "real_authority": True})

    # A commercial hold racing with the planned invitation must be checked at
    # the existing text dispatch boundary, not only during product selection.
    case = await setup(16, initiative="lower")
    def hold_between_plan_and_send():
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        sync_engine = create_engine("sqlite:///"+str(path))
        with Session(sync_engine) as session:
            open_case(session, case[0], case[1], "complaint", reason="delivery_question",
                evidence={"source_message_id": 100}, source_message_id=100, scope="commercial")
            session.commit()
        sync_engine.dispose()
    http, calls, events, stats = await turn(case, before_wire=hold_between_plan_and_send, expected_stop="fan-case-complaint")
    assert not http.posts
    results.append({"case": "invitation_hold_race", "cards": 0, "real_authority": True})

    for n, change in enumerate(("edit", "human_takeover", "budget"), 17):
        case = await setup(n, text="show me your workout photos" if change == "budget" else "I love your workout photos")
        def mutate():
            with sqlite3.connect(path) as con:
                if change == "edit":
                    con.execute("UPDATE messages SET body='Changed request' WHERE account_id=? AND fan_id=? AND message_id=100", (case[0], case[1]))
                elif change == "human_takeover":
                    con.execute("UPDATE of_conversation_state SET mode='human_controlled',takeover_state='requested' WHERE account_id=? AND fan_id=?", (case[0], case[1]))
        if change == "budget":
            async with get_session() as session:
                row = await session.get(Message, (case[0], case[1], 100)); row.body = "show me your workout photos, my budget is $3 total"
                await session.commit()
        http, calls, events, stats = await turn(case, mutate=mutate if change != "budget" else None,
            expected_stop="source-changed" if change == "edit" else ("fan-takeover" if change == "human_takeover" else None))
        assert not any(x.get("price", 0) for x in http.posts), (change, http.posts, stats)
        results.append({"case": change, "cards": 0, "real_authority": True})

    async def next_fan(case, text):
        async with get_session() as session:
            from sqlalchemy import update
            await session.execute(update(Message).where(Message.account_id == case[0], Message.direction == "out").values(
                created_at=datetime.utcnow()-timedelta(seconds=120), ingested_at=datetime.utcnow()-timedelta(seconds=119)))
            fanrow = await session.get(Fan, (case[0], case[1])); fanrow.automation_paused_until = None
            session.add(Message(account_id=case[0], fan_id=case[1], message_id=2000, direction="in", body=text,
                created_at=datetime.utcnow()-timedelta(seconds=20), ingested_at=datetime.utcnow()-timedelta(seconds=19)))
            await session.commit()

    case = await setup(20)
    http, calls, events, stats = await turn(case, hook_action="explore")
    assert http.posts and not any(x.get("price", 0) for x in http.posts)
    await next_fan(case, "show me your workout photos")
    with patch.object(FakeHTTP, "request", ids_after_input):
        http, calls, events, stats = await turn(case, source=2000, accept=True)
    assert len([x for x in http.posts if x.get("price", 0)]) == 1 and "content_resolver_hook" not in calls
    results.append({"case": "develop_then_fan_request", "cards": 1, "real_authority": True})

    for n, response in enumerate(("no thanks", "How was your day?"), 21):
        case = await setup(n, initiative="lower")
        http, calls, events, stats = await turn(case)
        assert http.posts and not any(x.get("price", 0) for x in http.posts)
        await next_fan(case, response)
        with patch.object(FakeHTTP, "request", ids_after_input):
            http, calls, events, stats = await turn(case, source=2000, no_interest=True, chat_reply="Understood, thanks for saying so." if n == 21 else "My day was relaxed, thanks for asking.")
        assert not any(x.get("price", 0) for x in http.posts)
        async with get_session() as session:
            invitation = (await session.execute(select(OfSalesInvitation).where(OfSalesInvitation.account_id == case[0]))).scalar_one()
            assert invitation.state in ("declined", "ignored")
            state = await session.get(OfConversationState, (case[0], case[1]))
            assert json.loads(state.commercial_state_json)["attempt"]["status"] == "closed_no_offer"
        results.append({"case": "rejected_invitation" if n == 21 else "ignored_invitation", "cards": 0, "real_authority": True})

    # The single clarification of an ambiguous item invitation keeps the same
    # item/quote and its confirmed answer association through the next fan turn.
    case = await setup(23, initiative="lower")
    await turn(case)
    async with get_session() as session:
        cfgrow = await session.get(AccountAiConfig, case[0]); cfg = json.loads(cfgrow.ai_chatter_config_json)
        cfg["fastt_first_offer_window"] = [1,3]; cfgrow.ai_chatter_config_json = json.dumps(cfg)
        await session.commit()
    await next_fan(case, "maybe")
    with patch.object(FakeHTTP, "request", ids_after_input):
        http, calls, events, stats = await turn(case, source=2000, hook_action="clarify",
            clarification_kind="ambiguous_invitation", chat_reply="I mean the gym portrait photos.")
    assert http.posts and not any(x.get("price", 0) for x in http.posts)
    async with get_session() as session:
        a = json.loads((await session.get(OfConversationState, (case[0],case[1]))).commercial_state_json)["attempt"]
        assert a["extension_used"]
        invitation = (await session.execute(select(OfSalesInvitation).where(OfSalesInvitation.account_id==case[0]))).scalar_one()
        assert json.loads(invitation.offer_terms_json)["clarification"]["delivery_message_id"]
        from sqlalchemy import update
        await session.execute(update(Message).where(Message.account_id==case[0],Message.direction=="out").values(
            created_at=datetime.utcnow()-timedelta(seconds=120), ingested_at=datetime.utcnow()-timedelta(seconds=119)))
        fanrow=await session.get(Fan,(case[0],case[1]));fanrow.automation_paused_until=None
        session.add(Message(account_id=case[0],fan_id=case[1],message_id=4000,direction="in",body="yes",
            created_at=datetime.utcnow()-timedelta(seconds=20),ingested_at=datetime.utcnow()-timedelta(seconds=19)))
        await session.commit()
    def ids_after_clarification(self,*args,**kw):
        response=original(self,*args,**kw);response.data["id"]+=4000;self.history[0]=dict(response.data);return response
    with patch.object(FakeHTTP,"request",ids_after_clarification):
        http,calls,events,stats=await turn(case,source=4000,accept=True)
    assert len([x for x in http.posts if x.get("price",0)])==1 and "content_resolver_hook" not in calls
    results.append({"case":"ambiguous_invitation_one_clarification_then_acceptance","cards":1,"real_authority":True})

    # Contextual shadow is not a revocation of the separately saved, existing
    # on-request sales permission. It must not write hypothetical attempt state.
    case=await setup(24,mode="shadow",text="show me your workout photos")
    http,calls,events,stats=await turn(case)
    assert len([x for x in http.posts if x.get("price",0)])==1
    async with get_session() as session:
        assert (await session.get(OfConversationState,(case[0],case[1]))).commercial_state_json is None
    results.append({"case":"shadow_preserves_separate_existing_direct_permission","cards":1,"real_authority":True})

    case=await setup(25,text="show me your workout photos, my budget is $50")
    http,calls,events,stats=await turn(case)
    cards=[x for x in http.posts if x.get("price",0)]
    assert len(cards)==1 and round(cards[0]["price"]*100)<=5000
    results.append({"case":"affordable_stated_budget_preserves_eligible_offer","cards":1,"real_authority":True})

    case=await setup(26)
    http,calls,events,stats=await turn(case,mirror_native_pointer=True)
    assert len([x for x in http.posts if x.get("price",0)])==1 and len(http.posts)>=2
    async with get_session() as session:
        state=await session.get(OfConversationState,(case[0],case[1]))
        assert state.unresolved_attempt_id and json.loads(state.commercial_state_json)["attempt"]["status"]=="offered"
    results.append({"case":"confirmed_native_pointer_allows_own_following_bubbles_without_releasing_lease","cards":1,"real_authority":True})

    from automations.sales_decisions import report
    async with get_session() as session:
        accounts = list((await session.execute(select(Account.id))).scalars())
    reports = {account: await report(account) for account in accounts}
    await engine.dispose()
    print(json.dumps({"transport": "synthetic_model_and_http_only", "scenarios": results, "reports": reports}))

def main():
    with tempfile.TemporaryDirectory(prefix="commercial-full-path-") as tmp:
        path = Path(tmp)/"test.db"
        os.environ["TWIN_FASTT_CONFIG_ISOLATED"] = "1"
        os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///"+str(path)
        os.environ["VAULT_SPOOL_DIR"] = str(Path(tmp)/"spool")
        os.environ["VAULT_IMAGE_DIR"] = str(Path(tmp)/"images")
        # The unread-flag journal otherwise defaults to the production spool volume (/var/lib/twin-vault), which a
        # read-only test container (rightly) cannot create; keep this isolated fixture on private temp paths.
        os.environ["ONLYFANS_UNREAD_JOURNAL_DIR"] = str(Path(tmp)/"unread-journal")
        os.environ.pop("ONLYFANS_PROCESS_ROLE", None)
        def deny(*a, **kw): raise AssertionError("network forbidden in isolated fixture")
        with asyncio.Runner() as runner:
            runner.get_loop()
            with patch.object(socket.socket, "connect", deny), patch.object(socket, "create_connection", deny):
                runner.run(exercise(path))

def test_full_path():
    result = subprocess.run([sys.executable, __file__], capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout+result.stderr

if __name__ == "__main__":
    main()

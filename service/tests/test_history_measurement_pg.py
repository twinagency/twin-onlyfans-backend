"""Measurement run on a disposable database with the fake provider: ten creators, explicit budgets, one worker loop.

Records requests, credits, rate and completeness per account and checks they reconcile with what the provider
actually saw. No real provider, model or credential is involved.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_commercial_preparation_pg import TENANT
from history_support import Provider, chat_page, fan_message, next_url, prepared, run, tx_page, tx_row
from conversation_engine import preparation as prep
from test_history_import_pg import hist, models

FANS = range(500, 508)


def test_ten_creators_drain_their_histories_within_explicit_budgets_and_the_meter_matches_the_wire(database):
    Source = models().OfFansImportSource
    accounts = []
    try:
        for i in range(1, 11):
            store, account, cfg = prepared(database, i, requests=400, credits=800)
            wire = Provider(cfg)
            for fan in FANS:
                base = 1000 * i + fan
                wire.chats[fan] = [chat_page([fan_message(base * 10 + 1, fan)], next_url(cfg, fan, base * 10)), chat_page([fan_message(base * 10 - 1, fan)])]
                hist().enlist_fan(store, account, fan, priority=3)
            wire.tx = [tx_page([tx_row("t%d-%d" % (i, n), 500 + n % 8) for n in range(20)], "m1"), tx_page([tx_row("t%d-x" % i, 500)])]
            accounts.append((store, account, wire))
        done_at = {}
        for round_number in range(1, 80):
            for store, account, wire in accounts:
                if account not in done_at and run(store, account, wire)["state"] == "complete":
                    done_at[account] = round_number
            if len(done_at) == 10:
                break
        assert len(done_at) == 10, "every creator finished inside the loop; none starved"
        assert max(done_at.values()) - min(done_at.values()) <= 1, "identical work finishes in lockstep"
        for store, account, wire in accounts:
            shown = hist().coverage_view(store, TENANT, account)
            measured = shown["measurement"]
            with Session(database[1]) as s:
                used = {r.source: (r.requests_used, r.credits_reserved) for r in s.scalars(select(Source).where(Source.account_id == account))}
            seen = {k: wire.count(k) for k in ("vault", "chats", "transactions", "chargebacks")}
            assert seen == {"vault": used["vault_window"][0], "chats": used["chats"][0], "transactions": used["transactions"][0], "chargebacks": used["chargebacks"][0]}
            assert seen["chats"] == 16 and seen["transactions"] == 2 and seen["chargebacks"] == 1
            assert measured["total"]["requests"] == sum(seen.values()) and measured["total"]["dispatchedUnrecorded"] == 0
            assert measured["total"]["ok"] == measured["total"]["requests"] and measured["total"]["creditsReserved"] == 2 * measured["total"]["requests"]
            scope = prep.view(store, TENANT, account)
            assert scope["providerRequestsUsed"] == sum(seen.values()) + wire.count("identity") <= 400
            assert shown["fans"]["known"] == 8 and shown["fans"]["complete"] == 8 and shown["sources"]["transactions"]["counts"]["new"] == 21
            assert measured["requestsPerHour"] > 0 and shown["state"] == "partial", "fans are listed, but the universe is never claimed enumerated"
        reads_per_account = sum(accounts[0][2].count(k) for k in ("chats", "transactions", "chargebacks", "vault"))
        assert reads_per_account == 20
    finally:
        for store, _, _ in accounts:
            store.close()

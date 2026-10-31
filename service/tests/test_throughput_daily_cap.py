"""B1: the account's optional DAILY send cap (rolling 24 h, beside the lifetime limit_units). Plain-assert house style.

NULL daily_limit_units / a store without the column = exactly the old behaviour. With a cap: the Nth+1 admission in 24 h
is refused with ``allowance-daily-cap`` (no model call is spent on it), unknown outcomes stay charged, a definite
non-delivery gives its place back, the window rolls, and the lifetime limit_units is untouched."""
import os, sys, tempfile, sqlite3
from pathlib import Path
from datetime import timedelta
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
import test_native_pilot_ledger as L
from native_pilot.ledger import Ledger, Refused
import native_pilot.tuning as _tuning
_tuning.MIN_SEND_INTERVAL_FLOOR_S = 0.0           # this suite drives 0 s throttles (the 20 s floor is a code constant)
os.environ["FASTT_FAN_DAILY_REPLY_LIMIT"] = "2000"


def setup(root, name, *, column=True, cap=None):
    f = L.Fixture(root, name); f.activate()
    cols = "account_id TEXT PRIMARY KEY,enabled INTEGER,limit_units INTEGER,used_units INTEGER,throttle_seconds INTEGER,next_dispatch_at TEXT,active_intent_id TEXT"
    f.sql("CREATE TABLE of_delivery_policy(" + cols + (",daily_limit_units INTEGER" if column else "") + ")")
    if column:
        f.sql("INSERT INTO of_delivery_policy VALUES(?,1,500,0,0,NULL,NULL,?)", (L.ACCT, cap))
    else:
        f.sql("INSERT INTO of_delivery_policy VALUES(?,1,500,0,0,NULL,NULL)", (L.ACCT,))
    return f, Ledger(f.path, allowance=True)


def seed(f, account, fan, n, when, state="sent", start=1000):
    with sqlite3.connect(f.path) as c:
        for i in range(n):
            aid = start + i
            c.execute("INSERT INTO twin_bridge_send_attempts(id,account_id,fan_id,turn_id,attempt_no,state,owner_token,revision,created_at) "
                      "VALUES(?,?,?,?,1,?,'seed',1,?)", (aid, account, fan, str(aid), state, when))
            c.execute("INSERT INTO native_pilot_receipts(attempt_id,pilot_id,input_key,bubble_no,state,created_at,updated_at) "
                      "VALUES(?,'seed',?,1,?,?,?)", (aid, str(aid), state, when, when))


def admit(ledger, fan, when, aid):
    try:
        with ledger._tx() as c:
            ledger._consume_allowance(c, L.ACCT, when, fan_id=fan, daily_limit=ledger.fan_daily_limit)
            c.execute("INSERT INTO twin_bridge_send_attempts(id,account_id,fan_id,turn_id,attempt_no,state,owner_token,revision,created_at) "
                      "VALUES(?,?,?,?,1,'dispatching','test',1,?)", (aid, L.ACCT, fan, str(aid), when))
            c.execute("INSERT INTO native_pilot_receipts(attempt_id,pilot_id,input_key,bubble_no,state,created_at,updated_at) "
                      "VALUES(?,'test',?,1,'dispatching',?,?)", (aid, str(aid), when, when))
        return "admitted"
    except Refused as exc:
        return exc.reason


now = L.NOW.isoformat(sep=" ", timespec="microseconds")
CHECKS = 0


def check(v, label=""):
    global CHECKS
    assert v, label
    CHECKS += 1


with tempfile.TemporaryDirectory() as d:
    # No cap (NULL): unchanged. Many fans, one account, nothing refused by the daily rule.
    f, led = setup(Path(d), "nullcap", cap=None)
    seed(f, L.ACCT, L.FAN, 50, now)
    s = led.allowance_status(L.ACCT, L.FAN, now=L.NOW)
    check(s["daily_limit"] is None and s["daily_used"] is None, "no cap reported as None")
    check(admit(led, L.FAN + 1, now, 9000) == "admitted", "NULL cap admits")

with tempfile.TemporaryDirectory() as d:
    # A store without the column at all (older schema / fixture) behaves as no cap.
    f, led = setup(Path(d), "nocolumn", column=False)
    check(led.allowance_status(L.ACCT, L.FAN, now=L.NOW)["daily_limit"] is None, "missing column reads as no cap")
    check(admit(led, L.FAN, now, 9001) == "admitted", "missing column admits")

with tempfile.TemporaryDirectory() as d:
    f, led = setup(Path(d), "cap", cap=10)
    seed(f, L.ACCT, L.FAN, 4, now); seed(f, L.ACCT, L.FAN + 1, 5, now, start=2000)       # 9 sent across two fans
    seed(f, "another-creator", L.FAN, 30, now, start=5000)                                  # other accounts never count
    s = led.allowance_status(L.ACCT, L.FAN, now=L.NOW)
    check((s["daily_limit"], s["daily_used"]) == (10, 9), f"daily status {s}")
    check(admit(led, L.FAN + 2, now, 9100) == "admitted", "the 10th is admitted")
    check(admit(led, L.FAN + 3, now, 9101) == "allowance-daily-cap", "the 11th is refused with the daily-cap reason")
    # Lifetime counters are not the daily cap: used_units/limit_units are untouched by daily mode.
    check(f.sql("SELECT limit_units,used_units FROM of_delivery_policy")[0] == (500, 0), "lifetime columns untouched")
    # Unknown outcomes stay charged, certain non-delivery gives the place back.
    f.sql("UPDATE native_pilot_receipts SET state='delivery_unknown' WHERE attempt_id=1000")
    f.sql("UPDATE native_pilot_receipts SET state='not_sent' WHERE attempt_id=1001")
    check(led.allowance_status(L.ACCT, L.FAN, now=L.NOW)["daily_used"] == 9, "unknown charged, not_sent released")
    f.sql("UPDATE twin_bridge_send_attempts SET state='sent' WHERE state='dispatching'")
    f.sql("UPDATE native_pilot_receipts SET state='sent' WHERE state='dispatching'")      # (one in-flight attempt at a time)
    check(admit(led, L.FAN + 3, now, 9102) == "admitted", "the released place can be used")
    f.sql("UPDATE twin_bridge_send_attempts SET state='sent' WHERE state='dispatching'")
    f.sql("UPDATE native_pilot_receipts SET state='sent' WHERE state='dispatching'")
    check(admit(led, L.FAN + 4, now, 9103) == "allowance-daily-cap", "and the cap binds again")
    # The window rolls: 24 h later the old sends no longer count (only the three just admitted are 'now').
    later = L.NOW + timedelta(hours=24, seconds=1)
    check(led.allowance_status(L.ACCT, L.FAN, now=later)["daily_used"] == 0, "window rolled")
    later_s = later.isoformat(sep=" ", timespec="microseconds")
    check(admit(led, L.FAN + 5, later_s, 9200) == "admitted", "admits again after the window")
    f.sql("UPDATE twin_bridge_send_attempts SET state='sent' WHERE state='dispatching'")
    f.sql("UPDATE native_pilot_receipts SET state='sent' WHERE state='dispatching'")
    # Raising the cap takes effect at once; clearing it (NULL) removes it.
    f.sql("UPDATE of_delivery_policy SET daily_limit_units=NULL")
    check(admit(led, L.FAN + 6, later_s, 9201) == "admitted", "NULL again = no cap")
    f.sql("UPDATE twin_bridge_send_attempts SET state='sent' WHERE state='dispatching'")
    f.sql("UPDATE native_pilot_receipts SET state='sent' WHERE state='dispatching'")
    f.sql("UPDATE of_delivery_policy SET daily_limit_units=0")
    check(admit(led, L.FAN + 7, later_s, 9202) == "allowance-daily-cap", "cap 0 blocks everything")

print(f"PASS daily send cap: {CHECKS} checks")

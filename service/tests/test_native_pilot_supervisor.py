"""Runner supervisor: desired set, per-account children, restart/backoff/hold, health. Fake children only.

SQLite scratch tables stand in for the application tables; the process spawner, clock and sleep are
injected, so no OnlyFans, model, network or real subprocess is involved.
"""
import json, os, sqlite3, sys, tempfile, threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_pilot import supervisor as S

A, B = 'of-900001', 'of-900002'
checks = 0


def check(cond, label=''):
    global checks
    assert cond, label
    checks += 1


class Child:
    def __init__(self, argv, account, ignore_term=False):
        self.argv, self.account, self.code, self.ignore_term = argv, account, None, ignore_term
        self.signals = []

    def poll(self):
        return self.code

    def terminate(self):
        self.signals.append('TERM')
        if not self.ignore_term and self.code is None:
            self.code = 0

    def kill(self):
        self.signals.append('KILL')
        self.code = -9


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def make_db(path):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE of_runtime (account_id TEXT PRIMARY KEY, binding_id TEXT, enabled INTEGER);
        CREATE TABLE of_creator_bindings (id TEXT PRIMARY KEY, account_id TEXT, channel TEXT, handle TEXT, revoked_at TEXT);
        CREATE TABLE of_delivery_policy (account_id TEXT PRIMARY KEY, enabled INTEGER);
        CREATE TABLE of_provider_connection (account_id TEXT PRIMARY KEY, binding_id TEXT, owner_id INTEGER, status TEXT,
            verified_at TEXT, last_error TEXT, expires_at TEXT, sealed_config TEXT);
        CREATE TABLE catalog_items (id INTEGER PRIMARY KEY, account_id TEXT, kind TEXT, media_ids TEXT,
            preview_media_ids TEXT, price_cents INTEGER, is_free_teaser INTEGER, enabled INTEGER, pricing_mode TEXT);
        CREATE TABLE vault_items (account_id TEXT, media_id INTEGER, kind TEXT, PRIMARY KEY (account_id, media_id));
    """)
    future = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)).isoformat(sep=' ')
    for n, acct in enumerate((A, B), 1):
        bid = f'bind-{n}'
        con.execute("INSERT INTO of_runtime VALUES (?,?,1)", (acct, bid))
        con.execute("INSERT INTO of_creator_bindings VALUES (?,?,?,?,NULL)", (bid, acct, 'onlyfans', f'@creator{n}'))
        con.execute("INSERT INTO of_delivery_policy VALUES (?,1)", (acct,))
        con.execute("INSERT INTO of_provider_connection VALUES (?,?,?,?,?,NULL,?,?)",
                    (acct, bid, int(acct[3:]), 'verified', '2026-01-01 00:00:00', future, 'SEALED-SECRET'))
        con.execute("INSERT INTO vault_items VALUES (?,?,?)", (acct, 1001, 'photo'))
        con.execute("INSERT INTO catalog_items VALUES (?,?,?,?,?,?,0,1,'fixed')",
                    (n, acct, 'photo', '[1001]', '[]', 500))
    con.commit()
    return con


class Rig:
    def __init__(self, tmp, scope, handles=None):
        self.db = str(Path(tmp) / 'app.db')
        self.con = make_db(self.db)
        self.clock = Clock()
        self.events, self.children = [], []
        self.scope = scope
        self.sup = S.Supervisor(self.db, manifest_dir=Path(tmp) / 'manifests', status_path=Path(tmp) / 'status.json',
                                handles={} if handles is None else handles, spawn=self.spawn,
                                scope_reader=lambda: self.scope, clock=self.clock, sleep=self.clock.sleep,
                                wall=lambda: datetime(2026, 10, 24), emit=self.events.append,
                                interval_s=30, grace_s=60, poll_s=1)
        self.ignore_term = set()

    def spawn(self, argv, account):
        child = Child(argv, account, ignore_term=account in self.ignore_term)
        self.children.append(child)
        return child

    def sql(self, q, *a):
        self.con.execute(q, a)
        self.con.commit()

    def tick(self, seconds=0.0):
        self.clock.t += seconds
        self.sup.step()

    def live(self, account):
        return [c for c in self.children if c.account == account and c.code is None]

    def state(self, account):
        s = self.sup.status()['accounts'].get(account)
        return None if s is None else (s['state'], s['reason'])

    def names(self, name):
        return [e for e in self.events if e['event'] == name]


ALL = (set(), {'900001', '900002'})

with tempfile.TemporaryDirectory() as tmp:
    # Explicit chat-only mode works without an approved paid-media catalogue.
    r = Rig(tmp, ({('900001', 7)}, {'900002'}))
    r.sql("CREATE TABLE account_ai_config (account_id TEXT, ai_chatter_config_json TEXT)")
    r.sql("INSERT INTO account_ai_config VALUES (?,?)", A, json.dumps({'fastt_text_only': True}))
    r.sql("DELETE FROM catalog_items WHERE account_id=?", A)
    r.tick()
    a = r.live(A)[0]
    check(a.argv == ['--account', A, '--fan', '7', '--handle', 'creator1', '--twin-context', '--enable-send'], a.argv)
    check(not (Path(tmp) / 'manifests' / f'{A}.json').exists(), 'chat-only creates no paid-media manifest')
    check('--content-manifest' in r.live(B)[0].argv, 'other accounts keep their content configuration')
    check(S.text_only_enabled(r.db, A) and not S.text_only_enabled(r.db, B), 'chat-only flag is account scoped')
    r.sql("UPDATE account_ai_config SET ai_chatter_config_json=? WHERE account_id=?", json.dumps({'fastt_text_only': 'true'}), A)
    r.tick(30)
    check(r.state(A) == ('held', 'manifest:no-approved-items'), 'string true never enables chat-only')
    r.sql("UPDATE account_ai_config SET ai_chatter_config_json=? WHERE account_id=?", json.dumps({'fastt_text_only': True, 'fastt_vault_mode': True}), A)
    r.tick(30)
    check(r.state(A) == ('held', 'content-mode-conflict') and not r.live(A), 'conflicting content modes fail closed')
    r.sql("UPDATE account_ai_config SET ai_chatter_config_json=? WHERE account_id=?", json.dumps({'fastt_text_only': True}), A)
    r.sql("UPDATE of_runtime SET enabled=0 WHERE account_id=?", A)
    r.tick(30)
    check(r.state(A) == ('held', 'runtime-disabled'), 'chat-only preserves account activation gate')

with tempfile.TemporaryDirectory() as tmp:
    # --- start: exact arguments, manifest written privately, status file, handle from the binding ---
    r = Rig(tmp, ALL)
    r.tick()
    check(len(r.children) == 2, 'one child per account')
    a = next(c for c in r.children if c.account == A)
    manifest_path = str(Path(tmp) / 'manifests' / f'{A}.json')
    check(a.argv == ['--account', A, '--all-fans', '--handle', 'creator1', '--twin-context',
                     '--content-manifest', manifest_path, '--enable-send'], a.argv)
    check(json.loads(Path(manifest_path).read_text())['account_id'] == A, 'manifest written')
    check(oct(os.stat(manifest_path).st_mode & 0o777) == '0o600', 'manifest private')
    status = json.loads((Path(tmp) / 'status.json').read_text())
    check(status['accounts'][A]['state'] == 'running' and 'pid' not in json.dumps(status), 'status file, no pid')
    check('SEALED-SECRET' not in json.dumps(status) + json.dumps(r.events), 'no credential anywhere')

    # --- fastt_followups: boolean true (and only that) passes --followups; default and other accounts stay as before ---
    with tempfile.TemporaryDirectory() as tmp2:
        r2 = Rig(tmp2, ALL)
        r2.sql("CREATE TABLE account_ai_config (account_id TEXT, ai_chatter_config_json TEXT)")
        r2.sql("INSERT INTO account_ai_config VALUES (?,?)", A, json.dumps({'fastt_followups': True}))
        r2.sql("INSERT INTO account_ai_config VALUES (?,?)", B, json.dumps({'fastt_followups': 'true'}))
        r2.tick()
        a2 = next(c for c in r2.children if c.account == A)
        b2 = next(c for c in r2.children if c.account == B)
        mp = str(Path(tmp2) / 'manifests' / f'{A}.json')
        check(a2.argv == ['--account', A, '--all-fans', '--handle', 'creator1', '--twin-context', '--followups',
                          '--content-manifest', mp, '--enable-send'], a2.argv)
        check('--followups' not in b2.argv, 'only boolean true enables the follow-ups')
        check(S.followups_enabled(r2.db, A) is True and S.followups_enabled(r2.db, B) is False
              and S.followups_enabled(r2.db, '999999') is False)
        # Switching it off in the config restarts that child without the flag.
        r2.sql("UPDATE account_ai_config SET ai_chatter_config_json=? WHERE account_id=?", json.dumps({}), A)
        for _ in range(6):
            r2.tick(31)
        check(any('--followups' not in c.argv for c in r2.children if c.account == A and c.code is None), 'toggled off: restarted without the flag')

    # --- unchanged config keeps the same child ---
    for _ in range(5):
        r.tick(31)
    check(len(r.children) == 2 and a.signals == [], 'unchanged config: same child, no signal')

    # --- manifest change restarts exactly once ---
    r.sql("UPDATE catalog_items SET price_cents=700 WHERE account_id=?", A)
    r.tick(31)
    check(a.signals == ['TERM'] and a.code == 0, 'changed manifest: SIGTERM')
    check(len(r.live(A)) == 1 and r.live(A)[0] is not a and len(r.children) == 3, 'new child started once')
    check(r.sup.status()['accounts'][A]['restarts'] == 1, 'counted as one restart')
    for _ in range(3):
        r.tick(31)
    check(len(r.children) == 3, 'no further restart')
    check(json.loads(Path(manifest_path).read_text())['items'][0]['price_cents'] == 700, 'new manifest on disk')

    # --- handle change via env map restarts; env map beats the stored handle ---
    r.sup.handles = {'900001': 'renamed'}
    r.tick(31)
    check(r.live(A)[0].argv[r.live(A)[0].argv.index('--handle') + 1] == 'renamed' and len(r.children) == 4, 'handle map')

    # --- revoke / disable stops only that account ---
    b = r.live(B)[0]
    r.sql("UPDATE of_creator_bindings SET revoked_at='2026-10-01 00:00:00' WHERE account_id=?", B)
    r.tick(31)
    check(b.signals == ['TERM'] and r.state(B) == ('held', 'binding-revoked') and not r.live(B), 'revoke stops')
    check(len(r.live(A)) == 1, 'other account untouched')
    r.sql("UPDATE of_creator_bindings SET revoked_at=NULL WHERE account_id=?", B)
    r.tick(31)
    check(len(r.live(B)) == 1, 'reinstated account starts again')
    for q, reason in (("UPDATE of_runtime SET enabled=0 WHERE account_id=?", 'runtime-disabled'),
                      ("UPDATE of_delivery_policy SET enabled=0 WHERE account_id=?", 'policy-disabled'),
                      ("UPDATE of_provider_connection SET status='expired' WHERE account_id=?", 'connection-not-verified'),
                      ("UPDATE of_provider_connection SET last_error='x' WHERE account_id=?", 'connection-check-failing'),
                      ("UPDATE of_provider_connection SET expires_at='2020-01-01 00:00:00' WHERE account_id=?", 'connection-expired'),
                      ("UPDATE of_provider_connection SET owner_id=5 WHERE account_id=?", 'connection-owner-mismatch')):
        snap = {k: r.con.execute(f"SELECT * FROM {k} WHERE account_id=?", (B,)).fetchone()
                for k in ('of_runtime', 'of_delivery_policy', 'of_provider_connection')}
        r.sql(q, B)
        r.tick(31)
        check(r.state(B) == ('held', reason) and not r.live(B), reason)
        for k, row in snap.items():
            r.sql(f"DELETE FROM {k} WHERE account_id=?", B)
            r.sql(f"INSERT INTO {k} VALUES ({','.join('?' * len(row))})", *row)
        r.tick(31)
        check(len(r.live(B)) == 1, 'restored after ' + reason)

    # --- supervisor stop terminates every child, then finishes ---
    live = r.live(A) + r.live(B)
    r.sup.shutdown()
    check(all(c.code is not None and c.signals == ['TERM'] for c in live), 'stop: all children SIGTERMed cleanly')
    check(not r.live(A) and not r.live(B) and r.names('supervisor-finished'), 'stop: finished')
    check(json.loads((Path(tmp) / 'status.json').read_text())['accounts'][A]['state'] == 'held', 'stop: status held')

with tempfile.TemporaryDirectory() as tmp:
    # --- crash backoff, crash-loop hold, isolation ---
    r = Rig(tmp, ALL)
    r.tick()
    b = r.live(B)[0]
    delays = []
    for n in range(4):
        r.live(A)[0].code = 1
        r.tick(1)
        ev = r.names('child-exit')[-1]
        delays.append(ev['restart_in_s'])
        check(r.state(A) == ('restarting', 'exit:1') and not r.live(A), 'restarting after exit')
        r.tick(ev['restart_in_s'] - 1)
        check(not r.live(A), 'not before the backoff')
        r.tick(1.5)
        check(len(r.live(A)) == 1, 'restarted after the backoff')
    check(delays == [5, 10, 20, 40], delays)
    r.live(A)[0].code = 1
    r.tick(1)
    check(r.state(A) == ('held', 'crash-loop') and r.names('crash-loop'), '5th exit in 10 min holds')
    n_children = len(r.children)
    for _ in range(8):
        r.tick(31)
    check(len(r.children) == n_children and r.state(A) == ('held', 'crash-loop'), 'held until config changes')
    check(r.live(B) == [b] and b.signals == [] and r.state(B) == ('running', None), 'other account never touched')
    r.sql("UPDATE catalog_items SET price_cents=900 WHERE account_id=?", A)
    r.tick(31)
    check(len(r.live(A)) == 1 and r.state(A) == ('running', None), 'config change clears the crash-loop hold')

    # a spawn failure is an exit too
    r2 = Rig(tempfile.mkdtemp(dir=tmp), ALL)
    boom = {'on': True}
    real = r2.spawn
    r2.sup.spawn = lambda argv, account: (_ for _ in ()).throw(OSError('x')) if boom['on'] and account == A else real(argv, account)
    r2.tick()
    check(r2.state(A) == ('restarting', 'exit:spawn-failed') and len(r2.live(B)) == 1, 'spawn failure backs off, other runs')
    boom['on'] = False
    r2.tick(6)
    check(len(r2.live(A)) == 1, 'recovers when spawn works')

    # exits older than 10 minutes do not count toward the loop
    r3 = Rig(tempfile.mkdtemp(dir=tmp), (set(), {'900001'}))
    r3.tick()
    for _ in range(6):
        r3.live(A)[0].code = 1
        r3.tick(1)
        r3.tick(700)
    check(r3.state(A) == ('running', None), 'spaced exits never loop')

with tempfile.TemporaryDirectory() as tmp:
    # --- stuck child: SIGTERM, wait the grace, SIGKILL only then; others are not blocked ---
    r = Rig(tmp, ALL)
    r.ignore_term = {A}
    r.tick()
    a, b = r.live(A)[0], r.live(B)[0]
    r.sql("UPDATE catalog_items SET price_cents=800 WHERE account_id=?", A)
    r.sql("UPDATE catalog_items SET price_cents=800 WHERE account_id=?", B)
    r.tick(31)
    check(a.signals == ['TERM'] and r.state(A) == ('stopping', 'stopping'), 'stuck child asked to stop')
    check(b.signals == ['TERM'] and len(r.live(B)) == 1 and r.live(B)[0] is not b, 'other account restarted without waiting')
    r.tick(30)
    check(a.signals == ['TERM'], 'no kill inside the grace')
    r.tick(31)
    check(a.signals == ['TERM', 'KILL'] and len(r.live(A)) == 1 and r.live(A)[0] is not a, 'KILL after the grace, then new child')

    # supervisor stop with a stuck child: TERM all, KILL the straggler after the grace
    r.ignore_term = {A}
    stuck = r.live(A)[0]
    stuck.ignore_term = True
    other = r.live(B)[0]
    r.sup.shutdown()
    check(other.signals == ['TERM'] and stuck.signals == ['TERM', 'KILL'] and not r.live(A) and not r.live(B), 'stop kills only stragglers')

with tempfile.TemporaryDirectory() as tmp:
    # --- scope: one pair -> --fan; several pairs -> held; account outside scope never starts ---
    r = Rig(tmp, ({('900001', 7)}, set()))
    r.tick()
    check(len(r.children) == 1 and '--fan' in r.children[0].argv and r.children[0].argv[r.children[0].argv.index('--fan') + 1] == '7'
          and '--all-fans' not in r.children[0].argv, 'single pair -> --fan')
    check(json.loads((Path(tmp) / 'manifests' / f'{A}.json').read_text())['fan_id'] == 7, 'manifest is fan-scoped')
    check(r.state(B) is None, 'account outside the scope is not started or listed')
    # several selected fans: ONE all-fans runner (it serves only the selected ones through its ownership filter)
    first = r.live(A)[0]
    r.scope = ({('900001', 7), ('900001', 8)}, set())
    r.tick(31)
    check(first.signals == ['TERM'] and len(r.live(A)) == 1 and '--all-fans' in r.live(A)[0].argv
          and '--fan' not in r.live(A)[0].argv and r.state(A) == ('running', None), 'several fans -> --all-fans, restarted once')
    check(json.loads((Path(tmp) / 'manifests' / f'{A}.json').read_text())['fan_id'] == '*', 'manifest is account-wide')
    n = len(r.children)
    r.scope = ({('900001', 8), ('900001', 9)}, set())     # membership change, same args and manifest: no restart
    r.tick(31)
    check(len(r.children) == n and len(r.live(A)) == 1, 'a changed selection that keeps --all-fans is not a restart')
    r.scope = ({('900001', 8)}, set())                    # one fan again -> --fan 8, restarted once
    r.tick(31)
    check(len(r.children) == n + 1 and r.live(A)[0].argv[r.live(A)[0].argv.index('--fan') + 1] == '8', 'single again -> --fan')
    r.tick(31)
    check(len(r.children) == n + 1, 'a scope that did not change restarts nothing')
    r.scope = ({('900001', 7), ('900001', 8)}, set())
    r.tick(31)
    r.scope = ({('900001', 7), ('900001', 8)}, {'900001'})
    r.tick(31)
    check(len(r.live(A)) == 1 and '--all-fans' in r.live(A)[0].argv, 'account-wide entry wins')

    # the real scope reader (env) gives the same answer
    with patch.dict(os.environ, {'ONLYFANS_FASTT_SCOPE': 'of-900001:*,900002:5'}):
        check(S.read_scope() == ({('900002', 5)}, {'900001'}), 'real _scope reader')

    # database scope through the real reader: per-mode args, and a change restarts the child once
    from onlyfans_runtime import fastt_scope as FS
    rows, clk = {}, [5000.0]
    def reader(ids):
        return {i: (None if rows.get(i) is None else json.dumps({'fastt_scope': rows[i]})) for i in ids}
    os.makedirs(Path(tmp) / 'r2')
    r2 = Rig(Path(tmp) / 'r2', None)
    r2.sup.scope_reader = S.read_scope
    with patch.dict(os.environ, {'ONLYFANS_FASTT_SCOPE': 'db:of-900001'}), patch.object(FS, '_reader', reader), \
            patch.object(FS, '_clock', lambda: clk[0]):
        FS.reset_cache()
        def at(seconds, value, name):
            rows['of-900001'] = value
            clk[0] += seconds
            r2.tick(seconds)
        at(0, {'mode': 'single', 'fans': [7]}, 'single')
        check(not r2.children and r2.state(A) is None, 'a database scope is not started before the settle wait')
        at(FS.SETTLE_S + 1, {'mode': 'single', 'fans': [7]}, 'single')
        check(len(r2.children) == 1 and r2.children[0].argv[r2.children[0].argv.index('--fan') + 1] == '7', 'single -> --fan 7')
        at(31, {'mode': 'single', 'fans': [7]}, 'same')
        check(len(r2.children) == 1, 'unchanged scope: no restart')
        at(31, {'mode': 'selected', 'fans': [7, 8]}, 'selected')        # 8 appears: settles first, 7 keeps serving
        check(len(r2.children) == 1, 'a new fan waits for the settle before the child changes')
        at(FS.SETTLE_S + 1, {'mode': 'selected', 'fans': [7, 8]}, 'selected')
        check(len(r2.children) == 2 and r2.children[0].signals == ['TERM'] and '--all-fans' in r2.children[1].argv, 'selected -> --all-fans, one restart')
        at(31, {'mode': 'all', 'fans': []}, 'all')
        at(FS.SETTLE_S + 1, {'mode': 'all', 'fans': []}, 'all')
        check(len(r2.children) == 2, 'all keeps --all-fans: same args, no restart')
        at(31, None, 'cleared')
        check(r2.state(A) is None or r2.state(A)[0] in ('stopping', 'held') and not r2.live(A), 'scope cleared: child stopped')

with tempfile.TemporaryDirectory() as tmp:
    # --- handle: env map, stored binding handle, missing, invalid ---
    r = Rig(tmp, ALL)
    r.sql("UPDATE of_creator_bindings SET handle='' WHERE account_id=?", A)
    r.sql("UPDATE of_creator_bindings SET handle='bad handle;rm' WHERE account_id=?", B)
    r.tick()
    check(r.state(A) == ('held', 'handle-missing') and r.state(B) == ('held', 'handle-invalid') and not r.children, 'handle holds')
    with patch.dict(os.environ, {'FASTT_HANDLES': 'of-900001:@mapped, 900002:other'}):
        check(S.parse_handles(os.environ['FASTT_HANDLES']) == {'900001': 'mapped', '900002': 'other'}, 'FASTT_HANDLES parse')
    r.sup.handles = S.parse_handles('of-900001:mapped')
    r.tick(31)
    check(len(r.live(A)) == 1 and r.state(B) == ('held', 'handle-invalid'), 'env map supplies the missing handle')

with tempfile.TemporaryDirectory() as tmp:
    # --- manifest blocker: held with the reason, no child; a running child is stopped; fix restarts ---
    r = Rig(tmp, ALL)
    r.sql("INSERT INTO catalog_items VALUES (50,?,?,?,?,?,0,1,'fixed')", A, 'photo', '[7777]', '[]', 500)
    r.tick()
    check(r.state(A) == ('held', 'manifest:media-not-in-vault') and not r.live(A) and len(r.live(B)) == 1, 'blocker held, no child')
    check(not (Path(tmp) / 'manifests' / f'{A}.json').exists(), 'no manifest written for a blocked account')
    r.sql("DELETE FROM catalog_items WHERE id=50")
    r.tick(31)
    check(len(r.live(A)) == 1, 'fixed catalogue starts the runner')
    r.sql("UPDATE catalog_items SET enabled=0 WHERE account_id=?", A)
    child = r.live(A)[0]
    r.tick(31)
    check(r.state(A) == ('held', 'manifest:no-approved-items') and child.signals == ['TERM'], 'new blocker stops the running child')

with tempfile.TemporaryDirectory() as tmp:
    # --- a failing database read neither stops nor restarts a running child ---
    r = Rig(tmp, ALL)
    r.tick()
    kids = list(r.children)
    good = r.sup.account_reader
    r.sup.account_reader = lambda db, account: (_ for _ in ()).throw(RuntimeError('db down'))
    r.tick(31)
    check(all(c.signals == [] and c.code is None for c in kids) and r.names('evaluate-error'), 'db outage keeps children')
    r.sup.account_reader = good
    r.tick(31)
    check(len(r.children) == 2, 'back to normal')
    # missing account row
    r.sql("DELETE FROM of_provider_connection WHERE account_id=?", A)
    r.tick(31)
    check(r.state(A) == ('held', 'connection-missing'), 'connection missing holds')

with tempfile.TemporaryDirectory() as tmp:
    # --- run() loop: stop event stops children and exits ---
    r = Rig(tmp, ALL)
    stop = threading.Event()
    r.sup.poll_s = 0.01
    r.sup.clock = __import__('time').monotonic
    t = threading.Thread(target=r.sup.run, args=(stop,))
    t.start()
    for _ in range(300):
        if len(r.children) == 2:
            break
        __import__('time').sleep(0.01)
    check(len(r.children) == 2, 'run loop starts children')
    stop.set()
    t.join(5)
    check(not t.is_alive() and all(c.code is not None for c in r.children), 'run loop stops children and exits')

print(f"native pilot supervisor: {checks} checks; all cases passed")

# Explicit dashboard handover

Owner-approved policy change, 28 October 2026 Brisbane.

Dashboard-managed FASTT conversations use the existing automated/paused/human mode and takeover state. Creator-authored provider history no longer vetoes a reply merely because its message ID is absent from the current attempt. Human operators must press Take over before chatting, including when using OnlyFans directly. Hand back and Resume restore AI control. A provider request already sent cannot be recalled.

The existing strict conversation-state check still runs during pacing, after history validation and immediately before the physical send. Unreadable/foreign history, new or edited fan input, source direction changes, account/recipient boundaries, unknown outcomes, purchases, budgets and ownership fences retain their checks. Bounded pilots without required dashboard state retain their conservative outbound veto.

Runtime change: native_pilot/runner.py and native_pilot/service.py only. No schema, prompt, price, selection, allowance or account-activation change.

Live-baseline isolated checks: runner 262, service 703, follow-ups 45; all passed with network disabled. These are 1,010 script assertions, not 1,010 acceptance tests. New cases cover equal/newer creator timestamps, explicit human/paused/requested states, takeover after admission, resume, missing/cross-account/cross-fan state, new fan input, changed direction, foreign history and uncertain-send non-replay. Existing bounded-pilot refusals remain asserted.

Source port in this commit is based on 98923531d0531944b8dfe5a28d24f03f2718f170 and preserves that candidate's receipt recovery. It is not the source of the whole live container. Production baseline was image 6e83c77f12e5e1e00d1ecab1b870f247065dbaf2d981bf12a3dc7e334f0b2076 with runner blob d886ed9d7f42b5f944c1e03f010d1a0e2e3fc436 and service blob dbbcd9ea67a9a3a39dc5f83ee0f8da972dcde97a. The identical handover change was tested against its actual Python source snapshot. Deployment status and immutable image IDs belong in the host release manifest; this commit alone does not claim deployment.

Integration into the wider reliability candidate requires rerunning its native suites and affected paired acceptance shards/aggregate at the new commit. The old 2,260-test evidence remains evidence for its original pinned commits only.

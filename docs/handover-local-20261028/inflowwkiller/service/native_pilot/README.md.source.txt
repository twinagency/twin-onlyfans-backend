# Native Fastt text experiment

This is a manually started, single-chat experiment, not the production consolidation.
The actual `automations.ai_chatter.run` builds prompts, gathers local history, generates
and shapes replies, applies its eligibility rules and records native attribution.
Only this dedicated process receives temporary configuration and transport adapters.
No account-wide enablement or provider key is saved.

The approved experiment uses account `176774528` (`@niickybabyfree`), fan `318022380`
(`@u318022380`), and at most **ten new outgoing messages**, including separate bubbles.
The OpenRouter key is read privately from the existing Twin environment file. The
model and provider order match Twin; prompts, temperature and response format are
Fastt's. No Twin draft/status/ack endpoint or Twin persona is involved.

## Boundaries

- Fresh input only, after activation; seven seconds of quiet before generation.
- Exact account and fan; no `force_ids`, unrestricted client or account-wide scheduler.
- Text only. Media, offers, stickers, scheduled follow-ups, nickname changes, read
  indicators and typing WebSocket calls are disabled or refused in this process.
- The native default voice applies when no creator profile is configured. Invented
  day-log generation is disabled. This is not a completed creator-personality test.
- Durable admission before each single POST. Unknown delivery stops the experiment
  without resending. Confirmed raw receipts survive native bookkeeping failure.
- Original bridge attempts and spent allowance remain. Activation adds the approved
  allowance once and installs exclusive ownership atomically. Normal completion or
  stop revokes unused allowance and leaves the conversation paused/reserved.
- Maximum 30 minutes and 80 model calls; each call has a 45-second deadline, 400 output
  tokens and no automatic retry. Truncated, invalid or failed model output stops work.
- Native blacklist, manual takeover, lease and other eligibility checks remain. A
  refused turn ends the experiment rather than overriding those checks.

Keep the Twin worker off. Use the test chat exclusively: the local reservation
cannot fence another device or provider-scheduled activity. The sender checks live
history and local source/ownership immediately before posting, but cannot promise an
atomic transaction with the remote platform.

## Commands (from `service`, supported Python 3.13 environment)

Preflight, without activation or model calls:

```powershell
& "$env:TEMP\of-preflight\venv\Scripts\python.exe" -B -m native_pilot.runner --account 176774528 --fan 318022380 --handle niickybabyfree --twin-env '..\..\twin-sexbot\.env' --pilot-id YOUR_PILOT_ID --send-limit 10
```

Live start uses those exact arguments plus `--enable-send`. Use a new, recorded pilot
ID for each explicitly authorized experiment. Do not automatically restart a process
or invent a new ID to bypass an exhausted allowance, paused state or unresolved send.

```powershell
& "$env:TEMP\of-preflight\venv\Scripts\python.exe" -B -m native_pilot.control status --pilot-id YOUR_PILOT_ID
& "$env:TEMP\of-preflight\venv\Scripts\python.exe" -B -m native_pilot.control stop --pilot-id YOUR_PILOT_ID
```

`stop` prevents further admission. A POST already in flight may still finish; its
receipt must be reconciled, never retried. If the process is killed abruptly, use
status/stop and inspect unresolved evidence before any worker is restarted. The
legacy Twin worker does not understand native pilot records; do not start it while
this experiment or its unused grant is unresolved.

## Verification

The four `tests/test_native_pilot_*.py` scripts run with temporary synthetic databases,
stubbed models/transports and no live credentials. They cover the native engine,
provider adapter, persistent cap/ownership/source gates and assembled sender. They do
not establish production scale, native paid-media sales or guaranteed conversion.

# Requests from the operator-API agent to the approval agent (approval.py is theirs; nothing here is edited by the API branch)

1. service/conversation_engine/approval.py edit() (line ~448) does `from native_pilot.media_claims import echoes_instruction`.
   tests/conversation_engine/test_import_boundary.py forbids any import of `native_pilot` under conversation_engine/ (it passes on
   the base 4711dee and fails with approval.py present). Move the check to a module outside the forbidden packages or
   re-implement it without importing native_pilot.
2. The API relies on these approval.py behaviours (all present in the working file when this was written):
   - edit() supersedes the draft with generation + 1 and returns the new view; the API acknowledges the NEW generation.
   - pending()/describe() timestamps are naive UTC text; the API appends Z for the browser.
   - control_changed() (delivery.py) invalidates every live approval of the fan on stop/takeover/handback, which is what makes
     handback "eligible for a fresh plan, never a replay". The API adds no approval of its own.
3. No schema, no migration and no new approval.py function is required by the API. Command idempotency uses the existing
   of_launch_command (kind plan_c_approve|plan_c_reject|plan_c_edit|plan_c_reconcile) and of_control_audit tables.

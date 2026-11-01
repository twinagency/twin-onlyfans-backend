"""Public reasons are fixed copy. Internal errors never cross the control boundary."""
REASONS = {
    'history_quarantined': ('history_repair_required', 'History import requires repair. Sending is held until conflicting history is verified.'),
    'history_not_current': ('history_repair_required', 'History import requires repair. This conversation is not yet verified.'),
    'control revision conflict': ('revision_changed', 'Conversation revision changed. The latest state has been requested. Review it before trying again.'),
    'revision conflict': ('revision_changed', 'Conversation revision changed. The latest state has been requested. Review it before trying again.'),
    'takeover_pending': ('takeover_pending', 'Takeover is still pending. Wait for the current delivery to finish.'),
    'unresolved_delivery': ('delivery_unresolved', 'Delivery remains unresolved. Check the existing attempt before sending again.'),
    'account delivery unresolved': ('delivery_unresolved', 'Delivery remains unresolved. Check the existing attempt before sending again.'),
    'account_disabled': ('account_disabled', 'Account automation is disabled. An account operator must review the intentional pause.'),
    'delivery_disabled': ('sender_unavailable', 'The sender is unavailable or disabled for this account.'),
    'contact_blocked': ('contact_blocked', 'This conversation is intentionally paused or contact is blocked.'),
    'human_control_required': ('takeover_required', 'Take over this conversation before sending a manual reply.'),
    'takeover belongs to another team member': ('owner_changed', 'Another team member owns this conversation. Refresh reply ownership.'),
    'owner_changed': ('owner_changed', 'Reply ownership changed. Refresh reply ownership.'),
    'operator case holds resumption': ('operator_case_open', 'An open safety case must be resolved by a person before automation can resume.'),
    'idempotency key conflict': ('command_conflict', 'This request key belongs to a different command. Review the existing outcome.'),
}

def public_refusal(reason):
    return REASONS.get(reason, ('control_conflict', 'This action is blocked by the current conversation state. Refresh reply ownership and check delivery details.'))

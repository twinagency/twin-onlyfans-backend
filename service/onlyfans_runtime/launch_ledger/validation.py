"""Value-free validation details shared by the ledger and its diagnostic."""
from ..native_protocol import NativeProtocolError


VALIDATION_PATHS = frozenset({
    "$", "list", "hasMore", "nextMarker", "id", "ownerId", "user", "user.id", "description",
    "status", "currency", "amount", "createdAt", "messageId", "clearedAt",
    "payoutPendingDays", "net", "fee", "vatAmount",
})
VALIDATION_CODES = frozenset({
    "invalid_json", "invalid_response_size", "invalid_page_list", "invalid_has_more", "invalid_next_marker",
    "empty_nonterminal_page", "invalid_row", "invalid_provider_id", "duplicate_provider_id",
    "invalid_owner_id", "owner_mismatch", "invalid_user", "invalid_fan_id", "fan_is_owner",
    "invalid_description", "unknown_kind", "invalid_status", "invalid_currency",
    "invalid_amount", "nonpositive_amount", "invalid_created_at", "outside_window", "invalid_window_order",
    "invalid_message_id", "unexpected_message_id", "missing_fan", "invalid_cleared_at",
    "cleared_before_created", "invalid_pending_days", "invalid_net", "invalid_fee", "invalid_vat",
})


class LedgerValidationError(NativeProtocolError):
    """Only fixed codes/paths and a bounded row position may cross the diagnostic boundary."""

    def __init__(self, code, path, row_index=None):
        if code not in VALIDATION_CODES or path not in VALIDATION_PATHS:
            raise ValueError("invalid ledger validation detail")
        if row_index is not None and (type(row_index) is not int or not 0 <= row_index < 20):
            raise ValueError("invalid ledger validation row")
        self.code, self.path, self.row_index = code, path, row_index
        super().__init__("unsupported native transaction page")

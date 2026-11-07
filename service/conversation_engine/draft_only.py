"""In-process draft-only scope; this object has no send or network capability."""

from onlyfans_runtime.store import Refused


class DraftOnlyTransport:
    def __init__(self, store, tenant_id, account_id, *, allowed_chats):
        if (store is None or type(tenant_id) is not str or not tenant_id
                or type(account_id) is not str or not account_id):
            raise Refused("explicit draft-only scope required")
        from fans_api_adapter.real_transport import require_allowed_chats
        self.store, self.tenant_id, self.account_id = store, tenant_id, account_id
        self.allowed_chats = require_allowed_chats(allowed_chats)

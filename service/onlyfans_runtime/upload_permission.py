"""Private current permission checks using the existing Twin admin authority."""
from datetime import datetime, timezone
from urllib.parse import urlencode

from .creator_facts import TwinFactsReader, load
from .store import Refused


class UploadPermissionReader:
    def __init__(self, base_url, token):
        self._source = TwinFactsReader(base_url, token)

    def __call__(self, *, tenant_id, account_id, binding_id, creator_id, actor, permission):
        if permission != "content.upload":
            return False
        # Account/binding/asset/lease are revalidated by the content owner at
        # every boundary. The private dashboard owns fresh staff/owner access.
        from onlyfans_read.contract import ID_PATTERN
        from onlyfans_read.agency_scope import ACTOR_PATTERN
        import os
        import re
        if (os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker" or
                any(type(value) is not str or not ID_PATTERN.fullmatch(value)
                    for value in (tenant_id, account_id, binding_id, creator_id)) or
                type(actor) is not str or re.fullmatch(ACTOR_PATTERN, actor) is None):
            return False
        query = urlencode({"actor": actor, "creatorId": creator_id})
        url = self._source.base_url.rstrip("/") + "/api/admin/v1/onlyfans/upload-permission?" + query
        try:
            body = load(self._source._get_once(url))
            if (set(body) != {"version", "actor", "creatorId", "permitted", "observedAt"}
                    or body["version"] != "of-upload-permission.v1"
                    or body["actor"] != actor or body["creatorId"] != creator_id
                    or body["permitted"] is not True or type(body["observedAt"]) is not str):
                return False
            stamp = datetime.fromisoformat(body["observedAt"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                return False
            age = (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()
            return -5 <= age <= 15
        except Exception:
            return False

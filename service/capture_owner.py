"""One OnlyFans login per capture: user-id == the one auth_id cookie == account owner.

Stdlib only; shared by every path that turns a capture into a stored credential
(relay paste-curl, capture-from-curl, Browserbase login, save_connection)."""


class MissingAuthId(ValueError):
    """The capture carries no auth_id cookie (not signed in)."""


class OwnerMismatch(ValueError):
    """The capture is signed in as another OnlyFans login than the account's owner."""


def check_capture_owner(account: str, owner, cookies: list) -> None:
    """`account` is the of-<owner> id the capture is saved under; `owner` the capture's
    user-id; `cookies` name/value dicts. Messages carry no ids or cookie values."""
    values = [c.get("value") for c in cookies if type(c) is dict and c.get("name") == "auth_id"]
    if not values:
        raise MissingAuthId("no auth_id cookie: copy the request from a signed-in OnlyFans tab")
    if values != [str(owner)] or account != f"of-{owner}":
        raise OwnerMismatch("owner_mismatch: the signed-in OnlyFans login is not this account's owner")

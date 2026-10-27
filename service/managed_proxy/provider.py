"""Read-only IPRoyal adapter. No purchasing, renewal or credential mutation."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import urllib.request
from urllib.parse import quote

BASE = "https://apid.iproyal.com/v1/reseller"
PRODUCT_ID = 9
COUNTRIES = {
    "Australia":"AU", "Austria":"AT", "Belgium":"BE", "Brazil":"BR",
    "Bulgaria":"BG", "Canada":"CA", "Colombia":"CO", "Croatia":"HR",
    "Czech Republic":"CZ", "Denmark":"DK", "Estonia":"EE", "Finland":"FI",
    "France":"FR", "Germany":"DE", "Hong Kong":"HK", "Hungary":"HU",
    "Ireland":"IE", "Italy":"IT", "Japan":"JP", "Lithuania":"LT",
    "Luxembourg":"LU", "Netherlands":"NL", "New Zealand":"NZ", "Norway":"NO",
    "Poland":"PL", "Portugal":"PT", "Romania":"RO", "Serbia":"RS",
    "Singapore":"SG", "Slovenia":"SI", "South Korea":"KR", "Spain":"ES",
    "Sweden":"SE", "Switzerland":"CH", "Taiwan Province":"TW", "Taiwan":"TW",
    "Thailand":"TH", "Turkiye":"TR", "Turkey":"TR", "Ukraine":"UA",
    "United Kingdom":"GB", "United States":"US",
}
COUNTRY_NAMES = {code: name for name, code in COUNTRIES.items()}


class ProxyError(Exception):
    """Only a fixed, non-secret public code may leave this boundary."""


@dataclass(frozen=True)
class Candidate:
    address_hash: str
    country: str
    order_id: int
    expires_at: datetime
    url: str = field(repr=False)


def address_hash(host):
    try:
        address = ipaddress.ip_address(host)
        if address.version != 4 or not address.is_global:
            raise ValueError()
        return hashlib.sha256(str(address).encode()).hexdigest()
    except (ValueError, TypeError):
        raise ProxyError("invalid_provider_address") from None


def parse_order(order, *, now=None):
    now = now or datetime.now(timezone.utc)
    if not isinstance(order, dict) or order.get("product_name") != "ISP Dedicated":
        return []
    if order.get("status") != "confirmed" or order.get("is_test") is True:
        return []
    try:
        expires = datetime.fromisoformat(order["expire_date"].replace("Z", "+00:00"))
        expires = expires.replace(tzinfo=timezone.utc) if expires.tzinfo is None else expires.astimezone(timezone.utc)
        if expires <= now:
            return []
        # Some single-country orders return location=null and put the country
        # in locations instead. Accept only an exact known country; lists,
        # mixed-country strings and conflicting fields still need review.
        location = order.get("location")
        if location is None:
            location = order.get("locations")
        country = COUNTRIES[location]
        order_id = order["id"]
        port = order["proxy_data"]["ports"]["http|https"]
        proxies = order["proxy_data"]["proxies"]
        if type(order_id) is not int or order_id <= 0 or type(port) is not int or not 1 <= port <= 65535:
            raise ValueError()
        if not isinstance(proxies, list) or len(proxies) > 10000:
            raise ValueError()
        # Mixed-country orders need per-proxy location evidence; never guess.
        if order.get("locations") not in (None, location):
            raise ValueError()
        result = []
        for proxy in proxies:
            ip = proxy["ip"]
            digest = address_hash(ip)
            user, password = proxy["username"], proxy["password"]
            if any(not isinstance(v, str) or not v or len(v) > 4096 or any(ord(c) < 32 or ord(c) > 126 for c in v) for v in (user, password)):
                raise ValueError()
            url = "http://" + quote(user, safe="") + ":" + quote(password, safe="") + "@" + ip + ":" + str(port)
            result.append(Candidate(digest, country, order_id, expires, url))
        return result
    except (ValueError, TypeError, KeyError, AttributeError, ProxyError):
        raise ProxyError("provider_inventory_needs_review") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class IPRoyal:
    def __init__(self, token, *, fetch_json=None):
        self._token = token
        self._fetch_json = fetch_json

    def get(self, path):
        if self._fetch_json is not None:
            return self._fetch_json(path)
        request = urllib.request.Request(BASE + path, headers={"X-Access-Token": self._token, "Accept": "application/json"})
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as response:
                data = response.read(2_000_001)
                if response.status != 200 or len(data) > 2_000_000:
                    raise ValueError()
            return json.loads(data)
        except Exception:
            raise ProxyError("provider_unavailable") from None

    def inventory(self):
        products = self.get("/products")
        if not isinstance(products, dict) or not any(p.get("id") == PRODUCT_ID and p.get("name") == "ISP Dedicated" for p in products.get("data", []) if isinstance(p, dict)):
            raise ProxyError("isp_product_unavailable")
        items = []
        # The live API accepts per_page=10; per_page=100 returns 422.
        for page in range(1, 101):
            body = self.get(f"/orders?product_id={PRODUCT_ID}&page={page}&per_page=10")
            if not isinstance(body, dict) or not isinstance(body.get("data"), list) or type(body.get("meta", {}).get("last_page")) is not int:
                raise ProxyError("provider_inventory_needs_review")
            for order in body["data"]:
                items.extend(parse_order(order))
            if body["meta"]["last_page"] <= page:
                break
        else:
            raise ProxyError("inventory_page_limit")
        if len({item.address_hash for item in items}) != len(items):
            raise ProxyError("duplicate_provider_address")
        return items

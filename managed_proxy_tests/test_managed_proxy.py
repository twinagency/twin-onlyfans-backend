"""Synthetic provider fixtures only. The real credential is never used by tests."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/"service" if (root/"service").exists() else root))
from managed_proxy.provider import Candidate, IPRoyal, ProxyError, address_hash, parse_order
from managed_proxy.store import Store, owner_key


def order(**patch):
    value={"id":123,"product_name":"ISP Dedicated","status":"confirmed","location":"United States","locations":"United States",
        "expire_date":(datetime.now(timezone.utc)+timedelta(days=20)).isoformat(),
        "proxy_data":{"ports":{"http|https":12323},"proxies":[{"ip":"8.8.4.4","username":"fixture-user","password":"fixture:pass@word"}]}}
    return {**value,**patch}


class ProviderTest(unittest.TestCase):
    def test_confirmed_isp_credentials_are_encoded_and_not_represented(self):
        value=parse_order(order())[0]
        self.assertEqual(value.country,"US")
        self.assertEqual(value.url,"http://fixture-user:fixture%3Apass%40word@8.8.4.4:12323")
        self.assertNotIn("fixture",repr(value))

    def test_other_products_and_inactive_orders_are_not_stock(self):
        for patch in ({"product_name":"Datacenter Dedicated"},{"product_name":"ISP Shared"},{"status":"expired"},{"status":"in-progress"},{"is_test":True},{"expire_date":"2020-01-01 00:00:00"}):
            self.assertEqual(parse_order(order(**patch)),[])

    def test_missing_geography_expiry_or_port_never_falls_back(self):
        for patch in ({"location":"Unknown"},{"expire_date":"invalid"},{"locations":"United States, Canada"},{"proxy_data":{"ports":{"http|https":0},"proxies":[]}}):
            with self.assertRaises(ProxyError):parse_order(order(**patch))

    def test_private_hosts_and_malformed_credentials_are_refused_without_disclosure(self):
        for ip in ("127.0.0.1","10.1.2.3","169.254.169.254","proxy.example","::1"):
            data=order();data["proxy_data"]["proxies"][0]["ip"]=ip
            with self.assertRaisesRegex(ProxyError,"provider_inventory_needs_review"):parse_order(data)
        data=order();data["proxy_data"]["proxies"][0]["password"]="secret\nvalue"
        with self.assertRaises(ProxyError) as error:parse_order(data)
        self.assertNotIn("secret",str(error.exception))

    def test_pagination_reads_only_dedicated_product_and_never_writes(self):
        calls=[]
        def fetch(path):
            calls.append(path)
            if path=="/products":return {"data":[{"id":9,"name":"ISP Dedicated"}]}
            if "page=1&" in path:return {"data":[order()],"meta":{"last_page":2}}
            return {"data":[],"meta":{"last_page":2}}
        self.assertEqual(len(IPRoyal("synthetic",fetch_json=fetch).inventory()),1)
        self.assertEqual(calls,["/products","/orders?product_id=9&page=1&per_page=10","/orders?product_id=9&page=2&per_page=10"])

    def test_duplicate_ip_under_different_credentials_is_not_two_resources(self):
        def fetch(path):
            return {"data":[{"id":9,"name":"ISP Dedicated"}]} if path=="/products" else {"data":[order(),order(id=124)],"meta":{"last_page":1}}
        with self.assertRaisesRegex(ProxyError,"duplicate_provider_address"):IPRoyal("synthetic",fetch_json=fetch).inventory()

    def test_wrong_product_registration_is_refused(self):
        with self.assertRaisesRegex(ProxyError,"isp_product_unavailable"):
            IPRoyal("synthetic",fetch_json=lambda _: {"data":[{"id":9,"name":"ISP Shared"}]}).inventory()

    def test_assignment_identity_is_tenant_and_account_handle_specific(self):
        a=owner_key("tenant-a","creator-a","free_account")
        self.assertEqual(a,owner_key("tenant-a","creator-a","FREE_ACCOUNT"))
        self.assertNotEqual(a,owner_key("tenant-b","creator-a","free_account"))
        self.assertNotEqual(a,owner_key("tenant-a","creator-a","paid_account"))

    def test_encryption_is_bound_to_the_inventory_identity(self):
        store=object.__new__(Store);store.key=bytes.fromhex("42"*32)
        digest=address_hash("8.8.4.4");secret="http://fixture:password@8.8.4.4:12323"
        sealed=store.seal(secret,digest)
        self.assertNotIn("password",sealed)
        self.assertEqual(store.open({"address_hash":digest,"sealed_url":sealed}),secret)
        with self.assertRaisesRegex(ProxyError,"saved_proxy_unavailable"):
            store.open({"address_hash":address_hash("8.8.8.8"),"sealed_url":sealed})


if __name__=="__main__":unittest.main()

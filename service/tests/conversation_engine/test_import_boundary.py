import ast
from pathlib import Path
import unittest


# The only engine modules that may import httpx: the audited egress module and the real transport
# that drives it. Nothing else under conversation_engine/ or fans_api_adapter/ may.
HTTPX_ALLOWED = frozenset({"https_transport.py", "real_transport.py"})


class ImportBoundaryTests(unittest.TestCase):
    def test_new_primitives_do_not_import_legacy_writers_or_network_clients(self):
        service_root = Path(__file__).resolve().parents[2]
        forbidden = {"moxy_replacement", "native_pilot", "twin_bridge", "automations",
                     "socket", "requests", "httpx", "urllib.request", "subprocess"}
        for package in ("conversation_engine", "fans_api_adapter"):
            for path in (service_root / package).glob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        modules = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        modules = [node.module or ""]
                    else:
                        continue
                    for module in modules:
                        with self.subTest(path=path.name, module=module):
                            if path.name in HTTPX_ALLOWED and (module == "httpx" or module.startswith("httpx.")):
                                continue  # the audited egress modules; asserted below
                            if path.name == "local_http.py" and module == "socket":
                                continue  # fixture uses SHUT_RDWR to simulate a lost response
                            self.assertFalse(any(module == name or module.startswith(name + ".") for name in forbidden))
                            if path.name in {"identity.py", "evidence.py", "recovery.py", "sales.py"}:
                                self.assertFalse(module.startswith("onlyfans_runtime"))

    def test_httpx_is_isolated_to_the_two_egress_modules(self):
        service_root = Path(__file__).resolve().parents[2]
        importers = set()
        for package in ("conversation_engine", "fans_api_adapter"):
            for path in (service_root / package).glob("*.py"):
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                             else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                    if any(n == "httpx" or n.startswith("httpx.") for n in names):
                        importers.add(path.name)
        self.assertTrue(importers <= HTTPX_ALLOWED, importers - HTTPX_ALLOWED)
        self.assertIn("https_transport.py", importers)

    def test_http_fixture_uses_only_fixed_loopback_endpoints(self):
        service_root = Path(__file__).resolve().parents[2]
        path = service_root / "fans_api_adapter" / "local_http.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        socket_attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name) and node.value.id == "socket"}
        self.assertEqual(socket_attributes, {"SHUT_RDWR"})
        client = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name) and node.func.id == "HTTPConnection"]
        server = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name) and node.func.id == "ThreadingHTTPServer"]
        self.assertEqual(len(client), 1)
        self.assertEqual(len(server), 1)
        self.assertEqual(ast.literal_eval(client[0].args[0]), "127.0.0.1")
        self.assertEqual(ast.literal_eval(server[0].args[0]), ("127.0.0.1", 0))


if __name__ == "__main__":
    unittest.main()

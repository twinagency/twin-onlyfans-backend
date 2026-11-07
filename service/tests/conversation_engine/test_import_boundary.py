import ast
from pathlib import Path
import unittest


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
                            if path.name == "https_transport.py" and module == "httpx":
                                continue  # the single audited egress module; asserted below
                            if path.name == "local_http.py" and module == "socket":
                                continue  # fixture uses SHUT_RDWR to simulate a lost response
                            self.assertFalse(any(module == name or module.startswith(name + ".") for name in forbidden))
                            if path.name in {"identity.py", "evidence.py", "recovery.py", "sales.py"}:
                                self.assertFalse(module.startswith("onlyfans_runtime"))

    def test_httpx_is_isolated_to_the_one_https_transport_module(self):
        service_root = Path(__file__).resolve().parents[2]
        importers = set()
        for package in ("conversation_engine", "fans_api_adapter"):
            for path in (service_root / package).glob("*.py"):
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                             else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                    if any(n == "httpx" or n.startswith("httpx.") for n in names):
                        importers.add(path.name)
        self.assertEqual(importers, {"https_transport.py"})

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

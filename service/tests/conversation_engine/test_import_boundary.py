import ast
from pathlib import Path
import unittest


class ImportBoundaryTests(unittest.TestCase):
    def test_new_primitives_do_not_import_legacy_writers_or_network_clients(self):
        service_root = Path(__file__).resolve().parents[2]
        forbidden = {"moxy_replacement", "native_pilot", "twin_bridge", "automations",
                     "onlyfans_runtime", "socket", "requests", "httpx", "urllib.request", "subprocess"}
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
                            self.assertFalse(any(module == name or module.startswith(name + ".") for name in forbidden))


if __name__ == "__main__":
    unittest.main()

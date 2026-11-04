"""Diagnostics contain fixed labels, never exception or provider payload text."""
import contextlib
import io
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from native_pilot.tools import describe_imported_vault as child
from onlyfans_runtime import vault_description_worker as parent


class VaultErrorObservabilityTests(unittest.TestCase):
    def test_known_type_and_source_stage(self):
        namespace = {}
        exec(compile("def fresh_media():\n    raise TypeError('private signed-url token')\n",
                     child.__file__, "exec"), namespace)
        try:
            namespace["fresh_media"]()
        except TypeError as exc:
            fields = child.safe_error_fields(exc)
        self.assertEqual(fields, {"reason": "error", "errorType": "TypeError", "errorStage": "metadata_refresh"})

    def test_unknown_type_and_external_path_never_leak(self):
        unknown = type("private-token-in-type-name", (Exception,), {})
        namespace = {"Unknown": unknown}
        exec(compile("def private_function_name():\n    raise Unknown('private provider response')\n",
                     "/private-signed-url-token/secret.py", "exec"), namespace)
        try:
            namespace["private_function_name"]()
        except Exception as exc:
            fields = child.safe_error_fields(exc)
        self.assertEqual(fields, {"reason": "error", "errorType": "OtherError", "errorStage": "unknown"})
        self.assertNotIn("private", json.dumps(fields))

    def test_known_categories_do_not_read_exception_text(self):
        for name, reason in [("LLMConfigError", "auth"), ("LLMCapExceeded", "budget"),
                             ("ReadDeferred", "provider_read_deferred")]:
            def forbidden_string(self):
                raise AssertionError("Exception text must not be read")
            exception_type = type(name, (Exception,), {"__str__": forbidden_string})
            self.assertEqual(child.safe_error_fields(exception_type())['reason'], reason)

    def test_main_emits_only_safe_diagnostic_labels(self):
        class UnprintableError(Exception):
            def __str__(self):
                raise AssertionError("Exception text must not be read")
        async def fail(args):
            raise UnprintableError("private auth header and provider URL")
        output = io.StringIO()
        with patch.object(child, "run", fail), contextlib.redirect_stdout(output):
            result = child.main(["--tenant", "synthetic", "--account", "of-900001",
                                 "--binding", "synthetic", "--revision", "1"])
        self.assertEqual(result, 1)
        event = json.loads(output.getvalue())
        self.assertEqual(event['errorType'], "OtherError")
        self.assertEqual(event['errorStage'], "entrypoint")
        self.assertEqual(event['reason'], "error")
        self.assertNotIn("private", output.getvalue())

    def test_parent_preserves_failed_child_diagnostics(self):
        stop = threading.Event()
        observed = []
        event = {"schemaVersion": "of-vault-description.v1", "account": "of-900001",
                 "state": "retry_pending", "reason": "error", "errorType": "TypeError",
                 "errorStage": "metadata_refresh"}
        process = SimpleNamespace(returncode=1, communicate=lambda **kwargs: (json.dumps(event), None))
        worker = parent.VaultDescriptionWorker(None, "synthetic", stop, observe=observed.append)
        with patch.object(parent, "accounts", return_value=([("of-900001", "binding", 1)], None)), \
             patch.object(parent.subprocess, "Popen", return_value=process), \
             patch.object(stop, "wait", side_effect=lambda seconds: stop.set()):
            worker.run()
        self.assertEqual(observed, [event])


if __name__ == "__main__":
    unittest.main()

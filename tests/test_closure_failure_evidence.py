"""A failed real child keeps its first error and complete output across retries."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from shiftboss import process_output, regen


class ClosureFailureEvidence(unittest.TestCase):
    def test_original_error_and_outputs_survive_later_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            control = root / "control"
            stderr = "FIRST_SOURCE_ERROR\n" + "x" * 2500 + "\nDOWNSTREAM_JSON_ERROR\n"
            script = root / "failure.py"
            script.write_text("import sys\nprint('partial-json')\nsys.stderr.write(" + repr(stderr)
                              + ")\nsys.exit(7)\n")
            command = [sys.executable, str(script)]
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "remote", "add", "origin", str(root / "hub.git")], cwd=root, check=True)
            with patch("shiftboss.config.closure_command", return_value=command), \
                    patch("shiftboss.config.control_dir", return_value=control), \
                    patch("shiftboss.verify.origin", return_value=root / "hub.git"), \
                    patch.object(process_output, "adapter", return_value=None):
                messages = []
                for _ in range(2):
                    with self.assertRaises(ValueError) as failure:
                        regen.with_closure([], root)
                    messages.append(str(failure.exception))
                records = sorted((control / "verification/closure-failures").glob("*/result.json"))
                self.assertEqual(len(records), 2)
                for path in records:
                    result = json.loads(path.read_text())
                    self.assertEqual(result["returncode"], 7)
                    self.assertEqual(result["command"], command)
                    self.assertEqual(Path(result["outputs"]["stderr"]["path"]).read_text(), stderr)
                    self.assertEqual(Path(result["outputs"]["stdout"]["path"]).read_text(), "partial-json\n")
                for message in messages:
                    self.assertIn("FIRST_SOURCE_ERROR", message)
                    self.assertIn("DOWNSTREAM_JSON_ERROR", message)
                    self.assertIn("result.json", message)

    def test_success_uses_existing_closure_without_failure_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = [sys.executable, "-c", "print('{\"web\":[\"source/\"]}')"]
            with patch("shiftboss.config.closure_command", return_value=command), \
                    patch("shiftboss.config.control_dir") as control:
                self.assertEqual(regen.with_closure([{"name": "web", "inputs": ["data/"]}], root),
                                 [{"name": "web", "inputs": ["data/", "source/"]}])
                control.assert_not_called()

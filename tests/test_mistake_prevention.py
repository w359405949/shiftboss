"""Regression checks for recurring operator mistakes, using disposable inputs only."""
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import Mock, patch

from shiftboss import automerge, bootstrap, inspect_json, merge, taskbook


class BoundedInspection(unittest.TestCase):
    def test_large_nested_reasons_and_long_keys_cannot_expand_output(self):
        for value in ([{"reasons": ["诊断" * 100000]}] * 200,
                      {"键" * 100000: "正文" * 100000},
                      {str(i): "值" * 2000 for i in range(50)}):
            for depth in range(4):
                text = inspect_json.render(value, source="原件.json", depth=depth, limit=1024)
                self.assertLessEqual(len((text + "\n").encode()), 1024)
                self.assertTrue(json.loads(text)["inspection_only"])

    def test_shape_distinguishes_array_from_object(self):
        array = inspect_json.preview([{"x": 1}, {"y": 2}], 0)
        self.assertEqual((array["type"], array["length"]), ("array", 2))
        self.assertNotIn("keys", array)
        self.assertEqual(inspect_json.preview({"x": 1}, 0)["keys"], ["x"])

    def test_pointer_is_explicit_and_missing_values_fail(self):
        value = {"a/b": {"~x": [True]}}
        self.assertIs(inspect_json.pointer(value, "/a~1b/~0x/0"), True)
        with self.assertRaises(KeyError):
            inspect_json.pointer(value, "/absent")
        with self.assertRaises(ValueError):
            inspect_json.pointer(value, "a/b")

    def test_stage_uses_real_command_not_ordinal(self):
        report = {"stages": {
            "gate-12": {"command": ["node", "check.mjs", "--case", "save-location"], "returncode": 1},
            "gate-11": {"command": ["node", "check.mjs", "--case", "resource-contracts"], "returncode": 0}}}
        selected = inspect_json.stage(report, "resource-contracts", "check.mjs")
        self.assertEqual((selected["stage"], selected["returncode"]), ("gate-11", 0))
        report["stages"]["duplicate"] = report["stages"]["gate-11"]
        with self.assertRaisesRegex(ValueError, "2 项"):
            inspect_json.stage(report, "resource-contracts", None)

    def test_cli_delegates_reference_validation_and_does_not_hide_failure(self):
        output, errors = io.StringIO(), io.StringIO()
        with (patch.object(inspect_json, "project_root", return_value=Path("/repo")),
              patch.object(inspect_json, "read_log", side_effect=ValueError("bad original digest")) as read,
              redirect_stdout(output), redirect_stderr(errors)):
            self.assertEqual(inspect_json.main(["report.json"]), 2)
        read.assert_called_once_with(Path("report.json"), Path("/repo"))
        self.assertEqual(output.getvalue(), "")
        self.assertIn("bad original digest", errors.getvalue())

    def test_cli_read_is_bounded_and_original_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "large.json"
            body = json.dumps({"result": {"status": "incomplete"}, "events": ["大" * 1000] * 100})
            path.write_text(body)
            output = io.StringIO()
            with (patch.object(inspect_json, "project_root", return_value=Path(temp)),
                  redirect_stdout(output)):
                self.assertEqual(inspect_json.main([str(path), "--pointer", "/result/status"]), 0)
            self.assertEqual(json.loads(output.getvalue())["preview"], "incomplete")
            self.assertEqual(path.read_text(), body)

    def test_field_assertion_is_fail_closed_and_does_not_coerce_boolean_to_zero(self):
        with patch.object(inspect_json, "read_log", return_value='{"returncode": false}'), redirect_stdout(io.StringIO()):
            self.assertEqual(inspect_json.main(["report.json", "--pointer", "/returncode", "--expect-json", "0"]), 1)
        with patch.object(inspect_json, "read_log", return_value='{"returncode": 0}'), redirect_stdout(io.StringIO()):
            self.assertEqual(inspect_json.main(["report.json", "--pointer", "/returncode", "--expect-json", "0"]), 0)
        with patch.object(inspect_json, "read_log", return_value='{"returncode": [false]}'), redirect_stdout(io.StringIO()):
            self.assertEqual(inspect_json.main(["report.json", "--pointer", "/returncode", "--expect-json", "[0]"]), 1)
        with patch.object(inspect_json, "read_log", return_value='{}'), redirect_stderr(io.StringIO()):
            self.assertEqual(inspect_json.main(["report.json", "--pointer", "/returncode", "--expect-json", "0"]), 2)


class DocumentationOnlyBootstrap(unittest.TestCase):
    def test_docs_only_cannot_create_hub_copies_or_hooks(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "project"
            repo.mkdir()
            with (patch.object(bootstrap, "REPO", repo),
                  patch("shiftboss.docs_install.install", return_value=True) as install,
                  patch.object(bootstrap, "ensure_bare") as bare,
                  patch.object(bootstrap, "configure_copies") as copies,
                  patch.object(bootstrap, "ensure_push_guard") as hooks):
                self.assertEqual(bootstrap.main(["--docs-only"]), 0)
                install.assert_called_once_with(repo, False)
                bare.assert_not_called()
                copies.assert_not_called()
                hooks.assert_not_called()
            self.assertEqual(list(repo.iterdir()), [])

    def test_check_mode_forwards_read_only_flag(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            with (patch.object(bootstrap, "REPO", repo),
                  patch("shiftboss.docs_install.install", return_value=False) as install):
                self.assertEqual(bootstrap.main(["--docs-only", "--check"]), 1)
                install.assert_called_once_with(repo, True)

    def test_conflicting_copy_flags_fail_before_any_install(self):
        with patch("shiftboss.docs_install.install") as install, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                bootstrap.main(["--docs-only", "--copies", "2"])
            self.assertEqual(raised.exception.code, 2)
            install.assert_not_called()

    def test_actual_doc_install_preserves_taskbooks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, source = root / "project", root / "package-docs"
            source.mkdir()
            (source / "README.md").write_text("rules\n")
            plan = repo / "shiftboss" / "plans" / "active" / "contract.md"
            plan.parent.mkdir(parents=True)
            plan.write_text("active work\n")
            with (patch.object(bootstrap, "REPO", repo),
                  patch("shiftboss.docs_install.SOURCE", source), redirect_stdout(io.StringIO())):
                self.assertEqual(bootstrap.main(["--docs-only"]), 0)
            self.assertEqual((repo / "shiftboss" / "README.md").read_text(), "rules\n")
            self.assertEqual(plan.read_text(), "active work\n")
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["package-docs", "project"])


class StructuredTaskbookLists(unittest.TestCase):
    def test_json_and_existing_separator_formats(self):
        expected = ["engine/a/", "engine/b/"]
        for value in ('["engine/a/", "engine/b/"]', "engine/a/、engine/b/", expected):
            self.assertEqual(taskbook.dependency_list(value), expected)
        self.assertEqual(taskbook.dependency_list('["path with spaces/"]'), ["path with spaces/"])

    def test_invalid_json_lists_are_not_saved(self):
        book = Mock()
        with patch.object(taskbook.Taskbook, "find", return_value=book), redirect_stdout(io.StringIO()):
            for value in ("[broken", "[null]", '["valid", 3]', '[""]'):
                self.assertEqual(taskbook.main(["set", "one", "--field", "写入范围=" + value]), 2)
        book.set_meta.assert_not_called()

    def test_valid_scope_does_not_store_json_punctuation(self):
        book = Mock()
        with (patch.object(taskbook.Taskbook, "find", return_value=book),
              patch("shiftboss.config.load", return_value={}), redirect_stdout(io.StringIO())):
            self.assertEqual(taskbook.main(["set", "one", "--field", '写入范围=["engine/a/"]']), 0)
        book.set_meta.assert_called_once_with(写入范围=["engine/a/"])


class DeclaredCheckReview(unittest.TestCase):
    def test_automerge_excludes_declared_helper_checks(self):
        book = SimpleNamespace(meta=lambda: {"子类型": "repair", "类别": "修复", "破坏性": "否", "改检查": "是"})
        with patch.object(taskbook.Taskbook, "iter_active", return_value=[book]), patch.object(automerge, "git") as git:
            self.assertEqual(list(automerge.candidates(Path("/repo"), ["repair"])), [])
        git.assert_not_called()

    def test_merge_requires_review_even_when_path_classifier_does_not_know_helper(self):
        book = SimpleNamespace(name="helper-check", meta=lambda: {"改检查": "是", "分支": "codex/helper-check"})
        def git(*args, **kwargs):
            text = "base" if args[0] == "merge-base" else "engine/tools/check/helpers.mjs\n"
            return subprocess.CompletedProcess(args, 0, text, "")
        with (patch.object(taskbook.Taskbook, "iter_active", return_value=[book]),
              patch.object(merge, "git", side_effect=git), patch.object(merge, "check_hits", return_value=[])):
            self.assertIn("--checks-reviewed", merge.check_change_reason("a" * 40, False))
            self.assertIsNone(merge.check_change_reason("a" * 40, True))


if __name__ == "__main__":
    unittest.main()

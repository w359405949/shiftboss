"""生成输入分节与全新检出的点名阶段，不执行产品生成。"""
import argparse
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from shiftboss import config, regen


class GenerationScope(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Scope test")
        self.git("config", "user.email", "scope@localhost")
        (self.root / "shiftboss.toml").write_text('[regen]\nplan = "plan.json"\n')
        self.plan = {"stages": [
            {"name": "unrelated", "command": ["python", "-B", "-m", "unused"], "inputs": ["unused/"]},
            {"name": "web", "command": ["python", "-B", "-m", "web"], "inputs": ["web/"]},
            {"name": "manifest", "command": ["python", "-B", "-m", "manifest"], "inputs": ["manifest/"], "after": ["web"]}],
            "gates": [["python", "check.py"]], "gate_layers": {"build": {"gates": [0]}}}
        self.write_plan(self.plan)
        self.commit()
        self.base = self.git("rev-parse", "HEAD")
        self.addCleanup(config.load.cache_clear)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True).stdout.strip()

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def write_plan(self, plan):
        (self.root / "plan.json").write_text(json.dumps(plan))

    def filtered(self, plan, paths=None):
        self.write_plan(plan)
        self.commit()
        with patch.object(regen, "REPO", self.root):
            return regen.generation_paths(paths or ["plan.json"], self.base, "HEAD")

    def test_check_registration_does_not_require_product_generation(self):
        plan = copy.deepcopy(self.plan)
        plan["gates"].append(["python", "another-check.py"])
        plan["gate_layers"]["build"]["gates"].append(1)
        self.assertEqual(self.filtered(plan, ["plan.json", "engine/producer.py"]), ["engine/producer.py"])

    def test_generator_command_change_is_still_an_input(self):
        plan = copy.deepcopy(self.plan)
        plan["stages"][1]["command"][-1] = "new_producer"
        self.assertEqual(self.filtered(plan), ["plan.json"])

    def test_unknown_plan_field_remains_an_input(self):
        plan = copy.deepcopy(self.plan)
        plan["producer_options"] = {"new": True}
        self.assertEqual(self.filtered(plan), ["plan.json"])

    def test_stage_order_and_dependency_changes_remain_inputs(self):
        plan = copy.deepcopy(self.plan)
        plan["stages"][2]["after"] = ["unrelated"]
        self.assertEqual(self.filtered(plan), ["plan.json"])

    def test_missing_plan_is_not_treated_as_registration_only(self):
        (self.root / "plan.json").unlink()
        self.commit()
        with patch.object(regen, "REPO", self.root):
            self.assertEqual(regen.generation_paths(["plan.json"], self.base, "HEAD"), ["plan.json"])

    def test_explicit_stages_do_not_expand_for_missing_catalog(self):
        args = argparse.Namespace(stage=["web"], full=False, base="irrelevant")
        with patch.object(regen, "with_closure") as closure, patch.object(regen, "previous_catalog", return_value=None):
            result = regen.stage_selection(self.plan["stages"], args)
        self.assertEqual([row["name"] for row in result], ["web", "manifest"])
        closure.assert_not_called()
        with self.assertRaisesRegex(ValueError, "未知阶段"):
            regen.stage_selection(self.plan["stages"], argparse.Namespace(stage=["missing"]))

    def test_branch_check_passes_exact_affected_stages_to_fresh_child(self):
        (self.root / "out").mkdir()
        head, base = "a" * 40, "b" * 40
        child_calls = []
        def child(command, **kwargs):
            if command[0] == "git":
                value = head if command[1] == "rev-parse" else base if command[1] == "merge-base" else "web/changed.py" if command[1] == "diff" else ""
                return subprocess.CompletedProcess(command, 0, value, "")
            child_calls.append(command)
            return subprocess.CompletedProcess(command, 0, "fixture child; no products", "")
        with (patch.object(regen, "REPO", self.root), patch.object(regen.subprocess, "run", side_effect=child),
              patch.object(regen, "load_plan", return_value=self.plan),
              patch.object(regen, "closure_stages", side_effect=lambda stages: stages),
              patch.object(regen, "checks_dir", return_value=self.root / "checks")):
            self.assertEqual(regen.branch_check("HEAD"), 0)
        self.assertEqual(len(child_calls), 1)
        self.assertEqual(child_calls[0][5:], ["--stage", "web", "--stage", "manifest"])
        record = json.loads((self.root / "checks" / (head + ".json")).read_text())
        self.assertEqual(record["stages"], ["web", "manifest"])


if __name__ == "__main__":
    unittest.main()

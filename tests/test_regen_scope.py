"""生成输入分节与全新检出的点名阶段，不执行产品生成。"""
import argparse
import copy
import json
from pathlib import Path
import subprocess
import sys
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

    def select_current(self, *, full=False, missing_catalog=False):
        stages = copy.deepcopy(self.plan["stages"])
        stages[0]["inputs"].extend(["shiftboss.toml", "plan.json"])
        catalog = None if missing_catalog else {"producers": [{"stage": s["name"]} for s in stages]}
        with (patch.object(regen, "REPO", self.root),
              patch.object(regen, "with_closure", side_effect=lambda value: value),
              patch.object(regen, "previous_catalog", return_value=catalog)):
            return [s["name"] for s in regen.stage_selection(
                stages, argparse.Namespace(stage=[], full=full, base=self.base))]

    def test_regular_generation_filters_committed_verify_only_change(self):
        with (self.root / "shiftboss.toml").open("a") as stream:
            stream.write('[verify]\noutput_adapter = "adapter.py"\n')
        self.commit()
        self.assertEqual(self.select_current(), [])
        self.assertEqual(self.select_current(full=True), ["unrelated", "web", "manifest"])
        self.assertEqual(self.select_current(missing_catalog=True), ["unrelated", "web", "manifest"])

    def test_regular_generation_reads_dirty_and_staged_regen_changes(self):
        path = self.root / "shiftboss.toml"
        path.write_text('[regen]\nplan = "plan.json"\n[verify]\nflag = true\n')
        self.commit()
        path.write_text('[regen]\nplan = "plan.json"\nnew_input = "source/"\n[verify]\nflag = true\n')
        self.assertEqual(self.select_current(), ["unrelated"])
        self.git("add", "shiftboss.toml")
        self.assertEqual(self.select_current(), ["unrelated"])

    def test_regular_generation_filters_dirty_gate_registration_but_keeps_sources(self):
        plan = copy.deepcopy(self.plan)
        plan["gates"].append(["python", "another.py"])
        self.write_plan(plan)
        (self.root / "web").mkdir()
        (self.root / "web/new.py").write_text("new = True\n")
        self.assertEqual(self.select_current(), ["web", "manifest"])
        plan["producer_options"] = {"new": True}
        self.write_plan(plan)
        self.assertEqual(self.select_current(), ["unrelated", "web", "manifest"])

    def test_regular_generation_keeps_removed_config_and_merge_base_semantics(self):
        (self.root / "shiftboss.toml").unlink()
        self.assertEqual(self.select_current(), ["unrelated"])
        self.git("update-ref", "refs/remotes/origin/main", self.base)
        with patch.object(regen, "REPO", self.root):
            self.assertEqual(regen.change_base(), self.base)
            (self.root / ".git/MERGE_HEAD").write_text(self.base + "\n")
            self.assertEqual(regen.change_base(), "HEAD")

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


class ClosureCache(unittest.TestCase):
    """真实 Git 和小型目录发现命令；不运行产品闭包或生成。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.cache = Path(self.temp.name) / "checks"
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Cache test")
        self.git("config", "user.email", "cache@localhost")
        (self.root / "sources").mkdir()
        (self.root / "sources/first.py").write_text("first = True\n")
        (self.root / "notes").mkdir()
        (self.root / "notes/task.md").write_text("first task\n")
        (self.root / "discover.py").write_text(
            "import json\nfrom pathlib import Path\n"
            "print(json.dumps({'web': sorted(str(p) for p in Path('sources').glob('*.py'))}))\n")
        self.command = [sys.executable, "discover.py"]
        self.stages = [{"name": "web", "inputs": ["data/"], "command": ["node", "web.mjs"]}]
        self.commit()
        for target, value in [("REPO", self.root), ("checks_dir", self.cache)]:
            patched = patch.object(regen, target, value) if target == "REPO" else patch.object(regen, target, return_value=value)
            patched.start()
            self.addCleanup(patched.stop)
        self.skip_patch = patch("shiftboss.config.generation_check_skip", return_value=["notes/"])
        self.skip_patch.start()
        self.addCleanup(self.skip_patch.stop)
        command_patch = patch("shiftboss.config.closure_command", side_effect=lambda root=None: self.command)
        command_patch.start()
        self.addCleanup(command_patch.stop)
        real_closure = regen.with_closure
        execution_patch = patch.object(regen, "with_closure", wraps=real_closure)
        self.executions = execution_patch.start()
        self.addCleanup(execution_patch.stop)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True).stdout.strip()

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def test_metadata_commit_and_dirty_notes_reuse_actual_closure(self):
        first = regen.closure_stages(self.stages)
        self.assertEqual(first[0]["inputs"], ["data/", "sources/first.py"])
        (self.root / "notes/task.md").write_text("next task\n")
        self.commit()
        self.assertEqual(regen.closure_stages(self.stages), first)
        (self.root / "notes/task.md").write_text("uncommitted task\n")
        (self.root / "notes/new.md").write_text("another task\n")
        self.assertEqual(regen.closure_stages(self.stages), first)
        self.assertEqual(self.executions.call_count, 1)

    def test_new_source_is_discovered_instead_of_trusting_old_file_list(self):
        regen.closure_stages(self.stages)
        (self.root / "sources/next.py").write_text("next = True\n")
        self.commit()
        actual = regen.closure_stages(self.stages)
        self.assertIn("sources/next.py", actual[0]["inputs"])
        self.assertEqual(self.executions.call_count, 2)

    def test_source_and_analyzer_changes_invalidate(self):
        regen.closure_stages(self.stages)
        for file in ["sources/first.py", "discover.py"]:
            path = self.root / file
            path.write_text(path.read_text() + "\n# source change\n")
            self.commit()
            regen.closure_stages(self.stages)
        self.assertEqual(self.executions.call_count, 3)

    def test_dirty_staged_and_untracked_inputs_never_use_or_write_cache(self):
        regen.closure_stages(self.stages)
        stored = sorted(self.cache.iterdir())
        (self.root / "sources/next.py").write_text("next = True\n")
        for staged in [False, True]:
            if staged:
                self.git("add", "sources/next.py")
            actual = regen.closure_stages(self.stages)
            self.assertIn("sources/next.py", actual[0]["inputs"])
            self.assertEqual(sorted(self.cache.iterdir()), stored)
        (self.root / "sources/first.py").write_text("dirty = True\n")
        regen.closure_stages(self.stages)
        self.assertEqual(sorted(self.cache.iterdir()), stored)
        self.assertEqual(self.executions.call_count, 4)

    def test_command_stages_and_exclusions_are_part_of_identity(self):
        regen.closure_stages(self.stages)
        self.command = [self.command[0], "-B", "discover.py"]
        regen.closure_stages(self.stages)
        self.stages[0]["inputs"].append("another/")
        self.assertIn("another/", regen.closure_stages(self.stages)[0]["inputs"])
        with patch("shiftboss.config.generation_check_skip", return_value=[]):
            regen.closure_stages(self.stages)
        self.assertEqual(self.executions.call_count, 4)

    def test_change_during_analysis_does_not_store_result(self):
        real_closure = self.executions._mock_wraps
        def mutate(stages):
            result = real_closure(stages)
            (self.root / "sources/late.py").write_text("late = True\n")
            return result
        with patch.object(regen, "with_closure", side_effect=mutate):
            regen.closure_stages(self.stages)
        self.assertFalse(self.cache.exists())

    def test_old_tree_only_cache_is_not_used_and_failure_not_stored(self):
        self.cache.mkdir()
        tree = self.git("rev-parse", "HEAD^{tree}")
        (self.cache / f"closure-{tree}.json").write_text('[{"stale": true}]')
        self.assertEqual(regen.closure_stages(self.stages)[0]["name"], "web")
        self.command = [self.command[0], "-c", "raise RuntimeError('expected fixture failure')"]
        before = sorted(self.cache.iterdir())
        with self.assertRaisesRegex(ValueError, "闭包命令失败"):
            regen.closure_stages(self.stages)
        self.assertEqual(sorted(self.cache.iterdir()), before)


if __name__ == "__main__":
    unittest.main()

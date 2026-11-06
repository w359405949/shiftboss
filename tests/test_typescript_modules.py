"""真实 TS 编译器上的有限工具验收，不执行产品或生成。"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from shiftboss import merge, typescript_modules


class TypeScriptModules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = os.environ.get("SHIFTBOSS_TEST_TYPESCRIPT")
        if not compiler:
            binary = shutil.which("tsc")
            compiler = str(Path(binary).resolve().parent / "typescript.js") if binary else ""
        if not compiler or not Path(compiler).is_file() or not shutil.which("node"):
            raise unittest.SkipTest("需要本机 TypeScript 编译器和 node")
        cls.compiler = str(Path(compiler).resolve())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        (self.repo / "src").mkdir()
        (self.repo / "shiftboss.toml").write_text(
            '[merge]\ntypescript_parser = ' + repr(self.compiler) + '\njs_module_prefixes = ["src/"]\n')
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.addCleanup(patch.stopall)
        patch.object(merge, "REPO", self.repo).start()
        patch.object(merge.config, "js_module_prefixes", return_value=["src/"]).start()
        patch.object(merge.config, "js_parser", return_value=None).start()

    def write(self, name, source):
        (self.repo / name).write_text(source)
        subprocess.run(["git", "-C", str(self.repo), "add", name], check=True)

    def test_existing_js_import_resolves_renamed_ts_and_still_checks_export(self):
        self.write("src/owner.ts", "export function value(x: number): number {return x;}\n")
        self.write("src/caller.js", "import {value} from './owner.js';\n")
        merge.check_js_imports(["src/owner.js", "src/owner.ts"])
        self.write("src/owner.ts", "export const different: number = 1;\n")
        with self.assertRaisesRegex(merge.MergeError, "value"):
            merge.check_js_imports(["src/owner.ts"])

    def test_type_only_exports_are_not_runtime_values(self):
        self.write("src/owner.ts", "export interface Shape {size: number}; export const size = 1;\n")
        self.write("src/caller.ts", "import {type Shape, size} from './owner.js'; const a: Shape = {size};\n")
        merge.check_js_imports(["src/caller.ts"])
        self.write("src/caller.ts", "import {Shape} from './owner.js'; console.log(Shape);\n")
        with self.assertRaisesRegex(merge.MergeError, "运行.*缺失"):
            merge.check_js_imports(["src/caller.ts"])

    def test_import_owners_are_batched_and_next_check_reads_changed_exports(self):
        for name in ('one', 'two', 'three'):
            self.write(f'src/{name}.ts', f'export const {name}: number = 1;\n')
        self.write('src/caller.ts', "import {one} from './one.js';\n")
        self.write('src/one.ts', "import {two} from './two.js';\n"
                   "import {three} from './three.js';\nexport const one: number = 1;\n")
        with patch.object(merge, 'typescript_details', wraps=merge.typescript_details) as parse:
            merge.check_js_imports(['src/caller.ts', 'src/one.ts'])
        self.assertEqual(parse.call_count, 2)
        self.assertEqual(set(parse.call_args_list[1].args[0]),
                         {'src/two.ts', 'src/three.ts'})
        self.write('src/two.ts', 'export const changed: number = 1;\n')
        with self.assertRaisesRegex(merge.MergeError, 'two.*缺失'):
            merge.check_js_imports(['src/caller.ts', 'src/one.ts'])

    def test_missing_type_export_and_missing_module_are_rejected(self):
        self.write("src/owner.ts", "export interface Shape {size: number}\n")
        self.write("src/caller.ts", "import type {Absent} from './owner.js';\n")
        with self.assertRaisesRegex(merge.MergeError, "Absent"):
            merge.check_js_imports(["src/caller.ts"])
        self.write("src/caller.ts", "import {value} from './absent.js';\n")
        with self.assertRaisesRegex(merge.MergeError, "模块不存在"):
            merge.check_js_imports(["src/caller.ts"])

    def test_local_type_reexport_is_not_a_value_and_default_name_is_private(self):
        self.write("src/owner.ts", "interface Shape {size: number}; export {Shape}; export default function named(): void {}\n")
        self.write("src/caller.ts", "import type {Shape} from './owner.js';\n")
        merge.check_js_imports(["src/caller.ts"])
        for name in ["Shape", "named"]:
            self.write("src/value-caller.js", f"import {{{name}}} from './owner.js';\n")
            with self.assertRaisesRegex(merge.MergeError, name):
                merge.check_js_imports(["src/value-caller.js"])

    def test_mts_runtime_specifier_and_pure_declarations(self):
        self.write("src/owner.mts", "export const value: number = 1;\n")
        self.write("src/caller.js", "import {value} from './owner.mjs';\n")
        merge.check_js_imports(["src/owner.mts"])
        self.write("src/contracts.d.ts", "export interface Shape {size: number}\n")
        merge.check_js_modules(["src/contracts.d.ts"])

    def test_typed_syntax_and_illegal_nested_export_are_rejected(self):
        self.write("src/valid.ts", "export const value: number = 1;\n")
        merge.check_js_modules(["src/valid.ts"])
        self.write("src/bad.ts", "export const value: = 1;\n")
        with self.assertRaisesRegex(merge.MergeError, "语法"):
            merge.check_js_modules(["src/bad.ts"])

    def test_batch_checks_emitted_syntax_without_linking_or_executing(self):
        marker = self.repo / 'executed'
        self.write('src/first.ts', "import {writeFileSync} from 'node:fs';\n"
                   + f"writeFileSync({str(marker)!r}, 'wrong');\nexport const value: number = 1;\n")
        self.write('src/second.ts', "import './not-installed.js'; export const other: number = 2;\n")
        original = subprocess.run
        with patch.object(typescript_modules.subprocess, 'run', wraps=original) as launch:
            result = typescript_modules.module_details(self.repo, ['src/first.ts', 'src/second.ts'])
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(result['src/first.ts']['errors'], [])
        self.assertEqual(result['src/second.ts']['errors'], [])
        self.assertFalse(marker.exists())
        self.write('src/duplicate.ts', 'export const value = 1; export const value = 2;\n')
        self.assertTrue(typescript_modules.module_details(self.repo, ['src/duplicate.ts'])['src/duplicate.ts']['errors'])
        self.write("src/bad.ts", "function outer() {export const value: number = 1;}\n")
        with self.assertRaisesRegex(merge.MergeError, "语法"):
            merge.check_js_modules(["src/bad.ts"])

    def test_duplicate_value_keys_rejected_but_accessor_pair_kept(self):
        self.write("src/owner.ts", "export const row: object = {value: 1, value: 2};\n")
        with self.assertRaisesRegex(merge.MergeError, "重复键.*value"):
            merge.check_js_duplicate_keys(["src/owner.ts"])
        self.write("src/owner.ts", "export const row = {get value(): number {return 1;}, set value(v: number) {}};\n")
        merge.check_js_duplicate_keys(["src/owner.ts"])

    def test_js_only_validation_keeps_missing_export_failure(self):
        self.write("src/owner.js", "export const value = 1;\n")
        self.write("src/caller.js", "import {missing} from './owner.js';\n")
        with self.assertRaisesRegex(merge.MergeError, "missing"):
            merge.check_js_imports(["src/owner.js"])

    def test_typed_source_without_parser_fails_instead_of_skipping(self):
        (self.repo / "shiftboss.toml").write_text('[merge]\njs_module_prefixes = ["src/"]\n')
        self.write("src/owner.ts", "export const value: number = 1;\n")
        with self.assertRaisesRegex(merge.MergeError, "typescript_parser"):
            merge.check_js_modules(["src/owner.ts"])

    def test_same_js_ts_implementation_is_rejected_for_both_caller_kinds(self):
        self.write("src/owner.ts", "export const value: number = 1;\n")
        self.write("src/owner.js", "export const value = 1;\n")
        self.write("src/caller.js", "import {value} from './owner.js';\n")
        with self.assertRaisesRegex(merge.MergeError, "双实现"):
            merge.check_js_imports(["src/caller.js"])
        self.write("src/caller.ts", "import {value} from './owner.js';\n")
        with self.assertRaisesRegex(merge.MergeError, "双实现"):
            merge.check_js_imports(["src/caller.ts"])

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

    def test_background_gate_scans_unchanged_readers_and_keeps_original_failure(self):
        from shiftboss.named_imports_check import run
        self.write('src/owner.ts', 'export interface Shape {size: number}\n')
        self.write('src/unchanged.ts', "import {Shape} from './owner.js'; console.log(Shape);\n")
        with self.assertRaisesRegex(merge.MergeError, '运行.*缺失'):
            run(self.repo)
        self.write('src/unchanged.ts', "import type {Shape} from './owner.js';\n")
        run(self.repo)

    def test_declaration_imports_use_type_space_without_weakening_runtime_readers(self):
        from shiftboss.named_imports_check import run
        self.write('src/owner.ts', 'export interface Shape {size: number}\n')
        self.write('src/contracts.d.ts', "import {Shape} from './owner.js'; export type Entry = Shape;\n")
        run(self.repo)
        self.write('src/contracts.d.ts', "import {Missing} from './owner.js'; export type Entry = Missing;\n")
        with self.assertRaisesRegex(merge.MergeError, 'Missing.*类型.*缺失'):
            run(self.repo)
        self.write('src/contracts.d.ts', "import {Shape} from './owner.js'; export type Entry = Shape;\n")
        self.write('src/runtime.ts', "import {Shape} from './owner.js'; console.log(Shape);\n")
        with self.assertRaisesRegex(merge.MergeError, 'Shape.*运行.*缺失'):
            run(self.repo)

    def test_adjacent_declarations_do_not_replace_runtime_export_owner(self):
        for runtime, declaration in [('js', 'ts'), ('mjs', 'mts')]:
            with self.subTest(runtime=runtime):
                self.write(f'src/library.{runtime}',
                           "throw new Error('must not execute'); export function parse() {}\n")
                self.write(f'src/library.d.{declaration}',
                           'export declare function parse(): void; export interface Options {size: number}\n'
                           'export declare function missing(): void;\n')
                self.write('src/caller.ts',
                           f"import {{parse as read, type Options}} from './library.{runtime}';\n")
                merge.check_js_imports(['src/caller.ts'], include_readers=False)
                self.write('src/caller.ts', f"import {{missing}} from './library.{runtime}';\n")
                with self.assertRaisesRegex(merge.MergeError, 'missing'):
                    merge.check_js_imports(['src/caller.ts'], include_readers=False)
                self.write('src/caller.ts', f"import {{Options}} from './library.{runtime}';\n")
                with self.assertRaisesRegex(merge.MergeError, 'Options'):
                    merge.check_js_imports(['src/caller.ts'], include_readers=False)
                self.write('src/caller.ts', f"import type {{Absent}} from './library.{runtime}';\n")
                with self.assertRaisesRegex(merge.MergeError, 'Absent.*类型.*缺失'):
                    merge.check_js_imports(['src/caller.ts'], include_readers=False)

    def test_declarations_cannot_stand_in_for_missing_or_duplicate_runtime_sources(self):
        self.write('src/library.d.mts', 'export declare function parse(): void;\n')
        self.write('src/caller.ts', "import type {parse} from './library.mjs';\n")
        merge.check_js_imports(['src/caller.ts'], include_readers=False)
        self.write('src/caller.ts', "import {parse} from './library.mjs';\n")
        with self.assertRaisesRegex(merge.MergeError, '模块不存在'):
            merge.check_js_imports(['src/caller.ts'], include_readers=False)
        self.write('src/library.mjs', 'export const parse = () => {};\n')
        merge.check_js_imports(['src/caller.ts'], include_readers=False)
        self.write('src/library.mts', 'export function parse(): void {}\n')
        with self.assertRaisesRegex(merge.MergeError, '双实现'):
            merge.check_js_imports(['src/caller.ts'], include_readers=False)

    def test_background_switch_registers_exact_rule_without_mutating_old_gates(self):
        from shiftboss.named_imports_check import enabled, verification_gates
        gates = [['python', '-m', 'existing']]
        self.assertFalse(enabled(self.repo))
        self.assertEqual(verification_gates(gates, self.repo), gates)
        config = self.repo / 'shiftboss.toml'
        config.write_text(config.read_text() + 'named_imports_background = true\n')
        self.assertTrue(enabled(self.repo))
        registered = verification_gates(gates, self.repo)
        self.assertEqual(len(gates), 1)
        self.assertEqual(registered[-1][-2:], ['-m', 'shiftboss.named_imports_check'])
        self.assertEqual(verification_gates(registered, self.repo), registered)

    def test_source_guard_switches_register_each_original_rule_once(self):
        from shiftboss import source_guards
        from shiftboss.named_imports_check import verification_gates
        self.assertFalse(source_guards.enabled(self.repo, 'syntax'))
        self.assertFalse(source_guards.enabled(self.repo, 'duplicate-keys'))
        gates = [['python', '-m', 'existing']]
        self.assertEqual(verification_gates(gates, self.repo), gates)
        config = self.repo / 'shiftboss.toml'
        original = config.read_text()
        config.write_text(original + 'source_syntax_background = true\nduplicate_keys_background = true\n')
        registered = verification_gates(gates, self.repo)
        self.assertEqual([command[-1] for command in registered[1:]], ['syntax', 'duplicate-keys'])
        self.assertTrue(all(command[-2] == 'shiftboss.source_guards' for command in registered[1:]))
        self.assertEqual(verification_gates(registered, self.repo), registered)
        config.write_text(original + 'source_syntax_background = "yes"\n')
        with self.assertRaisesRegex(ValueError, 'source_syntax_background'):
            source_guards.enabled(self.repo, 'syntax')

    def test_background_syntax_checks_unchanged_tracked_sources_and_unique_owner(self):
        from shiftboss.source_guards import run
        self.write('src/owner.ts', 'export const value: number = 1;\n')
        (self.repo / 'src/untracked.ts').write_text('export const invalid: = 1;\n')
        run(self.repo, 'syntax')
        self.write('src/owner.ts', 'export const value: = 1;\n')
        with self.assertRaisesRegex(merge.MergeError, '语法'):
            run(self.repo, 'syntax')
        self.write('src/owner.ts', 'export const value: number = 1;\n')
        self.write('src/owner.js', 'export const value = 1;\n')
        with self.assertRaisesRegex(merge.MergeError, '双实现'):
            run(self.repo, 'syntax')
        (self.repo / 'src/owner.js').unlink()
        with self.assertRaisesRegex(merge.MergeError, '源码不存在'):
            run(self.repo, 'syntax')

    def test_background_duplicate_guard_retains_rejection_and_accessor_pair(self):
        from shiftboss.source_guards import run
        self.write('src/owner.ts', 'export const row = {value: 1, value: 2};\n')
        with self.assertRaisesRegex(merge.MergeError, '重复键.*value'):
            run(self.repo, 'duplicate-keys')
        self.write('src/owner.ts', 'export const row = {get value(): number {return 1;}, set value(v: number) {}};\n')
        run(self.repo, 'duplicate-keys')
        (self.repo / 'src/owner.ts').unlink()
        (self.repo / 'src/owner.ts').symlink_to(self.repo / 'src/target.ts')
        with self.assertRaisesRegex(merge.MergeError, '符号链接'):
            run(self.repo, 'duplicate-keys')

    def test_background_source_transport_keeps_all_inputs_and_last_error(self):
        from shiftboss.source_guards import run
        names = [f'src/record-{i:03d}.ts' for i in range(300)]
        for name in names:
            (self.repo / name).write_text('export const value = 1;\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', 'src'], check=True)
        error = merge.MergeError('last source error')
        seen = []

        def check(group):
            seen.extend(group)
            if names[-1] in group:
                raise error

        with patch.object(merge, 'check_js_modules', side_effect=check):
            with self.assertRaises(merge.MergeError) as caught:
                run(self.repo, 'syntax')
        self.assertIs(caught.exception, error)
        self.assertEqual(seen, names)

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

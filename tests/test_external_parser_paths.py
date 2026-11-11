"""External parser selection keeps the existing guards and missing-input failures."""
from pathlib import Path
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from shiftboss import config, typescript_modules


class ExternalParserPaths(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        self.environment = Path(self.temp.name) / 'external' / 'node_modules'
        self.environment.mkdir(parents=True)
        self.local = self.root / '.local.json'
        self.local.write_text(json.dumps({'packages': str(self.environment)}))
        self.declare()
        self.addCleanup(config.load.cache_clear)

    def declare(self, parser='{local="packages", path="typescript/lib/typescript.js"}'):
        (self.root / 'shiftboss.toml').write_text(
            '[project]\nlocal_config=".local.json"\n[merge]\n'
            'js_parser={local="packages", path="acorn/dist/acorn.mjs"}\n'
            f'typescript_parser={parser}\n')

    def test_both_parsers_resolve_outside_checkout_without_installation(self):
        self.assertEqual(config.parser_path('typescript_parser', self.root),
                         self.environment / 'typescript/lib/typescript.js')
        self.assertEqual(config.js_parser(self.root), str(self.environment / 'acorn/dist/acorn.mjs'))
        self.assertFalse((self.root / 'node_modules').exists())

    def test_missing_machine_configuration_is_an_error(self):
        self.local.unlink()
        with self.assertRaisesRegex(ValueError, '本机配置'):
            config.parser_path('typescript_parser', self.root)

    def test_bad_missing_or_relative_environment_is_not_a_fallback(self):
        for local in ({}, [], {'packages': 'relative'}, {'packages': None}, {'packages': ''}):
            self.local.write_text(json.dumps(local))
            with self.subTest(local=local), self.assertRaisesRegex(ValueError, '绝对环境目录'):
                config.parser_path('typescript_parser', self.root)
        self.local.write_text('{broken')
        with self.assertRaisesRegex(ValueError, '本机配置'):
            config.parser_path('typescript_parser', self.root)

    def test_environment_cannot_be_escaped_by_declaration_or_symlink(self):
        for declaration in ('{local="packages", path="../parser.js"}',
                            '{local="packages", path="/parser.js"}',
                            '{local="packages", path="parser.js", fallback=true}'):
            self.declare(declaration)
            with self.subTest(declaration=declaration), self.assertRaises(ValueError):
                config.parser_path('typescript_parser', self.root)
        (self.environment / 'escape').symlink_to(self.root, target_is_directory=True)
        self.declare('{local="packages", path="escape/parser.js"}')
        with self.assertRaisesRegex(ValueError, '环境根之外'):
            config.parser_path('typescript_parser', self.root)

    def test_existing_string_paths_keep_their_meaning(self):
        for declaration, expected in [('"tools/parser.js"', self.root / 'tools/parser.js'),
                                      (json.dumps(str(self.environment / 'parser.js')), self.environment / 'parser.js')]:
            self.declare(declaration)
            self.assertEqual(config.parser_path('typescript_parser', self.root), expected)

    def test_typescript_guard_passes_external_file_to_original_parser(self):
        compiler = self.environment / 'typescript/lib/typescript.js'
        compiler.parent.mkdir(parents=True)
        compiler.write_text('// parser path fixture only\n')
        (self.root / 'source.ts').write_text('export const value: number = 1;\n')
        with patch.object(typescript_modules.shutil, 'which', return_value='/usr/bin/node'), \
             patch.object(typescript_modules.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{}', '')) as run:
            self.assertEqual(typescript_modules.module_details(self.root, ['source.ts']), {})
        self.assertIn(str(compiler), run.call_args.args[0])
        compiler.unlink()
        with self.assertRaisesRegex(ValueError, '编译器不存在'), \
             patch.object(typescript_modules.subprocess, 'run') as run:
            typescript_modules.module_details(self.root, ['source.ts'])
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()

"""Run every discovered Python test file and emit machine-readable evidence.

Executed by absolute controller path, with cwd set to the fixed source checkout.
No hard-coded module count, and import failures cannot remove a file from the run.
"""
from __future__ import annotations
import argparse
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest


def test_files(root: Path, directories: list[str]) -> list[str]:
    files = set()
    for name in directories:
        directory = (root / name).resolve()
        if not directory.is_relative_to(root.resolve()) or not directory.is_dir():
            raise ValueError(f'测试目录不存在或越界：{name}')
        files.update(p.relative_to(root).as_posix() for p in directory.rglob('test_*.py'))
    if not files:
        raise ValueError('全量测试清单为空')
    return sorted(files)


def load_module(module: str, path: Path):
    """按模块名导入；这个名字解析到别处时按文件路径加载，测试标识不变。

    包源码与已安装的同名包共处一个项目时（如 `shiftboss/tests/`），模块名 `shiftboss.tests.*`
    会解析进已安装的包而找不到测试文件。
    """
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        spec = None
    if spec and spec.origin and Path(spec.origin).resolve() == path.resolve():
        return importlib.import_module(module)
    spec = importlib.util.spec_from_file_location(module, path)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[module] = loaded
    spec.loader.exec_module(loaded)
    return loaded


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--only', action='append', default=None,
                        help='只跑这些测试模块（归因探针用）；不在清单里的模块跳过')
    parser.add_argument('directories', nargs='+')
    args = parser.parse_args(argv)
    root = Path.cwd().resolve()
    sys.path.insert(0, str(root))
    files = test_files(root, args.directories)
    if args.only is not None:
        wanted = set(args.only)
        files = [name for name in files if name[:-3].replace('/', '.') in wanted]
    suite = unittest.TestSuite()
    modules = []
    for name in files:
        module = name[:-3].replace('/', '.')
        modules.append(module)
        try:
            suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(load_module(module, root / name)))
        except (Exception, SystemExit) as error:
            # unittest handles ImportError itself, but arbitrary import-time failures
            # must also become test results instead of aborting the remaining files.
            class ImportFailure(unittest.TestCase):
                def __init__(self, name, exception):
                    super().__init__()
                    self.module_name, self.exception = name, exception
                def id(self): return self.module_name + '.import'
                def runTest(self): raise self.exception
            suite.addTest(ImportFailure(module, error))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failures = [{'id': test.id(), 'detail': detail}
                for test, detail in result.failures + result.errors]
    failures.extend({'id': test.id(), 'detail': 'unexpected success'}
                    for test in result.unexpectedSuccesses)
    report = {'files': files, 'modules': modules, 'tests': result.testsRun,
              'failures': failures,
              'skipped': [{'id': t.id(), 'reason': reason} for t, reason in result.skipped],
              'expected_failures': len(result.expectedFailures)}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return 0 if result.wasSuccessful() else 1

if __name__ == '__main__':
    raise SystemExit(main())

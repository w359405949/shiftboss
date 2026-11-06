"""Existing JS/TS named-import rule, over all maintained sources in a verification gate."""
from __future__ import annotations

import json
from pathlib import Path
import sys

from shiftboss import config


def enabled(repo: Path) -> bool:
    value = config.load(repo).get('merge', {}).get('named_imports_background', False)
    if not isinstance(value, bool):
        raise ValueError('merge.named_imports_background 必须是布尔值')
    return value


def verification_gates(gates: list, repo: Path) -> list:
    result = list(gates)
    command = [sys.executable, '-B', '-m', 'shiftboss.named_imports_check']
    if enabled(repo) and command not in result:
        result.append(command)
    from shiftboss.background_commands import commands
    for background in commands(repo):
        if background not in result:
            result.append(background)
    return result


def run(repo: Path) -> None:
    from shiftboss import merge
    from shiftboss.typescript_modules import MODULE_SUFFIXES

    prefixes = config.js_module_prefixes(repo)
    names = merge.git('ls-files', '-z', '--', *prefixes).stdout.split('\0') if prefixes else []
    names = [name for name in names if name.endswith(MODULE_SUFFIXES)
             and any(name.startswith(prefix) for prefix in prefixes)]
    # The full tracked source set already includes every maintained reader.
    merge.check_js_imports(names, include_readers=False)
    revision = merge.git('rev-parse', 'HEAD', check=False)
    print(json.dumps({'check': 'named-imports', 'scope': 'all-tracked-maintained-sources',
                      'prefixes': prefixes, 'sources': len(names),
                      'source_revision': revision.stdout.strip() if not revision.returncode else None},
                     ensure_ascii=False), flush=True)


def main() -> int:
    from shiftboss import merge
    try:
        run(merge.REPO)
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

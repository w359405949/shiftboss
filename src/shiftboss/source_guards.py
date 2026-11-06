"""Run existing source guards over tracked modules in the normal verification lane.

merge.source_syntax_background and merge.duplicate_keys_background move the
corresponding foreground rule together with its normal verification command.
Both switches default to false; this entry also runs independently of them.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from shiftboss import config

FLAGS = {"syntax": "source_syntax_background", "duplicate-keys": "duplicate_keys_background"}


def enabled(repo: Path, rule: str) -> bool:
    key = FLAGS[rule]
    value = config.load(repo).get("merge", {}).get(key, False)
    if not isinstance(value, bool):
        raise ValueError(f"merge.{key} 必须是布尔值")
    return value


def commands(repo: Path) -> list[list[str]]:
    return [[sys.executable, "-B", "-m", "shiftboss.source_guards", rule]
            for rule in FLAGS if enabled(repo, rule)]


def run(repo: Path, rule: str) -> None:
    from shiftboss import merge
    from shiftboss.typescript_modules import MODULE_SUFFIXES

    prefixes = config.js_module_prefixes(repo)
    tracked = merge.git("ls-files", "-z", "--", *prefixes).stdout.split("\0") if prefixes else []
    names = sorted({name for name in tracked if name.endswith(MODULE_SUFFIXES)
                    and any(name.startswith(prefix) for prefix in prefixes)})
    for name in names:
        path = repo / name
        if path.is_symlink():
            raise merge.MergeError(f"不检查符号链接源码：{name}")
        if not path.is_file():
            raise merge.MergeError(f"Git 登记的源码不存在：{name}")
    if rule not in FLAGS:
        raise ValueError(f"未知源码守卫：{rule}")
    check = merge.check_js_modules if rule == "syntax" else merge.check_js_duplicate_keys
    # Both original rules operate per source; module-owner resolution still
    # inspects real companion paths, so splitting transport does not hide twins.
    for start in range(0, len(names), 256):
        group = names[start:start + 256]
        check(group)
        print(f"源码守卫 {rule}: {start + len(group)}/{len(names)} 份完成", file=sys.stderr, flush=True)
    revision = merge.git("rev-parse", "HEAD", check=False)
    print(json.dumps({"check": rule, "scope": "all-tracked-configured-modules",
                      "prefixes": prefixes, "sources": len(names),
                      "source_revision": revision.stdout.strip() if not revision.returncode else None},
                     ensure_ascii=False), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rule", choices=tuple(FLAGS))
    args = parser.parse_args()
    from shiftboss import merge
    try:
        run(merge.REPO, args.rule)
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

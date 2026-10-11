"""新增测试项的识别（DONE.md 第 2 节第 7 条）：只放行经用户同意、登记在 `[verify] approved_tests` 的。

`shiftboss check`、合并与 hub 推送守卫共用这一份，三处口径一致。查四处：
测试目录下的新文件、已有测试文件里净增的用例名（改名与重写不算）、测试目录外新出现的测试框架导入、
生成计划的闸里新出现的参数。
"""
from __future__ import annotations

import json
import re
from typing import Callable

TEST_NAME = re.compile(r'^[+-]\s*(?:(?:test|it|describe)\s*\(\s*([\'"`])(.*?)\1|(?:async\s+)?def\s+(test_\w+)\s*\()')
FRAMEWORK = re.compile(r'^\+.*(?:from\s+[\'"]node:(?:test|assert)(?:/strict)?[\'"]|require\(\s*[\'"]node:(?:test|assert)'
                       r'|^\+\s*(?:import|from)\s+(?:unit' r'test|py' r'test)\b)')
SOURCE = (".js", ".mjs", ".cjs", ".py")


def find(git: Callable[..., str], before: str, after: str | None, dirs: list[str],
         plans: tuple[str | None, str | None], approved: set[str], untracked: list[str] | None = None,
         read: Callable[[str], str] | None = None) -> list[str]:
    """`git(*args)` 返回标准输出；`after` 为 None 时比对工作树，`untracked` 为工作树里未跟踪的文件，`read` 读其内容。

    `plans` 是改动前后的生成计划文本（读不到为 None）。
    """
    span = [before] if after is None else [before, after]
    found: list[str] = []
    in_dirs = lambda path: any(path == d or path.startswith(d.rstrip("/") + "/") for d in dirs)
    if dirs:
        added = git("diff", "--name-only", "--no-renames", "--diff-filter=A", *span, "--", *dirs).split()
        added += [p for p in untracked or [] if in_dirs(p)]
        found += [f"新测试文件 {p}" for p in added if p not in approved]
        names: dict[str, dict[str, set[str]]] = {}
        path = ""
        for line in git("diff", "--no-renames", "-U0", "--diff-filter=M", *span, "--", *dirs).splitlines():
            if line.startswith("+++ "):
                path = line[6:] if line.startswith("+++ b/") else line[4:]
            elif not line.startswith("---") and (hit := TEST_NAME.match(line)):
                names.setdefault(path, {"+": set(), "-": set()})[line[0]].add(hit.group(2) or hit.group(3))
        for path, sides in names.items():
            extra = sorted(sides["+"] - sides["-"])[:max(0, len(sides["+"]) - len(sides["-"]))]
            found += [f"新测试用例 {path}::{name}" for name in extra
                      if path not in approved and f"{path}::{name}" not in approved]
    path = ""
    for line in git("diff", "--no-renames", "-U0", *span, "--", ".", *[f":!{d}" for d in dirs]).splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else line[4:]
        elif path.endswith(SOURCE) and path not in approved and FRAMEWORK.match(line):
            found.append(f"测试目录外的测试框架导入 {path}")
            path = ""   # 每个文件报一次
    for path in untracked or []:
        if in_dirs(path) or not path.endswith(SOURCE) or path in approved or read is None:
            continue
        if any(FRAMEWORK.match("+" + line) for line in read(path).splitlines()):
            found.append(f"测试目录外的测试框架导入 {path}")
    old, new = plans
    if old is not None and new is not None:
        before_tokens = {token for gate in json.loads(old).get("gates", []) for token in gate}
        after_tokens = {token for gate in json.loads(new).get("gates", []) for token in gate}
        found += [f"闸里新出现 {token}" for token in sorted(after_tokens - before_tokens) if token not in approved]
    return found


def forbidden(git: Callable[..., str], before: str, after: str | None, rules: list[dict],
              untracked: list[str] | None = None, read: Callable[[str], str] | None = None) -> list[str]:
    """项目在 `[[guard.forbid]]` 禁止的写法：每个文件净增的命中数大于 0 即报（改写、删减不算）。

    规则字段：`name`、`pattern`（正则，逐行匹配）、`paths`（路径前缀）、`allow`（放行的文件）、`reason`。
    """
    span = [before] if after is None else [before, after]
    found = []
    for rule in rules:
        pattern = re.compile(rule["pattern"])
        allow = set(rule.get("allow", []))
        counts: dict[str, int] = {}
        path = ""
        for line in git("diff", "--no-renames", "-U0", *span, "--", *rule.get("paths", ["."])).splitlines():
            if line.startswith("+++ "):
                path = line[6:] if line.startswith("+++ b/") else line[4:]
            elif line[:1] in "+-" and not line.startswith(("+++", "---")) and pattern.search(line[1:]):
                counts[path] = counts.get(path, 0) + (1 if line[0] == "+" else -1)
        prefixes = [prefix for prefix in rule.get("paths", []) if prefix != "."] or [""]
        for path in untracked or []:
            if read and any(path.startswith(prefix) for prefix in prefixes):
                counts[path] = counts.get(path, 0) + sum(1 for line in read(path).splitlines() if pattern.search(line))
        found += [f"新增{rule['name']} {path}（净增 {count} 处）：{rule.get('reason', '')}"
                  for path, count in sorted(counts.items()) if count > 0 and path not in allow]
    return found


MESSAGE = ("新增测试项未经用户同意（演示项目以外不加测试；要加先与用户讨论，同意后由派活方登记到 "
           "shiftboss.toml [verify] approved_tests；DONE.md 第 2 节第 7 条）。撤回这些改动后再推送：")

"""新增测试项的识别（DONE.md 第 2 节第 7 条）：只放行经用户同意、登记在 `[verify] approved_tests` 的。

`shiftboss check`、合并与 hub 推送守卫共用这一份，三处口径一致。按改名追踪：只搬动位置的文件不算新增。查四处：
测试目录下的新文件、已有测试文件里净增的用例名（改名与重写不算）、测试目录外新出现的测试框架导入、
生成计划的闸里新出现的参数。
"""
from __future__ import annotations

import json
import hashlib
import re
from typing import Callable

TEST_NAME = re.compile(r'^[+-]\s*(?:(?:test|it|describe)\s*\(\s*([\'"`])(.*?)\1|(?:async\s+)?def\s+(test_\w+)\s*\()')
FRAMEWORK = re.compile(r'^\+.*(?:from\s+[\'"]node:(?:test|assert)(?:/strict)?[\'"]|require\(\s*[\'"]node:(?:test|assert)'
                       r'|^\+\s*(?:import|from)\s+(?:unit' r'test|py' r'test)\b)')
SOURCE = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".py")


def approved_runtime_prefixes(git: Callable[..., str], task: str, new: str,
                             configuration: str, registered: list[list[str]]) -> list[list[str]]:
    """Only main's explicit approval may authorize an exact candidate configuration.

    The approval is data on main, never a declaration from the pushed branch.
    Only runtime preparation identity changes; payloads still pass find().
    """
    directory = f"shiftboss/plans/{task}/inputs/"
    paths = git('ls-tree', '-r', '--name-only', 'refs/heads/main', '--', directory).splitlines()
    text = git('show', f'{new}:{configuration}')
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    for path in paths:
        if not path.startswith(directory + 'user-approved-') or not path.endswith('.json'):
            continue
        try:
            packet = json.loads(git('show', f'refs/heads/main:{path}'))
            approval = packet.get('runtimePrefixApproval', {})
            decision = packet.get('decision', {})
            if (approval.get('schema') != 'shiftboss/approved-runtime-prefix/v1'
                    or packet.get('task') != task or not decision.get('answer')
                    or task not in decision.get('line', '').split(',')
                    or approval.get('configuration') != configuration
                    or approval.get('sha256') != digest
                    or packet.get('files', {}).get(configuration) != digest):
                continue
            prefixes = approval['prefixes']
            command_identity([], prefixes)  # Validate exact registered preparation syntax.
            return list(registered) + [prefix for prefix in prefixes if prefix not in registered]
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return registered


def prepared_runtime(prefix: list[str]) -> tuple[str, list[str]] | None:
    """主线精确登记的 Bash 准备边界；末项为 exec 消费者的 $0 身份。

    准备正文不从待检命令推断，也不单独授权；仅完整匹配已登记前缀时生效。
    管道可复用同一准备正文一次，但其余各腿仍须是原有字面命令。
    """
    if prefix[:4] != ["bash", "-o", "pipefail", "-c"] or len(prefix) != 6:
        return None
    script, executable = prefix[4:]
    if not script.endswith(' "$@"') or ' && exec ' not in script:
        return None
    preparation, invocation = script[:-len(' "$@"')].rsplit(' && exec ', 1)
    if not preparation or not re.fullmatch(r"[A-Za-z0-9_./=+,:@%\- \t]+", invocation):
        return None
    runtime = invocation.split()
    if not runtime or runtime[0] != executable or executable != "node":
        return None
    return preparation, runtime


def command_identity(gate: list[str], prefixes: list[list[str]]) -> tuple[str, ...]:
    """只去掉 main 已登记的精确运行前缀；规则参数与未知加载器不归一化。

    [verify].runtime_prefixes 的每项含原执行器及其受理的加载参数，
    例如 ["node", "--enable-source-maps", "--import", "./runtime/register.mjs"]。
    Bash 准备前缀须精确登记，末项给出原执行器；未登记 shell 不归一化。
    管道只剥离该精确准备正文，其余无引号、替换或控制语法的各腿保留。
    """
    for prefix in prefixes:
        if (not isinstance(prefix, list) or len(prefix) < 2
                or not all(isinstance(token, str) and token for token in prefix)
                or prefix[0].startswith("-")):
            raise ValueError("verify.runtime_prefixes 每项须为执行器及精确参数组成的非空字符串数组")
        if prefix[0] in ("bash", "sh") and prepared_runtime(prefix) is None:
            raise ValueError("shell 运行前缀须为精确登记的准备正文、exec 与原 Node 身份")
    for prefix in prefixes:
        if gate[:len(prefix)] == prefix and len(gate) > len(prefix):
            prepared = prepared_runtime(prefix)
            return tuple([prepared[1][0] if prepared else prefix[0], *gate[len(prefix):]])
    script = gate[-1] if gate else ""
    literal_prefixes = []
    stripped_preparation = False
    for prefix in prefixes:
        prepared = prepared_runtime(prefix)
        literal_prefixes.append(prepared[1] if prepared else prefix)
        if prepared and not stripped_preparation and script.startswith(prepared[0] + " && "):
            script = script[len(prepared[0] + " && "):]
            stripped_preparation = True
    if (len(gate) >= 3 and gate[0] in ("bash", "sh") and gate[-2] == "-c"
            and re.fullmatch(r"[A-Za-z0-9_./=+,:@%\- \t|]+", script)
            and all(part.strip() for part in script.split("|"))):
        parts = script.split("|")
        for index, part in enumerate(parts):
            leading = part[:len(part) - len(part.lstrip())]
            body = part[len(leading):]
            for prefix in literal_prefixes:
                literal = " ".join(prefix)
                if (all(re.fullmatch(r"[A-Za-z0-9_./=+,:@%\-]+", token) for token in prefix)
                        and body.startswith(literal + " ")):
                    parts[index] = leading + prefix[0] + body[len(literal):]
                    break
        return tuple([*gate[:-1], "|".join(parts)])
    return tuple(gate)


def find(git: Callable[..., str], before: str, after: str | None, dirs: list[str],
         plans: tuple[str | None, str | None], approved: set[str], untracked: list[str] | None = None,
         read: Callable[[str], str] | None = None,
         runtime_prefixes: list[list[str]] | None = None,
         previous_runtime_prefixes: list[list[str]] | None = None) -> list[str]:
    """`git(*args)` 返回标准输出；`after` 为 None 时比对工作树，`untracked` 为工作树里未跟踪的文件，`read` 读其内容。

    `plans` 是改动前后的生成计划文本（读不到为 None）。
    """
    span = [before] if after is None else [before, after]
    found: list[str] = []
    in_dirs = lambda path: any(path == d or path.startswith(d.rstrip("/") + "/") for d in dirs)
    if dirs:
        added = git("diff", "--name-only", "-M", "-l100000", "--diff-filter=A", *span, "--", *dirs).split()
        added += [p for p in untracked or [] if in_dirs(p)]
        found += [f"新测试文件 {p}" for p in added if p not in approved]
        names: dict[str, dict[str, set[str]]] = {}
        path = ""
        for line in git("diff", "-M", "-l100000", "-U0", "--diff-filter=MR", *span, "--", *dirs).splitlines():
            if line.startswith("+++ "):
                path = line[6:] if line.startswith("+++ b/") else line[4:]
            elif not line.startswith("---") and (hit := TEST_NAME.match(line)):
                names.setdefault(path, {"+": set(), "-": set()})[line[0]].add(hit.group(2) or hit.group(3))
        for path, sides in names.items():
            extra = sorted(sides["+"] - sides["-"])[:max(0, len(sides["+"]) - len(sides["-"]))]
            found += [f"新测试用例 {path}::{name}" for name in extra
                      if path not in approved and f"{path}::{name}" not in approved]
    path = ""
    for line in git("diff", "-M", "-l100000", "-U0", *span, "--", ".", *[f":!{d}" for d in dirs]).splitlines():
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
        # 按整条闸核对：调用的脚本已登记的闸放行，参数（如规则名）不单独登记。
        before_gates = {tuple(gate) for gate in json.loads(old).get("gates", [])}
        historical = runtime_prefixes if previous_runtime_prefixes is None else previous_runtime_prefixes
        identities = {command_identity(list(gate), historical or []) for gate in before_gates}
        before_tokens = {token for gate in before_gates for token in gate}
        for gate in json.loads(new).get("gates", []):
            if command_identity(gate, runtime_prefixes or []) in identities or any(token in approved for token in gate if token.endswith(SOURCE)):
                continue
            found += [f"闸里新出现 {token}" for token in gate if token not in before_tokens and token not in approved]
    return found


def forbidden(git: Callable[..., str], before: str, after: str | None, rules: list[dict],
              untracked: list[str] | None = None, read: Callable[[str], str] | None = None) -> list[str]:
    """项目在 `[[guard.forbid]]` 禁止的写法：每个文件净增的命中数大于 0 即报（改写、删减不算）。

    规则字段：`name`、`pattern`（正则，逐行匹配）、`paths`（路径前缀）、`exclude`（不查的路径前缀，如第三方发行树）、
    `allow`（放行的文件）、`reason`。
    """
    span = [before] if after is None else [before, after]
    found = []
    for rule in rules:
        pattern = re.compile(rule["pattern"])
        allow = set(rule.get("allow", []))
        exclude = tuple(rule.get("exclude", []))
        counts: dict[str, int] = {}
        path = ""
        for line in git("diff", "-M", "-l100000", "-U0", *span, "--", *rule.get("paths", ["."])).splitlines():
            if line.startswith("+++ "):
                path = line[6:] if line.startswith("+++ b/") else line[4:]
            elif line[:1] in "+-" and not line.startswith(("+++", "---")) and pattern.search(line[1:]):
                counts[path] = counts.get(path, 0) + (1 if line[0] == "+" else -1)
        prefixes = [prefix for prefix in rule.get("paths", []) if prefix != "."] or [""]
        for path in untracked or []:
            if read and any(path.startswith(prefix) for prefix in prefixes):
                counts[path] = counts.get(path, 0) + sum(1 for line in read(path).splitlines() if pattern.search(line))
        found += [f"新增{rule['name']} {path}（净增 {count} 处）：{rule.get('reason', '')}"
                  for path, count in sorted(counts.items())
                  if count > 0 and path not in allow and not (exclude and path.startswith(exclude))]
    return found


MESSAGE = ("新增测试项未经用户同意（演示项目以外不加测试；要加先与用户讨论，同意后由派活方登记到 "
           "shiftboss.toml [verify] approved_tests；DONE.md 第 2 节第 7 条）。撤回这些改动后再推送：")

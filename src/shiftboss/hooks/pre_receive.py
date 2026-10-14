"""推送守卫：拒收与 main 冲突的执行方分支（README 2.3 第 1 条），以及越权改动受保护路径的分支。

受保护路径由 main 上的 `shiftboss.toml` `[guard] protected` 给出；可改它们的线是 main 上任务书表头「写入范围」明确写出该受保护目录的（`[guard] owners` 存量名单照认）。

交回顶部执行状态为「停下」的照收：卡点报告须能立即推送（4.2），解不了的冲突按 2.3 第 4 条标待议交回。
未经用户同意的新增测试项（DONE.md 第 2 节第 7 条，口径见 `shiftboss.testguard`）拒收。
推送照收时回显认领给该线、仍红的常驻验证票据（README 4.1 第 10 条），在跑的线由此得知。
守卫自身出错时放行。
"""

import subprocess
import sys
import tomllib
from pathlib import Path

from shiftboss import config
from shiftboss.paths import PLANS_DIR
from shiftboss.state import read_state
from shiftboss.taskbook import execution_status


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, timeout=120)


def conflicts(new: str) -> list[str]:
    """与 main 合并冲突的路径；无冲突或判不出时为空。"""
    done = git("merge-tree", "--write-tree", "--name-only", "--no-messages", "refs/heads/main", new)
    if done.returncode != 1:
        return []
    return [line for line in done.stdout.splitlines()[1:] if line]


def main_config() -> dict:
    """main 上的 `shiftboss.toml`；读不到时为空。"""
    shown = git("show", "refs/heads/main:shiftboss.toml")
    return {} if shown.returncode else tomllib.loads(shown.stdout)


def guard_rules(settings: dict) -> tuple[list[str], set[str]]:
    """受保护路径前缀与可改它们的任务书名；没配时不设防。"""
    guard = settings.get("guard", {})
    return list(guard.get("protected", [])), set(guard.get("owners", []))


def red_notice(name: str, settings: dict) -> None:
    """回显认领给该线、仍红的票据；控制目录配在项目根下时 bare 里解析不了，不回显。"""
    if {"copies_dir", "control_dir"} & settings.get("project", {}).keys():
        return
    state = config.default_control_dir(Path.cwd()) / config.VERIFICATION_NAME
    try:
        last = read_state(state / "index.json").get("last")
        failures = read_state(state / "runs" / f"{last}.json").get("failures", []) if last else []
        claims = read_state(state / "claims.json")
    except BaseException:
        return
    reds = [item for item in failures if claims.get(item["ticket"], {}).get("taskbook") == name]
    if reds:
        print(f"提醒 codex/{name}：常驻验证在 main@{last[:12]} 上仍有认领给本线的红，下一段先修（README 4.1 第 10 条）；"
              f"该提交已含的改动不算待确认：", file=sys.stderr)
    for item in reds:
        print(f"  [{item['ticket']}] {item['id'][:160]}\n    筛查：{state / 'findings' / (item['ticket'] + '.md')}", file=sys.stderr)


def granted_prefixes(name: str, protected: list[str]) -> list[str]:
    """main 上任务书表头「写入范围」里明确写出的受保护目录（或其子目录）：即授予该线写权。

    写权跟着任务书走，不改 `shiftboss.toml`：该文件是生成输入，每改一次都触发大批生成阶段重跑。
    范围须是受保护文件本身，或受保护目录下的目录与文件，才算授权；写更宽的目录（如 `engine/`）不授予其下受保护路径。
    """
    import json
    from shiftboss.taskbook import SCOPE_KEY, dependency_list
    shown = git("show", f"refs/heads/main:{PLANS_DIR}/{name}/meta.json")
    if shown.returncode:
        return []
    scope = dependency_list(json.loads(shown.stdout).get(SCOPE_KEY))
    return [item for item in scope if any(
        item == prefix or (prefix.endswith("/") and item.startswith(prefix)) for prefix in protected)]


def trespass(new: str, protected: list[str], granted: list[str] = ()) -> list[str]:
    """分支自与 main 分叉以来改动的、未经表头授权的受保护路径。"""
    done = git("diff", "--name-only", "--no-renames", "refs/heads/main..." + new)
    if done.returncode:
        return []
    return [path for path in done.stdout.splitlines()
            if any(path == prefix or (prefix.endswith("/") and path.startswith(prefix)) for prefix in protected)
            and not any(path == item or (item.endswith("/") and path.startswith(item)) for item in granted)]


def scope_trespass(name: str, new: str) -> list[str]:
    """分支自与 main 分叉以来写到 main 上任务书表头「写入范围」之外的路径。"""
    import json
    from shiftboss.taskbook import out_of_scope
    shown = git("show", f"refs/heads/main:{PLANS_DIR}/{name}/meta.json")
    if shown.returncode:
        return []
    # 只算这条线自己的提交，经合并带进来的 main 提交不算。
    done = git("log", "--name-only", "--format=", "--no-renames", new, "--not", "refs/heads/main")
    if done.returncode:
        return []
    return out_of_scope(name, json.loads(shown.stdout), sorted({line for line in done.stdout.splitlines() if line}))


def new_tests(new: str, settings: dict) -> list[str]:
    """分支自与 main 分叉以来未登记的新增测试项。"""
    from shiftboss import testguard
    base = git("merge-base", "refs/heads/main", new).stdout.strip()
    if not base:
        return []
    plan = settings.get("regen", {}).get("plan", "regen.json")
    shown = lambda ref: (lambda done: None if done.returncode else done.stdout)(git("show", f"{ref}:{plan}"))
    return testguard.find(lambda *args: git(*args).stdout, base, new,
                          list(settings.get("merge", {}).get("assertion_dirs", [])),
                          (shown(base), shown(new)), set(settings.get("verify", {}).get("approved_tests", [])))


def forbidden(new: str, settings: dict) -> list[str]:
    """分支自与 main 分叉以来净增的项目禁止写法。"""
    from shiftboss import testguard
    rules = settings.get("guard", {}).get("forbid", [])
    base = git("merge-base", "refs/heads/main", new).stdout.strip()
    if not rules or not base:
        return []
    return testguard.forbidden(lambda *args: git(*args).stdout, base, new, rules)


def main() -> int:
    settings = main_config()
    protected, owners = guard_rules(settings)
    rejected = []
    trespassed = False
    for row in sys.stdin.read().splitlines():
        parts = row.split()
        if len(parts) != 3 or not parts[2].startswith("refs/heads/codex/") or set(parts[1]) == {"0"}:
            continue
        _old, new, ref = parts
        name = ref[len("refs/heads/codex/"):]
        if protected and name not in owners and (paths := trespass(new, protected, granted_prefixes(name, protected))):
            print(f"拒收 {ref}：改动了受保护路径，表头「写入范围」未授予；缺测试或构建改动写进交回，由派活方转交。", file=sys.stderr)
            for path in paths:
                print(f"  {path}", file=sys.stderr)
            trespassed = True
            continue
        if outside := scope_trespass(name, new):
            print(f"拒收 {ref}：写到了任务书表头「写入范围」之外，撤回这些改动或请派活方调整范围：", file=sys.stderr)
            for path in outside[:40]:
                print(f"  {path}", file=sys.stderr)
            trespassed = True
            continue
        if banned := forbidden(new, settings):
            print(f"拒收 {ref}：出现项目禁止的写法（shiftboss.toml [[guard.forbid]]），撤回后再推：", file=sys.stderr)
            for item in banned:
                print(f"  {item}", file=sys.stderr)
            trespassed = True
            continue
        if added := new_tests(new, settings):
            from shiftboss.testguard import MESSAGE
            print(f"拒收 {ref}：" + MESSAGE, file=sys.stderr)
            for item in added:
                print(f"  {item}", file=sys.stderr)
            trespassed = True
            continue
        paths = conflicts(new)
        delivery = git("show", f"{new}:{PLANS_DIR}/{name}/delivery.md").stdout if paths else ""
        if paths and execution_status(delivery) != "停下":
            rejected.append((ref, paths))
            continue
        red_notice(name, settings)
    for ref, paths in rejected:
        print(f"拒收 {ref}：与 main 冲突，先 git fetch origin 并合入 origin/main 再推（README 2.3、4.1）。", file=sys.stderr)
        for path in paths:
            print(f"  {path}", file=sys.stderr)
    return 1 if rejected or trespassed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        raise SystemExit(0)

"""推送守卫：业务代码开放，检查代码按main任务书「改检查」授权。

同时保留实际合并冲突、新增测试审批、禁止写法与产物提交守卫。
"""

import subprocess
import re
import sys
import tomllib
from pathlib import Path

from shiftboss import config
from shiftboss.paths import PLANS_DIR
from shiftboss.state import read_state
from shiftboss.taskbook import execution_status
from shiftboss.permissions import CHECKS_KEY, check_hits, check_paths


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


def check_trespass(name: str, new: str, settings: dict) -> list[str]:
    """检查授权只认main元数据，不认分支自授或旧路径清单。"""
    import json
    shown = git("show", f"refs/heads/main:{PLANS_DIR}/{name}/meta.json")
    if shown.returncode or json.loads(shown.stdout).get(CHECKS_KEY) == "是":
        return []
    done = git("diff", "--name-only", "--no-renames", "refs/heads/main..." + new)
    if done.returncode:
        return []
    return check_hits(done.stdout.splitlines(), settings)


def ignored_additions(new: str) -> list[str]:
    """分支自与 main 分叉以来新增的、被根 `.gitignore` 忽略的路径（强行提交的一次性产物）。"""
    from shiftboss.taskbook import ignored_paths
    shown = git("show", f"{new}:.gitignore")
    # 按净改动算：先提交后删除的不算。
    added = git("diff", "--diff-filter=A", "--name-only", "--no-renames", "refs/heads/main..." + new)
    if shown.returncode or added.returncode:
        return []
    return ignored_paths(shown.stdout, sorted({line for line in added.stdout.splitlines() if line}))


def new_tests(new: str, settings: dict, task: str | None = None) -> list[str]:
    """分支自与 main 分叉以来未登记的新增测试项。"""
    from shiftboss import testguard
    base = git("merge-base", "refs/heads/main", new).stdout.strip()
    if not base:
        return []
    plan = settings.get("regen", {}).get("plan", "regen.json")
    shown = lambda ref: (lambda done: None if done.returncode else done.stdout)(git("show", f"{ref}:{plan}"))
    historical = git('show', f'{base}:{config.CONFIG_NAME}')
    previous_prefixes = (tomllib.loads(historical.stdout).get('verify', {}).get('runtime_prefixes', [])
                         if historical.returncode == 0 else [])
    prefixes = settings.get('verify', {}).get('runtime_prefixes', [])
    if task:
        prefixes = testguard.approved_runtime_prefixes(
            lambda *args: git(*args).stdout, task, new, config.CONFIG_NAME, prefixes)
    return testguard.find(lambda *args: git(*args).stdout, base, new,
                          list(settings.get("merge", {}).get("assertion_dirs", [])),
                          (shown(base), shown(new)), set(settings.get("verify", {}).get("approved_tests", [])),
                          runtime_prefixes=prefixes,
                          previous_runtime_prefixes=previous_prefixes)


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
    rejected = []
    trespassed = False
    for row in sys.stdin.read().splitlines():
        parts = row.split()
        if len(parts) != 3 or not parts[2].startswith("refs/heads/codex/") or set(parts[1]) == {"0"}:
            continue
        _old, new, ref = parts
        name = ref[len("refs/heads/codex/"):]
        if touched := check_trespass(name, new, settings):
            print(f"拒收 {ref}：改动了检查本身（检查脚本、测试或基线），任务书表头未声明「改检查：是」；"
                  "撤回这些改动，确需改检查的写进交回由派活方定：", file=sys.stderr)
            for path in touched[:40]:
                print(f"  {path}", file=sys.stderr)
            trespassed = True
            continue
        if ignored := ignored_additions(new):
            print(f"拒收 {ref}：提交了被 .gitignore 忽略的路径；交回证据留在工作副本的 out/ 里、交回写绝对路径，不进提交：", file=sys.stderr)
            for path in ignored[:40]:
                print(f"  {path}", file=sys.stderr)
            trespassed = True
            continue
        if banned := forbidden(new, settings):
            print(f"拒收 {ref}：出现项目禁止的写法（shiftboss.toml [[guard.forbid]]），撤回后再推：", file=sys.stderr)
            for item in banned:
                print(f"  {item}", file=sys.stderr)
            trespassed = True
            continue
        if added := new_tests(new, settings, name):
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

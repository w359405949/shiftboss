#!/usr/bin/env python3
"""合并之后按固定顺序重新生成派生物，并跑一遍闸。

    shiftboss regen            # 生成 ＋ 跑闸
    shiftboss regen --check    # 只跑闸，不生成

**为什么要有这个入口**：顺序写在项目文档里、靠人记得跑，2026-09-02 我一天里漏了
三次——两次是同一条合同报「写回链与发布 target 脱节」，一次是模块图视图过期。
**每次都不是不知道，是合并完转头去核下一条线了。**
一条能跑的命令拦得住，一段文档拦不住。

**这份是工作流的通用外壳，具体跑什么由项目给。** 项目在仓库根放一个
`regen.json`：

    {"stages": [{"name": "…", "command": ["python", "-B", "-m", "…"],
                 "inputs": ["路径前缀/"], "after": ["上游阶段"]}, …],
     "gates": [["python", "-B", "-m", "..."], …]}

只跑输入被改动的阶段及其下游；`--full` 按顺序跑全部阶段，即完整生成。
旧格式 `steps`（每次全跑）仍可用，迁完即撤。

没有这个文件就什么都不做并说明——**不要在这里写死任何项目专有的模块名**。
"""

from __future__ import annotations
from shiftboss.paths import project_root

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from shiftboss.outputs import CATALOG, REPORT_ENV, SCHEMA, catalog_document, exact_paths, merge_catalog

REPO = project_root()


def load_plan() -> dict:
    """
    顺序由项目的生成计划给（`shiftboss.toml` 的 `[regen] plan`），**这里不写死任何项目专有的模块名**
    """
    from shiftboss import config
    path = REPO / config.plan_file()
    if not path.exists():
        raise SystemExit(
            f"没有 {path}。这份脚本是通用外壳，要跑什么由项目在生成计划里给：\n"
            '  {"steps": [[…生成命令…]], "gates": [[…闸…]]}')
    from shiftboss.state import read_state
    return read_state(path)


def test_ownership(plan: dict) -> dict[str, str]:
    """Derive test ownership from runnable unittest commands, never a second list."""
    layers = plan["gate_layers"]
    gates = plan["gates"]
    indices = [index for layer in layers.values() for index in layer["gates"]]
    if sorted(indices) != list(range(len(gates))):
        missing = [i for i in range(len(gates)) if i not in indices]
        extra = sorted({i for i in indices if indices.count(i) > 1 or i >= len(gates)})
        detail = "；".join(filter(None, [
            "未归层：" + "、".join(f"{i} {' '.join(gates[i])}" for i in missing) if missing else "",
            f"重复或越界下标：{extra}" if extra else ""]))
        raise ValueError(f"每道闸必须恰好归属一层（{detail}；gates 中间插入会使其后下标顺移）")
    owners = {}
    for name, layer in layers.items():
        if not layer["gates"]:
            raise ValueError(f"{name}: 层没有可运行的闸")
        for required in layer.get("requires", []):
            if required not in layers:
                raise ValueError(f"{name}: 未知依赖层 {required}")
        for index in layer["gates"]:
            command = gates[index]
            if command[:4] != ["python", "-B", "-m", "unittest"]:
                continue
            for module in command[4:]:
                if module.startswith("-"):
                    continue
                path = module.replace(".", "/") + ".py"
                if path in owners:
                    raise ValueError(f"测试重复归层：{path}")
                owners[path] = name
    return owners


def validate_layers(plan: dict) -> dict[str, str]:
    owners = test_ownership(plan)
    if plan.get("test_root"):
        actual = {path.relative_to(REPO).as_posix()
                  for path in (REPO / plan["test_root"]).rglob("test_*.py")}
        missing, stale = actual - owners.keys(), owners.keys() - actual
    else:
        # 没声明测试根就不查未归层，只查登记的模块是否存在。
        missing, stale = set(), {path for path in owners if not (REPO / path).is_file()}
    if missing or stale:
        raise ValueError(f"测试归层不完整：未归层={sorted(missing)}；不存在={sorted(stale)}")
    layers = plan["gate_layers"]
    for names in [plan["always_layers"], *plan["path_layers"].values()]:
        if not set(names) <= layers.keys():
            raise ValueError(f"路径规则引用未知层：{names}")
    return owners


STAGE_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
SHELL_CHARS = re.compile(r"[;&|<>`$\n]")


def matches_prefix(path: str, prefix: str) -> bool:
    return path == prefix or (prefix.endswith("/") and path.startswith(prefix))


# 「完整生成」只是按顺序跑全部阶段，套壳本身没有代码；阶段细节只在各自的命令里。
# 防跑偏靠这里与 `merge_catalog`：一条命令只调一个模块、不串第二条命令，一个产物只归一个阶段。
def validate_stages(plan: dict) -> list[dict] | None:
    """生成计划的 `stages`；仍用旧格式 `steps` 时为 None。"""
    stages = plan.get("stages")
    if stages is None:
        return None
    if plan.get("steps"):
        raise ValueError("stages 与 steps 不能同时声明")
    names: list[str] = []
    commands: set[tuple[str, ...]] = set()
    for stage in stages:
        name, command = stage.get("name"), stage.get("command")
        if not isinstance(name, str) or not STAGE_NAME.fullmatch(name) or name in names:
            raise ValueError(f"阶段名须唯一且为小写短横线：{name!r}")
        python = (isinstance(command, list) and len(command) >= 4 and command[:3] == ["python", "-B", "-m"]
                  and "-m" not in command[4:] and "-c" not in command[3:])
        node = (isinstance(command, list) and len(command) >= 2 and command[0] == "node"
                and command[1].endswith((".mjs", ".js")) and not command[1].startswith(("-", "/"))
                and not any(arg.startswith("-e") or arg in ("-p", "--eval", "--print") for arg in command[1:]))
        if (not (python or node) or not all(isinstance(arg, str) for arg in command)
                or any(SHELL_CHARS.search(arg) for arg in command)):
            raise ValueError(f"{name}: 命令只能是 python -B -m <一个模块> [参数] 或 node <仓库内脚本> [参数]，不串接其他命令")
        if tuple(command) in commands:
            raise ValueError(f"{name}: 与其他阶段命令重复")
        inputs = stage.get("inputs")
        if not isinstance(inputs, list) or not inputs or not all(isinstance(p, str) and p for p in inputs):
            raise ValueError(f"{name}: 须声明输入路径前缀 inputs")
        after = stage.get("after", [])
        if not isinstance(after, list) or not set(after) <= set(names):
            raise ValueError(f"{name}: after 只能引用排在它前面的阶段：{after}")
        names.append(name)
        commands.add(tuple(command))
    return stages


def select_stages(stages: list[dict], paths: list[str] | None, requested: list[str],
                  catalog_stages: set[str]) -> list[dict]:
    """输入改动的、点名的、从未生成过的阶段，连同全部下游；`paths` 为 None 即全量。"""
    names = [stage["name"] for stage in stages]
    unknown = set(requested) - set(names)
    if unknown:
        raise ValueError(f"未知阶段：{sorted(unknown)}")
    selected = set(names) if paths is None else set(requested) | (set(names) - catalog_stages)
    for stage in stages:
        if paths and any(matches_prefix(path, prefix) for path in paths for prefix in stage["inputs"]):
            selected.add(stage["name"])
        if selected & set(stage.get("after", [])):
            selected.add(stage["name"])
    return [stage for stage in stages if stage["name"] in selected]


def previous_catalog() -> dict | None:
    path = REPO / CATALOG
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def with_closure(stages: list[dict], root: Path | None = None) -> list[dict]:
    """声明的输入加上项目命令算出的代码读取闭包：生成计划只声明数据输入，代码依赖不手抄进计划。"""
    from shiftboss import config
    command = config.closure_command(root)
    if not command:
        return stages
    if command[0] == "python":
        command = [sys.executable, *command[1:]]
    done = subprocess.run(command, cwd=root or REPO, capture_output=True, text=True, encoding="utf-8", timeout=600)
    if done.returncode:
        raise ValueError(f"闭包命令失败（{' '.join(command)}）：{done.stderr.strip()[-500:]}")
    closure = json.loads(done.stdout)
    unknown = set(closure) - {stage["name"] for stage in stages}
    if unknown:
        raise ValueError(f"闭包命令给出未知阶段：{sorted(unknown)}")
    return [{**stage, "inputs": [*stage["inputs"], *closure.get(stage["name"], [])]} for stage in stages]


def stage_selection(stages: list[dict], args) -> list[dict]:
    stages = with_closure(stages)
    old = previous_catalog() or {}
    done = {p["stage"] for p in old.get("producers") or [] if p.get("stage")}
    paths = None if args.full else ([] if args.stage else changed_paths(args.base))
    return select_stages(stages, paths, args.stage, done)


def changed_paths(base: str | None = None) -> list[str]:
    """Include commits, index, worktree, both sides of renames and new files."""
    def git(*args):
        return subprocess.run(["git", *args], cwd=REPO, check=True,
                              capture_output=True).stdout.decode("utf-8")
    if base is None:
        merge_head = subprocess.run(["git", "rev-parse", "--verify", "-q", "MERGE_HEAD"],
                                    cwd=REPO, capture_output=True)
        base = "HEAD" if merge_head.returncode == 0 else git(
            "merge-base", "HEAD", "origin/main").strip()
    tracked = git("diff", "--name-only", "--no-renames", "-z", "--end-of-options", base, "--")
    untracked = git("ls-files", "--others", "--exclude-standard", "-z")
    return sorted(set(filter(None, (tracked + untracked).split("\0"))))


def select_layers(plan: dict, paths: list[str], requested: list[str] | None = None) -> list[str]:
    owners = test_ownership(plan)
    layers = plan["gate_layers"]
    selected = set(plan["always_layers"]) | set(requested or [])
    if not selected <= layers.keys():
        raise ValueError(f"未知层：{sorted(selected - layers.keys())}")
    for path in paths:
        if path in owners:
            selected.add(owners[path])
            continue
        matches = [prefix for prefix in plan["path_layers"] if
                   path == prefix or (prefix.endswith("/") and path.startswith(prefix))]
        if matches:
            selected.update(plan["path_layers"][max(matches, key=len)])
        else:
            # New source roots cannot silently escape the gates.
            selected.update(layers)
    pending = list(selected)
    while pending:
        for required in layers[pending.pop()].get("requires", []):
            if required not in selected:
                selected.add(required)
                pending.append(required)
    return [name for name in layers if name in selected]


def changed_files() -> dict[str, str]:
    """工作树里相对 HEAD 有改动或未跟踪的文件及其内容摘要；不存在的记为空串。"""
    listed = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"],
                            cwd=REPO, check=True, capture_output=True).stdout.decode("utf-8").split("\0")
    out = {}
    for row in listed:
        if len(row) < 4:
            continue
        name = row[3:]
        path = REPO / name
        out[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
    return out


def observed_writes(before: dict[str, str], after: dict[str, str]) -> set[str]:
    """两次快照之间内容变了的文件（新建、改写、删除、恢复原样都算）。"""
    return {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}


def run_all(label: str, cmds: list[list[str]], *, stop_on_failure: bool = False,
            outputs: list[dict] | None = None, parallel: bool = False,
            names: list[str] | None = None) -> bool:
    if parallel and (outputs is not None or stop_on_failure):
        raise ValueError("有产物依赖或失败即停的命令必须顺序执行")
    ok = True
    report_root = None
    if outputs is not None:
        parent = REPO / "out" / "producer-outputs"
        parent.mkdir(parents=True, exist_ok=True)
        report_root = Path(tempfile.mkdtemp(prefix="run-", dir=parent))

    def run_one(number: int, command: list[str]) -> tuple[bool, str]:
        lines: list[str] = []
        # 和调用入口用同一个 Python，不依赖 shell 是否激活了项目环境。
        cmd = [sys.executable, *command[1:]] if command[0] == "python" else command
        shown = subprocess.list2cmdline(command)
        started = time.monotonic()
        env = os.environ.copy()
        env.pop(REPORT_ENV, None)
        report_path = report_root / f"{number}.json" if report_root else None
        if report_path:
            env[REPORT_ENV] = str(report_path)
        try:
            # Node 生产端没有 Python 的审计钩子：它只声明产物，写了什么由这里前后对照工作树独立观察。
            before = changed_files() if report_path and command[0] == "node" else None
            proc = subprocess.run(cmd, cwd=str(REPO), capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", env=env)
            if proc.returncode == 0 and report_path:
                report = json.loads(report_path.read_text(encoding="utf-8"))
                if report.get("schema") != SCHEMA:
                    raise ValueError("生产端没有给出受支持的产物报告")
                if before is not None:
                    declared = set(exact_paths(report["files"]))
                    written = observed_writes(before, changed_files()) - {report_path.relative_to(REPO).as_posix()}
                    if not written <= declared:
                        raise ValueError(f"Node 生产端写了未声明的文件：{sorted(written - declared)[:10]}")
                    report = {**report, "written": sorted(written), "retained": sorted(declared - written)}
                files = exact_paths(report["files"])
                actual = set(exact_paths(report["written"])) | set(exact_paths(report["retained"]))
                if set(files) != actual:
                    raise ValueError("生产端产物声明与实际不符；仅报告，不自动扩展")
                entry = {"command": command, "files": files}
                if names:
                    entry = {"stage": names[number - 1], **entry}
                outputs.append(entry)
        except OSError as exc:
            proc = subprocess.CompletedProcess(cmd, 1, "", str(exc))
        except (ValueError, KeyError, TypeError) as exc:
            proc = subprocess.CompletedProcess(cmd, 1, "", str(exc))
        out = (proc.stdout + proc.stderr).strip()
        tail = out.splitlines()
        mark = "✔" if proc.returncode == 0 else "✘"
        lines.append(f"  {mark} {label} {number}/{len(cmds)} {shown}  ({int(time.monotonic() - started)}s)")
        if proc.returncode == 0 and report_path:
            lines.append(f"      生产端声明 {len(files)} 个路径；实写 {len(report['written'])}，"
                  f"原样 {len(report['retained'])}；{report_path}")
        if proc.returncode != 0:
            for line in tail[-4:]:
                lines.append(f"      {line[:160]}")
            # **红了就把整份输出落盘。** 上面这 4 行尾巴恰好是 unittest 的汇总
            # （「Ran N tests / FAILED」），**永远不是那条 traceback**——
            # 2026-09-02 我因此只知道「errors=1」，不知道是哪一页，
            # 只能整批重跑一遍七分钟，而那一遍还碰巧过了。
            # 文件名从命令里取，但**只留能进文件名的字符**——
            # 命令里带引号/分号时直接拼会得到非法路径（我第一版就是这么写的，
            # 反证时立刻 OSError）。取不出名字就用序号兜底。
            safe = re.sub(r"[^0-9A-Za-z._-]+", "-", shown).strip("-")[:60]
            log = REPO / "out" / "regen-failures" / f"{number}-{safe or 'gate'}.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(out, encoding="utf-8")
            lines.append(f"      完整输出：{log}")
        return proc.returncode == 0, "\n".join(lines)

    if parallel and cmds:
        # Each worker drains its own stdout/stderr. Only this thread prints a
        # complete result group, so concurrent failures never mix their logs.
        with ThreadPoolExecutor(max_workers=len(cmds)) as executor:
            pending = [executor.submit(run_one, number, command)
                       for number, command in enumerate(cmds, 1)]
            for future in as_completed(pending):
                success, output = future.result()
                print(output, flush=True)
                ok = success and ok
    else:
        for number, command in enumerate(cmds, 1):
            success, output = run_one(number, command)
            print(output, flush=True)
            ok = success and ok
            if not success and stop_on_failure:
                return False
    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="只跑闸，不重新生成")
    ap.add_argument("--steps", action="store_true", help="只重新生成，不跑闸")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--layer", action="append", default=[], help="指定层，可重复；仍带依赖层")
    group.add_argument("--all", action="store_true", help="明确跑全部层")
    ap.add_argument("--base", help="与此 Git 提交比较；默认合并前 HEAD 或与 origin/main 的共同祖先")
    ap.add_argument("--list", action="store_true", help="只显示实际选层与生成计划")
    ap.add_argument("--full", action="store_true", help="完整生成：按顺序跑全部阶段")
    ap.add_argument("--stage", action="append", default=[], help="点名阶段，可重复；连同下游")
    args = ap.parse_args(argv)
    if args.full and args.stage:
        ap.error("--full 与 --stage 不能一起使用")
    if args.check and args.steps:
        ap.error("--check 与 --steps 不能一起使用")
    if args.steps and (args.layer or args.all):
        ap.error("--steps 是完整生成入口，不能指定闸层")

    # **手工恢复那条路也要下锁。** `shiftboss.merge` 一进门就下锁，可
    # 「`git merge --no-commit` ＋ 这里 ＋ `git commit --no-edit`」那条路没有入口
    # 能下——而它同样是「合并没提交完」的状态：这期间任何一次无关的 `git commit`
    # 都会把整个索引连同 `MERGE_HEAD` 一起交掉，闸一步没跑。
    from shiftboss.state import acquire_merge_lock, release_merge_lock, lock_path
    holding = False
    if (REPO / ".git" / "MERGE_HEAD").exists() and not lock_path(REPO).exists():
        acquire_merge_lock(REPO, "why=手工合并恢复中(shiftboss.regen)")
        holding = True
    try:
        return _run(args)
    finally:
        if holding:
            release_merge_lock(REPO)


def _run(args) -> int:
    """
    合并后按固定顺序重新生成派生物并跑闸

    `--check` 只跑闸不生成
    """
    plan = load_plan()
    try:
        stages = validate_stages(plan)
    except ValueError as exc:
        print(f"生成计划无效：{exc}")
        return 1
    if stages is None and (args.full or args.stage):
        print("生成计划仍是 steps 格式，没有阶段可选")
        return 1
    ok = True
    selected = None
    generate = True
    if "gate_layers" in plan:
        try:
            validate_layers(plan)
            paths = [] if args.layer or args.all or args.steps else changed_paths(args.base)
            selected = select_layers(plan, paths, list(plan["gate_layers"]) if args.all else args.layer)
        except (ValueError, KeyError, subprocess.CalledProcessError) as exc:
            print(f"选层失败：{exc}")
            return 1
        generate = (args.steps or args.full or bool(args.stage)
                    or any(plan["gate_layers"][name].get("generate", False) for name in selected))
        print(f"改动路径 {len(paths)} 个；选层：{', '.join(selected)}", flush=True)
        print(f"本次生成：{'是' if generate and not args.check else '否'}", flush=True)
    elif args.layer:
        print("regen.json 没有声明分层，不能指定 --layer")
        return 1
    if args.list:
        # **`--list` 要真的把要跑的东西列出来。** 只报选层不报命令，
        # 等于让人拿它当「能看到这一步」的判据时无从核对——
        # 2026-09-09 一份任务书就因为这条判据不可能满足而挂了待议。
        chosen = stage_selection(stages, args) if stages is not None else None
        if chosen is not None:
            print(f"生成阶段（{len(chosen)}/{len(stages)}）：{', '.join(s['name'] for s in chosen) or '无'}", flush=True)
        for label, commands in (("生成步骤", (plan.get("steps") or []) if chosen is None
                                 else [s["command"] for s in chosen]),
                                ("闸", [plan["gates"][i] for name in selected
                                       for i in plan["gate_layers"][name].get("gates", [])]
                                 if plan.get("gate_layers") else plan.get("gates") or [])):
            print(f"{label}（{len(commands)} 条）：", flush=True)
            for command in commands:
                print(f"  {' '.join(command)}", flush=True)
        return 0
    if not args.check and generate:
        print("重新生成派生物：")
        # **顺序要紧**，按 regen.json 里写的来，不要并发也不要跳步——
        # 后一步吃前一步的产物，跳了会得到「看起来绿、其实是旧输入」的结果。
        producers: list[dict] = []
        if stages is None:
            ok = run_all("生成", plan.get("steps") or [], stop_on_failure=True, outputs=producers)
        else:
            chosen = stage_selection(stages, args)
            print(f"生成阶段（{len(chosen)}/{len(stages)}）：{', '.join(s['name'] for s in chosen) or '无'}", flush=True)
            ok = run_all("生成", [s["command"] for s in chosen], stop_on_failure=True,
                         outputs=producers, names=[s["name"] for s in chosen])
        if not ok:
            print("\n生成这一步就红了，闸不跑了——先把上面那条修好。")
            return 1
        try:
            catalog = (catalog_document(producers) if stages is None
                       else merge_catalog(previous_catalog(), stages, producers))
        except ValueError as exc:
            print(f"产物报告不成立：{exc}")
            return 1
        (REPO / CATALOG).write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n")
        count = len({name for producer in producers for name in producer["files"]})
        print(f"产物报告：{count} 个生成器产物 + 1 份报告 = {count + 1} 个精确路径。", flush=True)
    if args.steps:
        print("\n只生成，没跑闸。接着跑 `--check`。")
        return 0
    # **生成 ＋ 跑闸串起来会很久**（本项目实测两段合计超过十分钟，撞过调用方的超时）。
    # 所以留了 `--steps` / `--check` 分两段跑；一次跑完只在时间够时用。
    print("跑闸（按层，层内按配置并行）：" if selected is not None else "跑闸（并行）：", flush=True)
    gate_started = time.monotonic()
    if selected is None:
        ok = run_all("闸", plan.get("gates") or [], parallel=True) and ok
    else:
        for position, name in enumerate(selected):
            layer = plan["gate_layers"][name]
            started = time.monotonic()
            commands = [plan["gates"][index] for index in layer["gates"]]
            passed = run_all(f"闸[{name}]", commands, parallel=layer.get("parallel", False))
            print(f"层 {name} 墙钟：{time.monotonic() - started:.3f}s", flush=True)
            ok = passed and ok
            if not passed:
                skipped = selected[position + 1:]
                print(f"层 {name} 红；后续层未跑：{', '.join(skipped) or '无后续层'}。", flush=True)
                break
    print(f"闸合计墙钟：{time.monotonic() - gate_started:.3f}s", flush=True)
    print("\n全绿。" if ok else "\n**有红的，上面写着是哪一条。**")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    raise SystemExit(main())

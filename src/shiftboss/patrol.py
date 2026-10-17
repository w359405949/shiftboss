"""常驻巡检：按项目登记的页面清单挨个核对 main 的最新提交，查到一页红就敲一次门铃。

与常驻验证并行、分工不同：常驻验证只核本批改到的页面，快；巡检不看改动，一遍遍从头扫全部页面，
兜住改动之外受牵连的页面。逐页串行，一次只占一个浏览器。

项目在生成计划（`regen.json`）的 `verification.patrol` 登记两条命令：

    "patrol": {"list": [...], "check": [..., "{route}", ...]}

- `list` 在工作区运行，标准输出某一行是 JSON 对象，其 `pages` 为 `[{"route": ...}, ...]`；
- `check` 对一页运行，`{route}` 换成该页路由、`{index}` 换成序号；退出码非 0 即该页红，输出记进日志。

用法：
    shiftboss patrol start      # 起后台进程
    shiftboss patrol status     # 当前进度与红页
    shiftboss patrol stop       # 当前页核完后停
    shiftboss patrol claim <路由> <任务书名>   # 红页关联负责修它的线（在办或已归档）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from shiftboss import config
from shiftboss.doorbell import ring
from shiftboss.paths import project_root
from shiftboss.state import read_state, write_state
from shiftboss.verify import git, origin

NAME = "patrol"
CHECK_TIMEOUT = 900   # 单页上限：页面闸自身有 120 秒会话期限与两次启动重试，超过即视为卡死
MIN_FREE_BYTES = 5 * 2**30   # 磁盘剩余低于此值即停：逐页证据会累积，写满会拖垮全部执行方与常驻验证


def state_dir(remote: Path) -> Path:
    return config.control_dir(remote) / NAME


def now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


class Patrol:
    def __init__(self, repo: Path, workspace: Path):
        self.repo = repo.resolve()
        self.remote = origin(self.repo)
        self.workspace = workspace.resolve()
        if self.workspace in (self.repo, self.remote):
            raise ValueError("巡检副本必须独立")
        self.directory = state_dir(self.remote)
        (self.directory / "logs").mkdir(parents=True, exist_ok=True)
        self.state = read_state(self.directory / "state.json") or {"pass": 0, "red": {}}

    def save(self) -> None:
        write_state(self.directory / "state.json", self.state)

    def prepare(self) -> None:
        marker = self.workspace / ".git/shiftboss-patrol-owned"
        if self.workspace.exists():
            if not marker.is_file() or marker.read_text(encoding="utf-8") != str(self.remote):
                raise ValueError("拒绝接管不是巡检创建的工作副本")
        else:
            git(self.repo, "clone", "--no-checkout", str(self.remote), str(self.workspace))
            marker.write_text(str(self.remote), encoding="utf-8")

    def policy(self) -> dict:
        plan = json.loads((self.workspace / config.plan_file(self.repo)).read_text(encoding="utf-8"))
        patrol = (plan.get("verification") or {}).get("patrol")
        if not patrol or not patrol.get("list") or not patrol.get("check"):
            raise ValueError(f"{config.plan_file(self.repo)} 的 verification.patrol 缺 list 或 check")
        return patrol

    def checkout_tip(self) -> str:
        git(self.workspace, "fetch", "--quiet", "origin", "main")
        sha = git(self.workspace, "rev-parse", "FETCH_HEAD").stdout.strip()
        git(self.workspace, "checkout", "--quiet", "--force", "--detach", sha)
        git(self.workspace, "clean", "-fdq", "-e", "out/")
        # 本机路径配置不进 git，clean 会删掉它；每遍检出后从项目根重拷。
        name = config.local_config_file(self.repo)
        local = self.repo / name if name else None
        if local and local.is_file():
            shutil.copyfile(local, self.workspace / local.name)
        return sha

    def routes(self, command: list[str]) -> list[str]:
        p = subprocess.run(command, cwd=self.workspace, capture_output=True, text=True, encoding="utf-8")
        for line in p.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                data = json.loads(line)
                if isinstance(data.get("pages"), list):
                    return [page["route"] for page in data["pages"]]
        raise ValueError(f"巡检清单命令没有输出 pages：{p.stderr.strip()[-500:]}")

    def check(self, command: list[str], route: str, index: int) -> tuple[int, Path]:
        log = self.directory / "logs" / f"{self.state['pass']:04d}-{index:03d}.log"
        argv = [part.replace("{route}", route).replace("{index}", str(index)) for part in command]
        env = os.environ.copy(); env["PYTHONIOENCODING"] = "utf-8"
        with log.open("w", encoding="utf-8") as handle:
            handle.write(f"{now()} {route}\n$ {subprocess.list2cmdline(argv)}\n")
            handle.flush()
            try:
                code = subprocess.run(argv, cwd=self.workspace, stdout=handle, stderr=subprocess.STDOUT,
                                      env=env, timeout=CHECK_TIMEOUT).returncode
            except subprocess.TimeoutExpired:
                handle.write(f"\n超过 {CHECK_TIMEOUT} 秒，按卡死记红\n")
                code = 124
        return code, log

    def record(self, route: str, code: int, log: Path, sha: str) -> None:
        red = self.state["red"]
        if code == 0:
            if red.pop(route, None):
                self.save()
            return
        known = red.get(route)
        red[route] = {"first": known["first"] if known else now(), "last": now(), "commit": sha,
                      "log": str(log), "code": code}
        self.save()
        if not known:
            ring(self.remote, f"patrol red {route}; run shiftboss patrol status")

    def watch(self) -> None:
        lock = self.directory / "worker.lock"
        try:
            handle = lock.open("x", encoding="utf-8")
        except FileExistsError:
            raise ValueError(f"已有巡检进程锁 {lock}；确认其 PID 已结束后才能删除")
        with handle:
            handle.write(str(os.getpid()))
        stop = self.directory / "stop"
        stop.unlink(missing_ok=True)
        try:
            self.prepare()
            while not stop.exists():
                sha = self.checkout_tip()
                policy = self.policy()
                routes = self.routes(policy["list"])
                self.state["pass"] += 1
                self.state.update(commit=sha, total=len(routes), position=0, started=now(), error=None)
                self.save()
                for index, route in enumerate(routes):
                    if stop.exists():
                        break
                    free = shutil.disk_usage(self.workspace).free
                    if free < MIN_FREE_BYTES:
                        raise RuntimeError(f"磁盘剩余 {free / 2**30:.1f} GiB，低于 {MIN_FREE_BYTES / 2**30:.0f} GiB，巡检停下")
                    self.state.update(position=index + 1, route=route); self.save()
                    code, log = self.check(policy["check"], route, index)
                    self.record(route, code, log, sha)
                else:
                    self.state["finished"] = now(); self.save()
        except Exception as exc:
            self.state["error"] = str(exc); self.save()
            ring(self.remote, "patrol failed; run shiftboss patrol status")
            raise
        finally:
            lock.unlink(missing_ok=True)


def owner(remote: Path, taskbook: str) -> dict | None:
    """main 上该任务书的归属状态：在办，或已归档及其合并提交；main 上没有返回 None。"""
    from shiftboss.taskbook import from_ref
    def _read(rel):
        out = git(remote, "show", f"refs/heads/main:{rel}", check=False)
        return "" if out.returncode else out.stdout
    meta, _body = from_ref(_read, taskbook)
    if meta is None:
        return None
    status = str(meta.get("调度状态") or "")
    merged = re.search(r"合并 ([0-9a-f]{7,40})", status) if status.startswith("已归档") else None
    return {"archived": status.startswith("已归档"), "merge": merged.group(1) if merged else None}


def claim(remote: Path, route: str, taskbook: str) -> None:
    if Path(taskbook).name != taskbook:
        raise ValueError("任务书给文件名，不带目录或扩展名")
    red = (read_state(state_dir(remote) / "state.json") or {}).get("red", {})
    if route not in red:
        raise ValueError("这一页当前不红；认领 `patrol status` 列出的红页")
    if owner(remote, taskbook) is None:
        raise ValueError("认领拒绝：main 上没有这份任务书")
    # 认领绑定这一段红（首见时刻）：消红后再红是新的一段，旧认领不再显示；巡检进程因此不必写认领文件。
    path = state_dir(remote) / "claims.json"
    claims = {key: item for key, item in (read_state(path) or {}).items()
              if key in red and item.get("first") == red[key]["first"]}
    claims[route] = {"taskbook": taskbook, "first": red[route]["first"], "claimed": now()}
    write_state(path, claims)
    print(f"已认领 {route} → {taskbook}")


def red_state(remote: Path, item: dict, claimed: dict | None) -> tuple[str, str]:
    """→ (状态词, 负责线)。只有「无人负责」与「修复无效」要开线。"""
    if not claimed or claimed.get("first") != item["first"]:
        return "无人负责", ""
    taskbook = claimed["taskbook"]
    info = owner(remote, taskbook)
    if info is None:
        return "任务书缺失", taskbook
    if not info["archived"]:
        return "在修", taskbook
    if not info["merge"]:
        return "已归档", taskbook
    scanned = git(remote, "merge-base", "--is-ancestor", info["merge"], item["commit"], check=False).returncode == 0
    return ("修复无效" if scanned else "待复扫"), taskbook


def status_lines(remote: Path, detail: bool = False) -> list[str]:
    """进度一行；红页按（状态，负责线）合并成行。`detail` 再逐页列首见、所在遍与日志（`status` 用）。"""
    import psutil
    directory = state_dir(remote)
    state = read_state(directory / "state.json")
    lock = directory / "worker.lock"
    pid = lock.read_text(encoding="utf-8").strip() if lock.exists() else ""
    alive = pid.isdigit() and psutil.pid_exists(int(pid))
    if not state:
        return [f"- 进度｜{'在跑' if alive else '没在跑'}｜尚无记录"]
    head = (f"- 进度｜{'在跑' if alive else '没在跑'}｜第 {state.get('pass', 0)} 遍 "
            f"{state.get('position', 0)}/{state.get('total', '?')}｜main@{(state.get('commit') or '')[:12]}")
    lines = [head]
    if state.get("error"):
        lines.append(f"- 出错｜{state['error'][:120]}")
    claims = read_state(directory / "claims.json") or {}
    groups: dict[tuple[str, str], list[str]] = {}
    reds = sorted(state.get("red", {}).items(), key=lambda pair: pair[1]["first"])
    for route, item in reds:
        groups.setdefault(red_state(remote, item, claims.get(route)), []).append(route)
    for (word, taskbook), routes in groups.items():
        lines.append(f"- 红 {len(routes)}｜{word}" + (f"｜{taskbook}" if taskbook else "") + "｜" + "、".join(route.removeprefix("/?view=") for route in routes))
    if detail:
        for route, item in reds:
            lines.append(f"  {route}｜首见 {item['first'][5:16].replace('T', ' ')}｜{item['log']}")
    return lines


def stop_now(remote: Path) -> int:
    import psutil
    lock = state_dir(remote) / "worker.lock"
    pid = lock.read_text(encoding="utf-8").strip() if lock.exists() else ""
    if pid.isdigit() and psutil.pid_exists(int(pid)):
        root = psutil.Process(int(pid))
        tree = root.children(recursive=True) + [root]
        for proc in tree:
            try:
                proc.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(tree, timeout=10)
        print(f"已结束常驻巡检 PID {pid} 及其 {len(tree) - 1} 个子进程")
    else:
        print("常驻巡检没在跑")
    lock.unlink(missing_ok=True)
    return 0


def start(repo: Path, workspace: Path) -> int:
    import psutil
    from shiftboss import native
    lock = state_dir(origin(repo)) / "worker.lock"
    if lock.exists():
        pid = lock.read_text(encoding="utf-8").strip()
        if pid.isdigit() and psutil.pid_exists(int(pid)):
            print(f"常驻巡检已在跑：PID {pid}"); return 1
        lock.unlink()
    argv = [sys.executable, "-B", "-m", "shiftboss.patrol", "watch", "--repo", str(repo),
            "--workspace", str(workspace.resolve())]
    print(f"常驻巡检已起：PID {native.spawn(argv, str(repo))}"); return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["watch", "start", "status", "stop", "claim"])
    parser.add_argument("route", nargs="?", help="claim：红页路由，照 `status` 所列")
    parser.add_argument("taskbook", nargs="?", help="claim：负责修它的任务书名")
    parser.add_argument("--repo", type=Path, default=None)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--now", action="store_true", help="stop：不等当前页核完，立即结束整棵进程树")
    args = parser.parse_args(argv)
    repo = (args.repo or project_root()).resolve()
    remote = origin(repo)
    workspace = args.workspace or config.copies_dir(remote) / NAME
    if args.command == "status":
        print("\n".join(status_lines(remote, detail=True))); return 0
    if args.command == "claim":
        if not args.route or not args.taskbook:
            parser.error("claim 需要 路由 任务书名")
        claim(remote, args.route, args.taskbook); return 0
    if args.command == "stop":
        if args.now:
            return stop_now(remote)
        state_dir(remote).mkdir(parents=True, exist_ok=True)
        (state_dir(remote) / "stop").touch(); print("已请求在当前页核完后停止"); return 0
    if args.command == "start":
        return start(repo, workspace)
    Patrol(repo, workspace).watch()
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

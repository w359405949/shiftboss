"""常驻巡检：按项目登记的页面清单挨个核对最近一次发布的提交（页面读已发布的派生物），查到一页红就敲一次门铃。

与常驻验证并行、分工不同：常驻验证只核本批改到的页面，快；巡检不看改动，一遍遍从头扫全部页面，
兜住改动之外受牵连的页面。逐页串行，一次只占一个浏览器。

项目在生成计划（`regen.json`）的 `verification.patrol` 登记两条命令：

    "patrol": {"list": [...], "check": [..., "{route}", ...]}

- `list` 在工作区运行，标准输出某一行是 JSON 对象，其 `pages` 为 `[{"route": ...}, ...]`；
- `check` 对一页运行，`{route}` 换成该页路由、`{index}` 换成序号；退出码非 0 即该页红，输出记进日志。
  输出里带 `route` 与 `seconds` 的 JSON 行是该页的耗时，可另带 `metrics` 对象（如首次可见、交互反应）；
  巡检把它们连同提交与负载记进性能记录，性能线据此挑对象，不另开测量（README 3.2 第 9 条）。

可选 `periodic` 登记定时检查（如构建）：

    "periodic": [{"name": "演示项目构建", "run": [...], "every_minutes": 10, "timeout_minutes": 30}]

- `timeout_minutes` 省略时按单页上限；墙钟随机器负载伸缩的重活（如并行构建）按满载时的耗时登记；
- 巡检在页与页之间、以及给常驻验证让路的等待期间，到点就在独立副本里检出 main 最新提交运行；
- main 自上次运行未前进则跳过；退出码非 0 即红，转红时敲门铃。

用法：
    shiftboss patrol start      # 起后台进程
    shiftboss patrol status     # 当前进度与红页
    shiftboss patrol stop       # 当前页核完后停
    shiftboss patrol claim <路由> <任务书名>   # 红页关联负责修它的线（在办或已归档）
    shiftboss patrol perf       # 各页最近耗时与历次中位数，最慢的在前
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
from shiftboss import verify

NAME = "patrol"
CHECK_TIMEOUT = 900   # 单页上限：页面闸自身有 120 秒会话期限与两次启动重试，超过即视为卡死
AUTOMERGE_INTERVAL = 120   # 自动合入至多每两分钟查一次
YIELD_POLL = 30   # 让路时每隔多少秒再看一次常驻验证的阶段
PUBLISH_LAG = 3600   # main 上未发布的产品改动最早一条超过此秒数即算发布落后，巡检暂停
MIN_FREE_BYTES = 5 * 2**30   # 磁盘剩余低于此值即停：逐页证据会累积，写满会拖垮全部执行方与常驻验证


def interpreter(command: list[str]) -> list[str]:
    """项目命令开头的 `python` 换成本进程的解释器（项目虚拟环境），与常驻验证、生成一致；系统 python 缺项目依赖。"""
    return [sys.executable, *command[1:]] if command and command[0] == "python" else list(command)


def state_dir(remote: Path) -> Path:
    return config.control_dir(remote) / NAME


def now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


class Patrol:
    def __init__(self, repo: Path, workspace: Path):
        self.repo = repo.resolve()
        self.remote = origin(self.repo)
        self.workspace = workspace.resolve()
        self.pass_release = None
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

    def policy(self, workspace: Path | None = None) -> dict:
        plan = json.loads(((workspace or self.workspace) / config.plan_file(self.repo)).read_text(encoding="utf-8"))
        patrol = (plan.get("verification") or {}).get("patrol")
        if not patrol or not patrol.get("list") or not patrol.get("check"):
            raise ValueError(f"{config.plan_file(self.repo)} 的 verification.patrol 缺 list 或 check")
        return patrol

    def checkout_tip(self, workspace: Path | None = None, published: bool = False) -> str:
        """检出 main 最新提交；`published` 时检出最近一次发布：页面读已发布的派生物，
        代码比发布新时新代码要的发布文件不存在，整批页面假红。"""
        workspace = workspace or self.workspace
        git(workspace, "fetch", "--quiet", "origin", "main")
        sha = git(workspace, "rev-parse", "FETCH_HEAD").stdout.strip()
        release = (verify.index_of(self.remote) or {}).get("last_consistent") if published else None
        if release and git(workspace, "merge-base", "--is-ancestor", release, sha, check=False).returncode == 0:
            sha = release
        if published:
            self.pass_release = sha if sha == release else None
        git(workspace, "checkout", "--quiet", "--force", "--detach", sha)
        git(workspace, "clean", "-fdq", "-e", "out/")
        # 本机路径配置不进 git，clean 会删掉它；每遍检出后从项目根重拷。
        name = config.local_config_file(self.repo)
        local = self.repo / name if name else None
        if local and local.is_file():
            shutil.copyfile(local, workspace / local.name)
        return sha

    @property
    def periodic_workspace(self) -> Path:
        return self.workspace.with_name(self.workspace.name + "-periodic")

    def run_automerge(self) -> None:
        """满足条件的交回自动合入（`shiftboss.automerge`），在独立副本里做，每两分钟至多一次。"""
        from shiftboss import automerge
        if time.time() - self.state.get("automerge_tried", 0) < AUTOMERGE_INTERVAL:
            return
        self.state["automerge_tried"] = time.time(); self.save()
        workspace = self.workspace.with_name(self.workspace.name + "-automerge")
        marker = workspace / ".git/shiftboss-patrol-owned"
        if not workspace.exists():
            git(self.repo, "clone", "--quiet", str(self.remote), str(workspace))
            marker.write_text(str(self.remote), encoding="utf-8")
        elif not marker.is_file() or marker.read_text(encoding="utf-8") != str(self.remote):
            raise ValueError("拒绝接管不是巡检创建的自动合入副本")
        name = config.local_config_file(self.repo)
        if name and (self.repo / name).is_file():
            shutil.copyfile(self.repo / name, workspace / name)
        automerge.run(self.remote, workspace, ring)

    def run_periodic(self) -> None:
        """到点的定时检查在独立副本里跑 main 最新提交：巡检一遍要几小时，构建错误不能等到下一遍才发现。"""
        # 自动合入出错只记下并敲铃，不让巡检整体停下。
        try:
            self.run_automerge()
        except Exception as exc:
            self.state["automerge_error"] = str(exc)[:300]; self.save()
            ring(self.remote, f"automerge failed: {str(exc)[:120]}")
        try:
            checks = self.policy().get("periodic") or []
        except (OSError, ValueError):
            return
        results = self.state.setdefault("periodic", {})
        due = [item for item in checks
               if time.time() - (results.get(item["name"]) or {}).get("tried", 0) >= int(item.get("every_minutes", 10)) * 60]
        if not due:
            return
        workspace = self.periodic_workspace
        marker = workspace / ".git/shiftboss-patrol-owned"
        if not workspace.exists():
            git(self.repo, "clone", "--no-checkout", str(self.remote), str(workspace))
            marker.write_text(str(self.remote), encoding="utf-8")
        elif not marker.is_file() or marker.read_text(encoding="utf-8") != str(self.remote):
            raise ValueError("拒绝接管不是巡检创建的定时检查副本")
        sha = self.checkout_tip(workspace)
        # 定时项跑 main 最新，配置也取同一提交：巡检副本检出的是发布提交，可能缺新登记的上限。
        current = {entry["name"]: entry for entry in self.policy(workspace).get("periodic") or []}
        due = [current.get(item["name"], item) for item in due]
        for item in due:
            name, previous = item["name"], results.get(item["name"]) or {}
            if previous.get("commit") == sha:
                results[name] = {**previous, "tried": time.time()}; self.save()
                continue
            log = self.directory / "logs" / f"periodic-{re.sub(r'[\s/\\:]+', '_', name)}.log"
            env = os.environ.copy(); env["PYTHONIOENCODING"] = "utf-8"
            started, limit = time.time(), int(item.get("timeout_minutes", 0)) * 60 or CHECK_TIMEOUT
            with log.open("w", encoding="utf-8") as handle:
                handle.write(f"{now()} {name} @ {sha}\n$ {subprocess.list2cmdline(item['run'])}\n")
                handle.flush()
                try:
                    code = subprocess.run(interpreter(item["run"]), cwd=workspace, stdout=handle, stderr=subprocess.STDOUT,
                                          env=env, timeout=limit).returncode
                except subprocess.TimeoutExpired:
                    handle.write(f"\n超过 {limit} 秒，按卡死记红\n")
                    code = 124
            red = code != 0
            first = previous.get("first") if red and previous.get("code") else (now() if red else None)
            results[name] = {"commit": sha, "at": now(), "tried": time.time(), "code": code,
                             "seconds": round(time.time() - started), "log": str(log), "first": first}
            self.save()
            if red and not previous.get("code"):
                ring(self.remote, f"patrol periodic red {name} @ {sha[:12]}; run shiftboss patrol status")

    def routes(self, command: list[str]) -> list[str]:
        p = subprocess.run(interpreter(command), cwd=self.workspace, capture_output=True, text=True, encoding="utf-8")
        for line in p.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                data = json.loads(line)
                if isinstance(data.get("pages"), list):
                    return [page["route"] for page in data["pages"]]
        raise ValueError(f"巡检清单命令没有输出 pages：{p.stderr.strip()[-500:]}")

    def check(self, command: list[str], route: str, index: int) -> tuple[int, Path]:
        log = self.directory / "logs" / f"{self.state['pass']:04d}-{index:03d}.log"
        argv = interpreter([part.replace("{route}", route).replace("{index}", str(index)) for part in command])
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

    def record_perf(self, route: str, log: Path, sha: str) -> None:
        """从该页日志取耗时与指标，追加进性能记录；页面没给出耗时就不记。"""
        result = None
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("{") and '"route"' in line and '"seconds"' in line:
                result = json.loads(line)
        if not result:
            return
        entry = {"at": now(), "pass": self.state["pass"], "route": route, "commit": sha,
                 "load": round(os.getloadavg()[0], 1), "seconds": result.get("seconds"),
                 "ok": not result.get("returncode")}
        if isinstance(result.get("metrics"), dict):
            entry["metrics"] = result["metrics"]
        with perf_path(self.remote).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def record(self, route: str, code: int, log: Path, sha: str) -> None:
        self.record_perf(route, log, sha)
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

    def verify_browsing(self) -> bool:
        """常驻验证正在跑页面阶段：两边同时开浏览器会把机器压满，页面闸大面积超时成假红。"""
        import psutil
        index = verify.index_of(self.remote) or {}
        stage = str((index.get("active") or {}).get("stage") or "")
        pid = index.get("pid")
        return stage.startswith("pages") and isinstance(pid, int) and psutil.pid_exists(pid)

    def publication_lag(self) -> bool:
        """发布落后：已合入 main 的产品改动迟迟未发布。页面读已发布的派生物，此时整遍只会重报已修的红，
        巡检暂停到发布追上；合入照常（用户裁定 D28）。"""
        published = (verify.index_of(self.remote) or {}).get("last_consistent")
        if not published:
            return False
        times = git(self.remote, "log", "--first-parent", "--format=%ct", f"{published}..main", "--", "engine/",
                    check=False).stdout.split()
        return bool(times) and time.time() - int(times[-1]) > PUBLISH_LAG

    def wait_reason(self) -> str | None:
        return "常驻验证在跑页面" if self.verify_browsing() else "发布落后" if self.publication_lag() else None

    def yield_to_verify(self, stop: Path) -> None:
        if not self.wait_reason():
            return
        self.state["yielding"] = now(); self.save()
        while (reason := self.wait_reason()) and not stop.exists():
            self.state["yield_reason"] = reason; self.save()
            self.run_periodic()
            time.sleep(YIELD_POLL)
        self.state.pop("yielding", None); self.state.pop("yield_reason", None); self.save()

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
                sha = self.checkout_tip(published=True)
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
                    self.run_periodic()
                    self.yield_to_verify(stop)
                    if stop.exists():
                        break
                    if self.pass_release and (verify.index_of(self.remote) or {}).get("last_consistent") != self.pass_release:
                        break   # 发布已前进：从头扫新发布，已扫的旧发布结果不再有意义
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


def perf_path(remote: Path) -> Path:
    return state_dir(remote) / "perf.jsonl"


PERF_HISTORY = 5   # 每页取最近几次通过的耗时算中位数
PERF_SHOWN = 3   # 状态摘要列出最慢的几页


def perf_rows(remote: Path) -> list[dict]:
    """每页最近一次耗时、最近 PERF_HISTORY 次通过的中位数与当时负载，按中位数从慢到快。"""
    import statistics
    path = perf_path(remote)
    if not path.exists():
        return []
    history: dict[str, list[dict]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entry = json.loads(line)
            history.setdefault(entry["route"], []).append(entry)
    rows = []
    for route, entries in history.items():
        passed = [e for e in entries if e.get("ok") and isinstance(e.get("seconds"), (int, float))][-PERF_HISTORY:]
        if not passed:
            continue
        last = entries[-1]
        rows.append({"route": route, "median": statistics.median(e["seconds"] for e in passed), "samples": len(passed),
                     "last": last.get("seconds"), "load": last.get("load"), "commit": last.get("commit", "")[:12],
                     "metrics": last.get("metrics") or {}})
    return sorted(rows, key=lambda row: -row["median"])


def perf_lines(remote: Path, limit: int | None = None) -> list[str]:
    rows = perf_rows(remote)[:limit]
    return [f"- {row['route'].removeprefix('/?view=')}｜中位 {row['median']:.1f} 秒（{row['samples']} 次）｜最近 {row['last']} 秒"
            f"｜负载 {row['load'] if row['load'] is not None else '—'}" + "".join(f"｜{key} {round(value) if isinstance(value, float) else value}" for key, value in row["metrics"].items())
            for row in rows]


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
    """→ (状态词, 负责线)。只有「无人负责」与「修复无效」要开线；「待发布」等常驻验证发布。"""
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
    if not scanned:
        return "待复扫", taskbook
    # 页面读取已发布的派生物：修复已在所扫提交里、但最近一次发布还不含它时，修复尚未生效，不算无效；
    # 发布已含修复、但所扫提交早于这次发布时，等下一遍复扫。
    published = (verify.index_of(remote) or {}).get("last_consistent")
    ancestor = lambda older, newer: git(remote, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0
    if not published or not ancestor(info["merge"], published):
        return "待发布", taskbook
    return ("修复无效" if ancestor(published, item["commit"]) else "待复扫"), taskbook


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
    running = (f"让路（{state.get('yield_reason') or '常驻验证在跑页面'}）" if state.get("yielding") else "在跑") if alive else "没在跑"
    head = (f"- 进度｜{running}｜第 {state.get('pass', 0)} 遍 "
            f"{state.get('position', 0)}/{state.get('total', '?')}｜main@{(state.get('commit') or '')[:12]}")
    lines = [head]
    if state.get("error"):
        lines.append(f"- 出错｜{state['error'][:120]}")
    for name, item in (state.get("periodic") or {}).items():
        if "code" not in item:
            continue
        word = f"红（首见 {item['first'][5:16].replace('T', ' ')}）" if item["code"] else "绿"
        lines.append(f"- {name}｜{word}｜main@{item['commit'][:12]}｜{item['at'][11:16]}｜{item.get('seconds', '?')} 秒"
                     + (f"｜{item['log']}" if item["code"] else ""))
    claims = read_state(directory / "claims.json") or {}
    groups: dict[tuple[str, str], list[str]] = {}
    reds = sorted(state.get("red", {}).items(), key=lambda pair: pair[1]["first"])
    for route, item in reds:
        groups.setdefault(red_state(remote, item, claims.get(route)), []).append(route)
    # 摘要只列前几条路由，全量见 `shiftboss patrol status`。
    shown = 5
    for (word, taskbook), routes in groups.items():
        names = [route.removeprefix("/?view=") for route in routes]
        lines.append(f"- 红 {len(routes)}｜{word}" + (f"｜{taskbook}" if taskbook else "") + "｜" + "、".join(names[:shown])
                     + (f" 等 {len(names)} 条" if len(names) > shown else ""))
    slowest = perf_lines(remote, PERF_SHOWN)
    if slowest:
        lines.append(f"- 最慢 {len(slowest)} 页（全部见 `shiftboss patrol perf`）")
        lines += ["  " + line.removeprefix("- ") for line in slowest]
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
    parser.add_argument("command", choices=["watch", "start", "status", "stop", "claim", "perf"])
    parser.add_argument("route", nargs="?", help="claim：红页路由，照 `status` 所列")
    parser.add_argument("taskbook", nargs="?", help="claim：负责修它的任务书名")
    parser.add_argument("--repo", type=Path, default=None)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--now", action="store_true", help="stop：不等当前页核完，立即结束整棵进程树")
    args = parser.parse_args(argv)
    repo = (args.repo or project_root()).resolve()
    remote = origin(repo)
    workspace = args.workspace or config.copies_dir(remote) / NAME
    if args.command == "perf":
        print("\n".join(perf_lines(remote)) or "尚无性能记录"); return 0
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

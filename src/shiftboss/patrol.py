"""常驻巡检：常驻验证的两条浏览器支线（用户定 D53：验证与巡检是一个调度下并行的三条支线）。

- 工具闸支线（A）：`shiftboss.verify`，按提交生成、发布、跑测试与闸，不开浏览器；
- 页面支线（B，本模块 `watch`）：按项目登记的页面清单挨个核对最近一次发布的提交（页面读已发布的派生物），
  发布前进时先核本批改到的页面，再接着从头扫全部页面；逐页串行，一次只占一个浏览器；
- 定时支线（C，本模块 `periodic`）：定时重活（如构建与 ROM 冒烟）与自动合入，独立进程、独立副本。

三条各自推进、各自记红，互不让路。查到一页红就敲一次门铃。

项目在生成计划（`regen.json`）的 `verification.patrol` 登记两条命令：

    "patrol": {"list": [...], "check": [..., "{route}", ...]}

- `list` 在工作区运行，标准输出某一行是 JSON 对象，其 `pages` 为 `[{"route": ...}, ...]`；
- `check` 对一页运行，`{route}` 换成该页路由、`{index}` 换成序号；退出码非 0 即该页红，输出记进日志。
  输出里带 `route` 与 `seconds` 的 JSON 行是该页的耗时，可另带 `metrics` 对象（如首次可见、交互反应）；
  巡检把它们连同提交与负载记进性能记录，性能线据此挑对象，不另开测量（README 3.2 第 9 条）。

可选 `periodic` 登记定时检查（如构建）：

    "periodic": [{"name": "演示项目构建", "run": [...], "every_minutes": 10, "timeout_minutes": 30}]

- 不设总时长，连续 `IDLE_KILL` 秒没有输出才判卡死（用户定 D54：以输出是否持续区分慢与卡死）；
- 定时支线到点就在独立副本里检出 main 最新提交运行；
- main 自上次运行未前进则跳过；退出码非 0 即红，转红时敲门铃。

用法：
    shiftboss patrol start      # 起页面与定时两条支线（`shiftboss verify start` 会一并拉起）
    shiftboss patrol status     # 当前进度与红页
    shiftboss patrol stop       # 当前页核完后停
    shiftboss patrol claim <路由> <任务书名>   # 红页关联负责修它的线（在办或已归档）
    shiftboss patrol claim --reason <原因> <任务书名>   # 失败原因只有这一种的红页都归该线，直到它归档
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
IDLE_KILL = 10 * 60   # 单页或定时项连续无输出多久判卡死；不设总时长（D54）
AUTOMERGE_INTERVAL = 120   # 自动合入至多每两分钟查一次
YIELD_POLL = 30   # 发布落后时每隔多少秒再看一次；定时支线的轮询间隔
PUBLISH_LAG = 3600   # main 上未发布的产品改动最早一条超过此秒数即算发布落后，巡检暂停
MIN_FREE_BYTES = 5 * 2**30   # 磁盘剩余低于此值即停：逐页证据会累积，写满会拖垮全部执行方与常驻验证


def interpreter(command: list[str]) -> list[str]:
    """项目命令开头的 `python` 换成本进程的解释器（项目虚拟环境），与常驻验证、生成一致；系统 python 缺项目依赖。"""
    return [sys.executable, *command[1:]] if command and command[0] == "python" else list(command)


def state_dir(remote: Path) -> Path:
    return config.control_dir(remote) / NAME


def now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def run_until_idle(argv: list[str], cwd: Path, handle, env: dict) -> int:
    """跑到结束，输出写进 `handle`；连续 IDLE_KILL 秒没有新输出才结束整个进程组并返回 124。"""
    process = subprocess.Popen(argv, cwd=cwd, stdout=handle, stderr=subprocess.STDOUT, env=env, start_new_session=True)
    last_size, last_change = -1, time.monotonic()
    while True:
        try:
            return process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            handle.flush()
            size = os.fstat(handle.fileno()).st_size
            if size != last_size:
                last_size, last_change = size, time.monotonic()
            elif time.monotonic() - last_change > IDLE_KILL:
                os.killpg(process.pid, 9)
                process.wait()
                handle.write(f"\n连续 {IDLE_KILL // 60} 分钟没有新输出，判为卡死\n")
                return 124


class Patrol:
    def __init__(self, repo: Path, workspace: Path, state_file: str = "state.json"):
        self.repo = repo.resolve()
        self.remote = origin(self.repo)
        self.workspace = workspace.resolve()
        self.pass_release = None
        if self.workspace in (self.repo, self.remote):
            raise ValueError("巡检副本必须独立")
        self.directory = state_dir(self.remote)
        (self.directory / "logs").mkdir(parents=True, exist_ok=True)
        # 页面支线与定时支线是两个进程，各写各的状态文件，免得整份覆盖吃掉对方的字段。
        self.state_file = self.directory / state_file
        self.state = read_state(self.state_file) or {"pass": 0, "red": {}}

    def save(self) -> None:
        write_state(self.state_file, self.state)

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
            started = time.time()
            with log.open("w", encoding="utf-8") as handle:
                handle.write(f"{now()} {name} @ {sha}\n$ {subprocess.list2cmdline(item['run'])}\n")
                handle.flush()
                code = run_until_idle(interpreter(item["run"]), workspace, handle, env)
            red = code != 0
            first = previous.get("first") if red and previous.get("code") else (now() if red else None)
            results[name] = {"commit": sha, "at": now(), "tried": time.time(), "code": code,
                             "seconds": round(time.time() - started), "log": str(log), "first": first}
            self.save()
            if red and not previous.get("code"):
                ring(self.remote, f"patrol periodic red {name} @ {sha[:12]}; run shiftboss patrol status")

    def routes(self, command: list[str], base: str | None = None, head: str | None = None) -> list[str]:
        """全部页面；给出 `base`／`head` 时去掉 `--all`，按本批范围只列改到的页面（项目的清单命令据环境变量选页）。"""
        env = os.environ.copy()
        if base:
            command = [part for part in command if part != "--all"]
            env.update(SHIFTBOSS_VERIFY_BASE=base, SHIFTBOSS_VERIFY_HEAD=head or "HEAD")
        p = subprocess.run(interpreter(command), cwd=self.workspace, capture_output=True, text=True, encoding="utf-8",
                           env=env)
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
            code = run_until_idle(argv, self.workspace, handle, env)
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
        kinds = reason_kinds(log)
        red[route] = {"first": known["first"] if known else now(), "last": now(), "commit": sha,
                      "log": str(log), "code": code, "kinds": kinds}
        self.save()
        # 按原因认领规则命中的新红已有负责线，不敲门铃。
        if not known and not rule_taskbook(self.remote, kinds):
            ring(self.remote, f"patrol red {route}; run shiftboss patrol status")

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
        return "发布落后" if self.publication_lag() else None

    def yield_to_verify(self, stop: Path) -> None:
        if not self.wait_reason():
            return
        self.state["yielding"] = now(); self.save()
        while (reason := self.wait_reason()) and not stop.exists():
            self.state["yield_reason"] = reason; self.save()
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
                previous = self.state.get("swept_release")
                sha = self.checkout_tip(published=True)
                policy = self.policy()
                routes = self.routes(policy["list"])
                # 发布前进：本批改到的页面排在最前，改动的牵连最先报出，其余照常从头扫。
                affected = []
                if previous and self.pass_release and previous != self.pass_release:
                    affected = self.routes(policy["list"], previous, self.pass_release)
                # 红页紧随其后：修复合入或偶发故障后尽早复扫消红。
                reds = [r for r in self.state.get("red", {}) if r in routes]
                routes = [*dict.fromkeys([*[r for r in affected if r in routes], *reds, *routes])]
                if self.pass_release:
                    self.state["swept_release"] = self.pass_release
                self.state["pass"] += 1
                self.state.update(commit=sha, total=len(routes), position=0, started=now(), error=None)
                self.save()
                for index, route in enumerate(routes):
                    if stop.exists():
                        break
                    free = shutil.disk_usage(self.workspace).free
                    if free < MIN_FREE_BYTES:
                        raise RuntimeError(f"磁盘剩余 {free / 2**30:.1f} GiB，低于 {MIN_FREE_BYTES / 2**30:.0f} GiB，巡检停下")
                    self.yield_to_verify(stop)
                    if stop.exists():
                        break
                    if self.pass_release and (verify.index_of(self.remote) or {}).get("last_consistent") != self.pass_release:
                        break   # 发布已前进：从头扫新发布，已扫的旧发布结果不再有意义
                    self.state.update(position=index + 1, route=route); self.save()
                    code, log = self.check(policy["check"], route, index)
                    # 读不出任何页面失败原因的失败（浏览器连接断开等）按偶发故障当场重试一次。
                    if code and not reason_kinds(log):
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


def periodic_loop(patrol: "Patrol") -> None:
    """定时支线：定时重活与自动合入，独立于页面支线，构建再久也不挡逐页检查。"""
    lock = patrol.directory / "periodic.lock"
    try:
        handle = lock.open("x", encoding="utf-8")
    except FileExistsError:
        raise ValueError(f"已有定时支线进程锁 {lock}；确认其 PID 已结束后才能删除")
    with handle:
        handle.write(str(os.getpid()))
    stop = patrol.directory / "periodic-stop"
    stop.unlink(missing_ok=True)
    try:
        while not stop.exists():
            patrol.run_periodic()
            time.sleep(YIELD_POLL)
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


def reason_kinds(log: Path) -> list[str]:
    """页面闸日志里本页失败原因的种类（「模板几何」「布局指纹」等冒号前的名字）；读不出返回空表。"""
    try:
        lines = Path(log).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    kinds: set[str] = set()
    for line in lines:
        if '"reasons"' not in line:
            continue
        try:
            result = json.loads(line)
        except json.JSONDecodeError:
            continue
        # 带 attempts 的是逐次尝试的详细记录，其 reasons 混入启动日志；只认页面结论行。
        if not isinstance(result, dict) or "attempts" in result:
            continue
        reasons = result.get("reasons") or []
        kinds.update(str(reason).split(":", 1)[0].strip() for reason in reasons)
    return sorted(kinds)


def claim_rules(remote: Path) -> dict[str, dict]:
    return read_state(state_dir(remote) / "claim-rules.json") or {}


def rule_taskbook(remote: Path, kinds: list[str], first: str | None = None) -> str:
    """失败原因只有一种且登记了按原因认领时返回负责线。负责线归档后规则只管首见早于归档合并的红
    （照常走待发布／待复扫／修复无效），之后新出的红不再认领；不给 `first` 即只问规则是否仍在生效。"""
    if len(kinds) != 1:
        return ""
    rule = claim_rules(remote).get(kinds[0])
    if not rule:
        return ""
    info = owner(remote, rule["taskbook"])
    if not info:
        return ""
    if not info["archived"]:
        return rule["taskbook"]
    if first is None or not info["merge"]:
        return ""
    merged_at = git(remote, "show", "-s", "--format=%cI", info["merge"], check=False).stdout.strip()
    return rule["taskbook"] if merged_at and first < merged_at[:19] else ""


def claim_reason(remote: Path, reason: str, taskbook: str) -> None:
    """按原因认领：此后失败原因只有 `reason` 一种的红页都归 `taskbook`，直到它归档。"""
    if Path(taskbook).name != taskbook:
        raise ValueError("任务书给文件名，不带目录或扩展名")
    info = owner(remote, taskbook)
    if info is None or info["archived"]:
        raise ValueError("认领拒绝：main 上没有这份在办任务书")
    # 已归档线的规则保留：它仍管归档前认领的红，直到这些红消掉或换线认领。
    rules = claim_rules(remote)
    rules[reason] = {"taskbook": taskbook, "claimed": now()}
    write_state(state_dir(remote) / "claim-rules.json", rules)
    print(f"已按原因认领「{reason}」→ {taskbook}（该线归档后失效）")


def red_state(remote: Path, item: dict, claimed: dict | None) -> tuple[str, str]:
    """→ (状态词, 负责线)。只有「无人负责」与「修复无效」要开线；「待发布」等常驻验证发布。"""
    if not claimed or claimed.get("first") != item["first"]:
        taskbook = rule_taskbook(remote, item.get("kinds") or reason_kinds(item["log"]), item["first"])
        if not taskbook:
            return "无人负责", ""
        claimed = {"taskbook": taskbook, "first": item["first"]}
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


def page_lines(remote: Path, detail: bool = False) -> list[str]:
    """页面支线：进度一行；红页按（状态，负责线）合并成行。`detail` 再逐页列首见与日志（`status` 用）。"""
    directory = state_dir(remote)
    state = read_state(directory / "state.json")
    alive = lane_pid(remote, "worker.lock")
    if not state:
        return [f"- 页面｜{'在跑' if alive else '没在跑'}｜尚无记录"]
    running = (f"暂停（{state.get('yield_reason') or '发布落后'}）" if state.get("yielding") else "在跑") if alive else "没在跑"
    lines = [f"- 页面｜{running}｜第 {state.get('pass', 0)} 遍 "
             f"{state.get('position', 0)}/{state.get('total', '?')}｜main@{(state.get('commit') or '')[:12]}"]
    if state.get("error"):
        lines.append(f"- 页面出错｜{state['error'][:120]}")
    claims = read_state(directory / "claims.json") or {}
    groups: dict[tuple[str, str], list[str]] = {}
    reds = sorted(state.get("red", {}).items(), key=lambda pair: pair[1]["first"])
    for route, item in reds:
        groups.setdefault(red_state(remote, item, claims.get(route)), []).append(route)
    # 摘要只列前几条路由，全量见 `shiftboss patrol status`。
    shown = 5
    for (word, taskbook), routes in groups.items():
        names = [route.removeprefix("/?view=") for route in routes]
        lines.append(f"- 页面红 {len(routes)}｜{word}" + (f"｜{taskbook}" if taskbook else "") + "｜" + "、".join(names[:shown])
                     + (f" 等 {len(names)} 条" if len(names) > shown else ""))
    if detail:
        for route, item in reds:
            lines.append(f"  {route}｜首见 {item['first'][5:16].replace('T', ' ')}｜{item['log']}")
    return lines


def periodic_lines(remote: Path) -> list[str]:
    """定时支线：进程是否在跑，各定时项最近一次的结果。"""
    periodic = read_state(state_dir(remote) / "periodic.json") or {}
    lines = [f"- 定时｜{'在跑' if lane_pid(remote, 'periodic.lock') else '没在跑'}"]
    for name, item in (periodic.get("periodic") or {}).items():
        if "code" not in item:
            continue
        word = f"红（首见 {item['first'][5:16].replace('T', ' ')}）" if item["code"] else "绿"
        lines.append(f"- {name}｜{word}｜main@{item['commit'][:12]}｜{item['at'][11:16]}｜{item.get('seconds', '?')} 秒"
                     + (f"｜{item['log']}" if item["code"] else ""))
    return lines


def slowest_lines(remote: Path) -> list[str]:
    slowest = perf_lines(remote, PERF_SHOWN)
    return [f"- 最慢 {len(slowest)} 页（全部见 `shiftboss patrol perf`）", *("  " + line.removeprefix("- ") for line in slowest)] if slowest else []


def status_lines(remote: Path, detail: bool = False) -> list[str]:
    return [*page_lines(remote, detail), *periodic_lines(remote), *slowest_lines(remote)]


LANES = (("worker.lock", "页面支线", "watch"), ("periodic.lock", "定时支线", "periodic"))


def lane_pid(remote: Path, lock_name: str) -> int | None:
    import psutil
    lock = state_dir(remote) / lock_name
    pid = lock.read_text(encoding="utf-8").strip() if lock.exists() else ""
    return int(pid) if pid.isdigit() and psutil.pid_exists(int(pid)) else None


def stop_now(remote: Path, workspace: Path) -> int:
    import psutil
    for lock_name, label, _ in LANES:
        if pid := lane_pid(remote, lock_name):
            root = psutil.Process(pid)
            tree = root.children(recursive=True) + [root]
            for proc in tree:
                try:
                    proc.kill()
                except psutil.NoSuchProcess:
                    pass
            psutil.wait_procs(tree, timeout=10)
            print(f"已结束{label} PID {pid} 及其 {len(tree) - 1} 个子进程")
        else:
            print(f"{label}没在跑")
        (state_dir(remote) / lock_name).unlink(missing_ok=True)
    # 进程树已杀尽，副本里残留的 git 锁必是被打断的检出留下的；不清掉下次拉起即报错退出。
    for copy in (workspace, *(workspace.with_name(workspace.name + suffix) for suffix in ("-periodic", "-automerge"))):
        (copy / ".git" / "index.lock").unlink(missing_ok=True)
    return 0


def start(repo: Path, workspace: Path) -> int:
    """拉起页面与定时两条支线；已在跑的照旧，只起没在跑的。"""
    from shiftboss import native
    remote = origin(repo)
    for lock_name, label, command in LANES:
        if pid := lane_pid(remote, lock_name):
            print(f"{label}已在跑：PID {pid}")
            continue
        (state_dir(remote) / lock_name).unlink(missing_ok=True)
        argv = [sys.executable, "-B", "-m", "shiftboss.patrol", command, "--repo", str(repo),
                "--workspace", str(workspace.resolve())]
        print(f"{label}已起：PID {native.spawn(argv, str(repo))}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["watch", "periodic", "start", "status", "stop", "claim", "perf"])
    parser.add_argument("route", nargs="?", help="claim：红页路由，照 `status` 所列")
    parser.add_argument("taskbook", nargs="?", help="claim：负责修它的任务书名")
    parser.add_argument("--repo", type=Path, default=None)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--now", action="store_true", help="stop：不等当前页核完，立即结束整棵进程树")
    parser.add_argument("--reason", help="claim：按失败原因认领（如「模板几何」），此时只给任务书名")
    args = parser.parse_args(argv)
    repo = (args.repo or project_root()).resolve()
    remote = origin(repo)
    workspace = args.workspace or config.copies_dir(remote) / NAME
    if args.command == "perf":
        print("\n".join(perf_lines(remote)) or "尚无性能记录"); return 0
    if args.command == "status":
        print("\n".join(status_lines(remote, detail=True))); return 0
    if args.command == "claim" and args.reason:
        if not args.route or args.taskbook:
            parser.error("claim --reason 原因 只需要 任务书名")
        claim_reason(remote, args.reason, args.route); return 0
    if args.command == "claim":
        if not args.route or not args.taskbook:
            parser.error("claim 需要 路由 任务书名")
        claim(remote, args.route, args.taskbook); return 0
    if args.command == "stop":
        if args.now:
            return stop_now(remote, workspace)
        state_dir(remote).mkdir(parents=True, exist_ok=True)
        (state_dir(remote) / "stop").touch(); (state_dir(remote) / "periodic-stop").touch()
        print("已请求页面支线在当前页核完后停、定时支线在当前项跑完后停"); return 0
    if args.command == "start":
        return start(repo, workspace)
    if args.command == "periodic":
        periodic_loop(Patrol(repo, workspace, "periodic.json"))
        return 0
    Patrol(repo, workspace).watch()
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

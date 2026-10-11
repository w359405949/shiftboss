"""Producer declarations and independent observation of generated files.

Producers declare exact paths before publishing them. Python's audit events
observe writes independently, including atomic replacement via temporary files.
The report is emitted only after the two sets agree; observations never enlarge
the declaration. No directory or extension is an ownership rule.
"""

from __future__ import annotations
from shiftboss.paths import project_root

import functools
import json
import os
import sys
from pathlib import Path, PurePosixPath
from typing import Iterable

REPO = project_root()
from shiftboss import config

CATALOG = config.catalog_file()  # 只给短命进程用；常驻验证等长进程每次现读，配置改了不留旧值
SCHEMA = "workflow.producer-outputs.v1"
REPORT_ENV = "WORKFLOW_OUTPUT_REPORT"
_active = None
_hook_installed = False


def exact_paths(values: object) -> list[str]:
    if not isinstance(values, list):
        raise ValueError("产物声明必须是精确文件路径列表")
    for name in values:
        if (not isinstance(name, str) or not name
                or PurePosixPath(name).is_absolute()
                or PurePosixPath(name).as_posix() != name
                or any(part in (".", "..", ".git") for part in name.split("/"))
                or any(char in name for char in "\\:*?[]\n\r\0")):
            raise ValueError(f"产物声明只能写仓库内的精确文件路径：{name!r}")
    if len(values) != len(set(values)):
        raise ValueError("产物声明有重复文件")
    return sorted(values)


def _relative(path: object) -> str | None:
    if isinstance(path, int):  # open(fd), not a new filesystem destination
        return None
    candidate = Path(os.fsdecode(path))
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    # Do not resolve symlinks into an apparently acceptable declaration.
    candidate = Path(os.path.abspath(candidate))
    try:
        name = candidate.relative_to(REPO).as_posix()
    except ValueError:
        raise ValueError(f"生成器写入仓库之外：{candidate}") from None
    if candidate.resolve() != candidate:
        raise ValueError(f"产物路径含符号链接：{name}")
    return name


def declare_outputs(paths: Iterable[Path]) -> None:
    """Declare destinations from the producer's own publication plan."""
    if _active is not None:
        names = [_relative(path) for path in paths]
        _active.declared.update(exact_paths(names))


def retain_output(path: Path, expected_text: str) -> None:
    """Report the producer's existing, content-equal write-skip branch."""
    if _active is not None:
        name = _relative(path)
        if not path.is_file():
            raise ValueError(f"原样产物不存在：{name}")
        if path.read_text(encoding="utf-8") != expected_text:
            raise ValueError(f"原样产物与生产端内容不同：{name}")
        _active.retained.add(name)


def _audit(event: str, args: tuple) -> None:
    if _active is None:
        return
    if event in ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn"):
        raise ValueError("生成器启动了未接入产物记录的子进程；停止，不发布产物报告")
    if event == "open":
        path, _mode, flags = args
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            name = _relative(path)
            # NamedTemporaryFile passes its directory to io.open with a custom
            # opener. That emits a nominal directory event followed by the
            # actual temporary file's os.open event; only the latter is a file.
            if name is not None and not (REPO / name).is_dir():
                _active.written.add(name)
    elif event == "os.rename":
        source, target, source_fd, target_fd = args
        if source_fd != -1 or target_fd != -1:
            raise ValueError("产物记录不支持 dir_fd 相对重命名")
        source_name, target_name = _relative(source), _relative(target)
        _active.written.discard(source_name)
        _active.written.add(target_name)
    elif event == "os.remove":
        path, dir_fd = args
        if dir_fd != -1:
            raise ValueError("产物记录不支持 dir_fd 相对删除")
        # Removed temporary files and retired outputs are not publications.
        _active.written.discard(_relative(path))


class OutputRun:
    def __init__(self) -> None:
        self.declared: set[str] = set()
        self.written: set[str] = set()
        self.retained: set[str] = set()

    def report(self) -> dict:
        """声明不符只报告、不补名单；

        生产端声明与实际写入的核对仍由 `shiftboss.outputs` 在后台生成时完成，不在合并时重跑。
        """
        actual = self.written | self.retained
        problems = [
            *(f"多写了没声明的：{name}" for name in sorted(actual - self.declared)),
            *(f"声明了没写的：{name}" for name in sorted(self.declared - actual)),
            *(f"产物不是普通文件：{name}" for name in sorted(self.declared)
              if not (REPO / name).is_file() or (REPO / name).is_symlink()),
        ]
        if problems:
            raise ValueError("产物声明与实际不符；仅报告，不自动扩展：\n"
                             + "\n".join(f"  {line}" for line in problems))
        return {"schema": SCHEMA, "files": sorted(self.declared),
                "written": sorted(self.written),
                "retained": sorted(self.retained - self.written)}


def producer_main(main):
    """Enable reporting when the workflow requests it, without changing output.

    生产端声明产物、独立观察写入并核对
    """
    @functools.wraps(main)
    def wrapped(*args, **kwargs):
        global _active, _hook_installed
        report_path = os.environ.get(REPORT_ENV)
        if not report_path:
            return main(*args, **kwargs)
        if _active is not None:
            raise ValueError("产物记录不能嵌套启动")
        if not _hook_installed:
            sys.addaudithook(_audit)
            _hook_installed = True
        run = OutputRun()
        _active = run
        try:
            result = main(*args, **kwargs)
        finally:
            _active = None
        if result == 0:
            report = run.report()
            path = Path(report_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8", newline="\n")
            print(f"PRODUCER_OUTPUTS_OK：声明 {len(report['files'])}，"
                  f"实写 {len(report['written'])}，原样 {len(report['retained'])}", flush=True)
        return result
    return wrapped


def catalog_document(producers: list[dict]) -> dict:
    # This catalog is itself produced here, not an extra manual allowlist.
    """`derived-artifacts.json` 是生成报告，不可手填
    """
    return {"schema": SCHEMA, "publisher": [config.catalog_file()], "producers": producers}


def plan_commands(plan: dict) -> list[list[str]]:
    """产物报告应有的生产端命令，按计划顺序：`stages` 的命令或旧格式 `steps`。"""
    stages = plan.get("stages")
    return [stage["command"] for stage in stages] if stages is not None else list(plan.get("steps") or [])


# 一个产物只归一个阶段：阶段若写了别的阶段的产物，就是把那份活塞进了自己，拒绝。
def merge_catalog(old: dict | None, stages: list[dict], producers: list[dict]) -> dict:
    """本次重跑阶段的报告换掉旧条目，未重跑的沿用；按计划顺序排列。"""
    previous = {p.get("stage"): p for p in (old or {}).get("producers") or [] if p.get("stage")}
    fresh = {p["stage"]: p for p in producers}
    merged = []
    for stage in stages:
        entry = fresh.get(stage["name"]) or previous.get(stage["name"])
        if entry is None or entry["command"] != stage["command"]:
            raise ValueError(f"阶段 {stage['name']} 没有与当前命令相符的产物报告；须重跑该阶段")
        merged.append(entry)
    owners: dict[str, str] = {}
    for entry in merged:
        for name in exact_paths(entry["files"]):
            if name in owners:
                raise ValueError(f"产物 {name} 同时归属阶段 {owners[name]} 与 {entry['stage']}")
            owners[name] = entry["stage"]
    return catalog_document(merged)


def catalog_files(catalog: dict, commands: list[list[str]]) -> list[str]:
    if catalog.get("schema") != SCHEMA or catalog.get("publisher") != [config.catalog_file()]:
        raise ValueError("缺少生产端产物报告；先运行 shiftboss regen --steps")
    producers = catalog.get("producers")
    if not isinstance(producers, list) or [p["command"] for p in producers] != commands:
        raise ValueError(f"生产端产物报告与 {config.plan_file()} 的 steps 不符；请重新生成")
    files = set(exact_paths(catalog["publisher"]))
    for producer in producers:
        files.update(exact_paths(producer["files"]))
    return sorted(files)


def commit_generated(workspace: Path, source: str, old_files: set[str],
                     steps: list[list[str]]) -> str | None:
    """把生成后的派生物改动提交在 `source` 之上，返回生成提交；与源一致时返回 None。

    改动只许落在生成前后两份产物清单之内，否则拒绝。常驻验证与合并时生成共用这一处，
    「哪些文件算派生物」只有一种认法。`old_files` 须在生成覆盖清单之前读取。
    """
    import subprocess

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=workspace, capture_output=True, text=True,
                              encoding="utf-8", check=True)

    catalog = json.loads((workspace / config.catalog_file(workspace)).read_text(encoding="utf-8"))
    allowed = set(old_files) | set(catalog_files(catalog, steps))
    changed = set(run("diff", "--name-only", "-z", source).stdout.split("\0"))
    changed.update(run("ls-files", "--others", "--exclude-standard", "-z").stdout.split("\0"))
    changed.discard("")
    if changed - allowed:
        raise ValueError("生成改了非产物文件：" + ", ".join(sorted(changed - allowed)))
    if not changed:
        return None
    run("add", "--all")
    run("commit", "-qm", f"Generate artifacts for {source}\n\nWorkflow-Generated-From: {source}")
    return run("rev-parse", "HEAD").stdout.strip()

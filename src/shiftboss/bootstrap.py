#!/usr/bin/env python3
"""把这套工作流在一台新机器（或一个新项目）上搭起来。

**可以反复跑**：只初始化新副本，已有副本只读核验，缺项就报告。

    shiftboss bootstrap --copies 7    # 同时设置 Codex 配额
    shiftboss bootstrap --executor agy --copies 2
    shiftboss bootstrap               # 按已存配额补齐
    shiftboss bootstrap --check       # 只报告差在哪，不动手

做五件事，**每一件都是踩过才写进来的**：

1. **建 bare origin**（`<仓库>.shiftboss/hub.git`）并把当前仓库的 `origin` 指过去。
   bare 只做中转——多个执行方各自 push 自己那条分支，不互相看见工作树。
2. **每个执行方克隆 N 个工作副本**（放在副本目录下：`codex1`、`codex2` …；agy、omp 同理）。
   **并行的单位是工作副本，不是分支**：一个工作树同时只能 checkout 一条分支，
   两个执行方在同一个目录里，谁切分支都会带走或撞掉对方的未提交改动。
3. **装门铃 hook**（`post-receive`）。hook 不进 git，所以装它必须是代码。
   同时把包内通用文档装进项目的 `shiftboss/`（见 `shiftboss.docs_install`）。
4. **把本机配置模板拷进每个副本**（若 `shiftboss.toml` 声明了本机配置文件且项目有其 `.example`）。
   它被 gitignore，所以每个副本各要一份，克隆不会带过去。
5. **装提交守卫 hook**（`pre-commit`）。`shiftboss.merge` 还在跑闸时，
   同一个副本里任何别的 `git commit` 都会把那次合并一起交掉——
   **提交的是整个索引，而 `MERGE_HEAD` 还在**，闸一步没跑完，回显却一切正常。

Codex 副本信任由 dispatch 启动 TUI 时用 `-c` 逐次传入，这里不读写共享 Codex 配置；
副本建好后调执行方插件的 `prepare`（如 agy 没有逐次传入信任的办法，要登记进它的 `trustedWorkspaces`）。
"""

from __future__ import annotations
from shiftboss.paths import origin_path, origin_url, project_root, relative_url

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

from shiftboss.dispatch import EXECUTORS

REPO = project_root()


def run(*args: str, cwd: Path | None = None, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          check=check)


def copy_paths(bare: Path, count: int, executor: str = "codex") -> list[Path]:
    """副本命名：副本目录下的 `<执行方><序号>`，序号从 1 起。"""
    from shiftboss.config import copies_dir
    return [copies_dir(bare) / f"{executor}{i}" for i in range(1, count + 1)]


def ensure_bare(repo: Path, check: bool) -> Path:
    # 从执行副本运行也沿用同一个 origin，不在副本旁另建 bare 或副本。
    configured = origin_path(repo)
    if configured is not None:
        require_bare(configured)
        return configured
    url = origin_url(repo)
    from shiftboss.config import HUB_NAME
    bare = repo.parent / (repo.name + ".shiftboss") / HUB_NAME
    if not bare.exists():
        if check:
            print(f"缺 bare origin：{bare}")
            return bare
        run("git", "clone", "--bare", str(repo), str(bare), check=True)
        print(f"建好 bare origin：{bare}")
    require_bare(bare)
    if url != relative_url(bare, repo):
        if check:
            print(f"origin 没指向 bare（现在是 {url or '空'}）")
        else:
            if url:
                run("git", "-C", str(repo), "remote", "remove", "origin", check=True)
            run("git", "-C", str(repo), "remote", "add", "origin",
                relative_url(bare, repo), check=True)
            print(f"origin 指向 {bare}")
    return bare


def require_bare(bare: Path) -> None:
    if not bare.is_dir():
        raise ValueError(f"bare origin 不存在：{bare}")
    result = run("git", "--git-dir", str(bare), "rev-parse", "--is-bare-repository")
    if result.returncode or result.stdout.strip() != "true":
        raise ValueError(f"origin 不是 bare 仓库：{bare}")


def existing_copies(bare: Path, executor: str = "codex") -> list[Path]:
    from shiftboss.config import copies_dir
    root = copies_dir(bare)
    pattern = re.compile(rf"{re.escape(executor)}([1-9][0-9]*)")
    found = [(int(m[1]), p) for p in (root.iterdir() if root.is_dir() else [])
             if p.is_dir() and (m := pattern.fullmatch(p.name))]
    return [p for _, p in sorted(found)]


def require_copy(path: Path, bare: Path) -> None:
    if not path.is_dir():
        raise ValueError(f"副本路径被非目录占用：{path}")
    top = run("git", "-C", str(path), "rev-parse", "--show-toplevel", check=True).stdout.strip()
    if Path(top).resolve() != path.resolve():
        raise ValueError(f"副本路径不是独立工作树：{path}")
    run("git", "-C", str(path), "rev-parse", "--verify", "HEAD", check=True)
    url = origin_url(path)
    if origin_path(path) != bare.resolve():
        raise ValueError(f"已有副本指向不同 origin，保持原样：{path} → {url}")


def ensure_copies(repo: Path, bare: Path, count: int, check: bool,
                  executor: str = "codex") -> list[Path]:
    """只初始化新克隆；已有副本只读核验，脏工作树照常保留。

    从主副本或执行副本调用都按共同的 bare origin 确定副本路径。

    已有副本只读核验，
      缺配置就报错，不替正在干活的工作树补写文件。多余副本照实报告，绝不自动删除。

    配额为 0 时：不凭空选择一个副本数，也不尝试无限克隆；
    """
    from shiftboss.state import validate_quota
    validate_quota(count)
    require_bare(bare)
    existing = existing_copies(bare, executor)
    wanted = copy_paths(bare, count, executor) if count else existing
    extra = [p.name for p in existing if p not in wanted]
    if extra:
        print(f"多出的 {len(extra)} 个副本原样保留：{'、'.join(extra)}")
    if not count:
        print("配额 0＝不分配；不新增副本，已有副本原样保留。")
    ready = []
    for path in wanted:
        new = not path.exists()
        if new and check:
            print(f"缺工作副本：{path}")
            continue
        if new:
            run("git", "clone", str(bare), str(path), check=True)
            # clone 记下的是绝对路径，换成相对写法。
            run("git", "-C", str(path), "remote", "set-url", "origin",
                relative_url(bare, path), check=True)
            print(f"克隆工作副本：{path.name}")
        require_copy(path, bare)
        # 已有副本绝不补写本机文件，缺配置就明确失败。提交守卫在 .git/hooks、不在工作树里，
        # 源码一改全部副本都会判为缺失，所以照常更新。
        local = ensure_local_config(repo, [path], check or not new)
        guarded = ensure_commit_guard(repo, [path], check, include_repo=False)
        if not (local and guarded):
            raise ValueError(f"副本尚不可用：{path}；已有内容未改动，请先处理上面报告的缺项")
        ready.append(path)
    if ready:
        from shiftboss.workers import WORKERS
        trust = WORKERS[executor].prepare
        if not trust(ready, check):
            return []
    return ready


def configure_copies(repo: Path, bare: Path, count: int, check: bool = False,
                     executor: str = "codex") -> bool:
    """两个 CLI 共用：准备齐全才提交配额；失败保留旧配额和已建好的副本。

    **失败保留旧配额**，已成功创建的副本保留；
    """
    from shiftboss.state import read_quota, validate_quota, write_quota
    validate_quota(count)
    read_quota(bare, executor)  # 旧状态坏了先停，不能用新值盖掉它。
    copies = ensure_copies(repo, bare, count, check, executor)
    complete = not count or len(copies) == count
    if check:
        print(f"{executor} 目标配额 {count}（0＝不分配）；已核验 {len(copies)} 个可用副本；配额未修改。")
        return complete
    if not complete:
        raise ValueError(f"需要 {count} 个副本，实际只有 {len(copies)} 个可用")
    write_quota(bare, count, executor)
    print(f"{executor} 配额已设为 {count}（0＝不分配）；已核验 {len(copies)} 个可用副本。")
    return True


def ensure_local_config(repo: Path, copies: list[Path], check: bool) -> bool:
    """本机配置每个副本各要一份——它被 gitignore，克隆带不过去。"""
    from shiftboss import config
    name = config.local_config_file(repo)
    if not name:
        return True
    src = repo / name
    if not src.exists() and not (repo / f"{name}.example").exists():
        return True
    ready = True
    for path in copies:
        if not path.exists():
            continue          # 副本还没建，本机配置无从谈起
        dst = path / src.name
        if dst.is_file():
            continue
        if dst.exists():
            raise ValueError(f"本机配置路径被非文件占用：{dst}")
        if check:
            print(f"缺本机配置：{dst}")
            ready = False
            continue
        if not src.is_file():
            raise ValueError(f"缺本机配置源文件，不能初始化 {path.name}：{src}")
        shutil.copyfile(src, dst)
        print(f"拷本机配置进 {path.name}")
    return ready


def python_hook(dst: Path, script: str) -> dict[Path, str]:
    """git hook 的两个文件：`dst` 是 sh 外壳，以绝对路径起当前解释器跑旁边的 `<名>.py`。

    Git for Windows 只按 shebang 里解释器的文件名在 PATH 上找，绝对路径不起作用；
    脚本不走标准输入，`post-receive` 要从那里读 ref 更新。
    """
    py = dst.with_name(dst.name + ".py")
    shell = f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "{py.as_posix()}" "$@"\n'
    return {py: script, dst: shell}


def write_hook(files: dict[Path, str]) -> None:
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        path.chmod(path.stat().st_mode | 0o111)


def ensure_commit_guard(repo: Path, copies: list[Path], check: bool, *,
                        include_repo: bool = True) -> bool:
    """每个工作副本都要装 pre-commit：合并在飞时拦住别的提交。

    拦的是这一种事故：`shiftboss.merge` 还在跑闸，同一个副本里有人（多半是我自己）
    为了别的事跑了一次 `git commit`。**提交的是整个索引，而 MERGE_HEAD 还在**，
    那次提交就直接变成合并提交——闸没跑完，回显却一切正常，推上去才发现。
    hook 不进 git，所以装它必须是代码。
    """
    src = Path(__file__).resolve().with_name("hooks") / "pre_commit.py"
    if not src.exists():
        raise ValueError(f"缺提交守卫源文件：{src}")
    script = src.read_text(encoding="utf-8")
    ready = True
    for path in ([repo, *copies] if include_repo else copies):
        if not path.exists():
            continue
        git_dir = Path(run("git", "-C", str(path), "rev-parse", "--absolute-git-dir",
                           check=True).stdout.strip())
        dst = git_dir / "hooks" / "pre-commit"
        want = python_hook(dst, script)
        if all(f.is_file() and f.read_bytes() == text.encode("utf-8") for f, text in want.items()):
            continue
        if check:
            print(f"缺提交守卫 hook：{dst}")
            ready = False
            continue
        write_hook(want)
        print(f"装提交守卫进 {path.name}")
    return ready


def main(argv: list[str] | None = None) -> int:
    """在新机器或新项目上把这套搭起来

    幂等，可反复跑；`--check` 只报告不动手

    目标副本数与配额：`shiftboss bootstrap --copies N` 设置同一个数；不带 `--copies`
      就按已存配额补齐，`--check` 只报告、不改配额或副本。
    """
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--copies", type=int, metavar="N",
                    help="设置配额并补齐副本；省略时用已存配额（0＝不分配，不新增）")
    ap.add_argument("--executor", choices=EXECUTORS,
                    help="只处理这个执行方（--copies 缺省作用于 codex）；省略时各执行方按已存配额补齐")
    ap.add_argument("--check", action="store_true", help="只报告差在哪，不动手")
    args = ap.parse_args(argv)

    from shiftboss.state import read_quota, validate_quota
    try:
        if args.copies is not None:
            validate_quota(args.copies)
        executors = ([args.executor or "codex"] if args.copies is not None or args.executor
                     else list(EXECUTORS))
        bare = ensure_bare(REPO, args.check)
        print(f"仓库 {REPO}{'（只检查）' if args.check else ''}\n")
        if not bare.exists():
            return 1
        if not args.check:
            from shiftboss.doorbell import install_hook
            install_hook(bare)
        hook_ready = (bare / "hooks" / "post-receive").exists()
        if not hook_ready:
            print("门铃 hook 没装")
        guarded = ensure_commit_guard(REPO, [], args.check)
        from shiftboss.docs_install import install as install_docs, install_entry
        documented = install_docs(REPO, args.check)
        documented = install_entry(REPO, args.check) and documented
        from shiftboss import config
        if path := config.missing(REPO):
            print(f"缺项目配置 {path}：派活方按 shiftboss/README.md 3.0 生成，生成前 dispatch 拒派。")
        copies_ready = True
        for executor in executors:
            count = read_quota(bare, executor) if args.copies is None else args.copies
            copies_ready = configure_copies(REPO, bare, count, args.check, executor) and copies_ready
        if not (hook_ready and guarded and documented and copies_ready):
            return 1
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(f"副本准备失败，配额未修改：{detail}")
        return 1
    print("\n完成。" if not args.check else "\n以上是差的部分；去掉 --check 就补齐。")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    raise SystemExit(main())

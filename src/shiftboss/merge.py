#!/usr/bin/env python3
"""合并一个 ref，自动解明确列出的派生物冲突，完成快速结构检查后提交；不在合并时生成派生物、不运行测试，两者交后台验证。

    shiftboss merge origin/codex/example
    shiftboss merge --regen <合并前HEAD>     # 合并时生成失败，修好后重试生成

要求干净工作树。不 fetch、不 push；失败保留现场，绝不替人解名单外的冲突。
生产端的精确产物报告在 `shiftboss.toml` 的 `[regen] catalog`，不支持目录或通配符。
"""

from __future__ import annotations
from shiftboss.paths import project_root

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import sys
from collections import Counter
from pathlib import Path

from shiftboss import regen
from shiftboss import config
from shiftboss.outputs import CATALOG, catalog_files, commit_generated, plan_commands

REPO = project_root()


class MergeError(RuntimeError):
    pass


# 合并在飞时下锁，拦住同一个副本里任何别的 `git commit`——理由与实现见
# `shiftboss.state` 里的「合并在飞的锁」。手工恢复那条路由 `shiftboss.regen` 下同一把锁。
from shiftboss.state import acquire_merge_lock, clear_merge_failed, mark_merge_failed, release_merge_lock  # noqa: E402


def acquire_lock(ref: str) -> None:
    acquire_merge_lock(REPO, f"ref={ref}")


def release_lock() -> None:
    release_merge_lock(REPO)


def git(*args: str, check: bool = True, input: str | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-c", "core.quotePath=false", *args], cwd=REPO,
        capture_output=True, text=True, encoding="utf-8", errors="strict", input=input,
    )
    if check and proc.returncode:
        raise MergeError(f"git {' '.join(args)}\n{proc.stdout}{proc.stderr}".strip())
    return proc


def show(proc: subprocess.CompletedProcess) -> None:
    for output in (proc.stdout, proc.stderr):
        if output.strip():
            print(output.rstrip(), flush=True)


def derived_files() -> list[str]:
    """产物清单按它自己记录的步骤读：它只由常驻验证的完整生成刷新，与当前步骤是否一致归常驻验证管。"""
    catalog = json.loads((REPO / CATALOG).read_text(encoding="utf-8"))
    files = catalog_files(catalog, [p["command"] for p in catalog.get("producers") or []])
    for name in files:
        output = REPO / name
        if not output.is_file() or output.is_symlink() or output.resolve() != output:
            raise MergeError(f"生产端声明的产物不存在或不是普通文件：{name}；请重新生成")
    return files


def preflight() -> None:
    root = Path(git("rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if root != REPO:
        raise MergeError(f"模块所在目录不是仓库根：{REPO}（实际 {root}）")
    for state in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
        path = Path(git("rev-parse", "--git-path", state).stdout.strip())
        if (REPO / path).exists():
            raise MergeError(f"已有 {state}，先人工处理现有操作")
    if git("symbolic-ref", "--quiet", "HEAD", check=False).returncode:
        raise MergeError("当前是 detached HEAD，请先切到接收合并的分支")
    status = git("status", "--porcelain=v1", "--untracked-files=all").stdout
    if status:
        raise MergeError(f"工作树不干净，尚未开始合并：\n{status.rstrip()}")
    generation_plan()


def generation_plan() -> None:
    # 合并前检查一次，合并后再检查新清单，避免空清单被当成“全绿”。
    plan = regen.load_plan()
    if not isinstance(plan, dict):
        raise MergeError("regen.json 必须是包含生成计划（stages 或 steps）与 gates 的对象")
    try:
        regen.validate_stages(plan)
    except ValueError as exc:
        raise MergeError(f"regen.json 的 stages 无效：{exc}") from exc
    for key, commands in (("stages" if plan.get("stages") is not None else "steps", plan_commands(plan)),
                          ("gates", plan.get("gates"))):
        if (not isinstance(commands, list) or not commands
                or any(not isinstance(cmd, list) or not cmd
                       or any(not isinstance(arg, str) or not arg for arg in cmd)
                       for cmd in commands)):
            raise MergeError(f"regen.json 的 {key} 必须是非空命令列表")


OLD_WORKFLOW_MODULE = re.compile(
    r"\bworkflow\.(?:tools\.)?(agy|bootstrap|codex|dispatch|doorbell|inbox|merge|omp|outputs|recover"
    r"|regen|state|taskbook|verify_tests|verify)\b(?!-|\.v1|ication)")


# 编辑器按 ES 模块加载；`node --check` 对 `.js` 按脚本解析，函数内误嵌的 `export` 查不出。
# 手工合成冲突漏掉括号时正是这样蒙混过关，所以复制成 `.mjs` 再查。
def check_js_module(path: Path, name: str) -> None:
    import shutil
    import tempfile
    node = shutil.which("node")
    if not node:
        raise MergeError("找不到 node，无法检查改动的 JS")
    with tempfile.TemporaryDirectory() as temp:
        copy = Path(temp) / "check.mjs"
        copy.write_bytes(path.read_bytes())
        result = subprocess.run([node, "--check", str(copy)], capture_output=True,
                                text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise MergeError(f"JS 模块语法错误：{name}\n{result.stderr.strip()[:600]}")


_NAMED_IMPORT = re.compile(r'(?:^|(?<=[;{}]))[ \t]*import\s*\{([^}]*)\}\s*from\s*["\'](\.{1,2}/[^"\']+)["\']',
                           re.MULTILINE)


def _code_only(text: str) -> str:
    """注释与模板字符串的内容换成空格，换行与引号字符串保留：模块路径就在引号字符串里。

    测试常把一段模块源码放进模板或引号字符串当夹具，那里的 `import` 不是本模块的导入。
    """
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "/" and i + 1 < n and text[i + 1] in "/*":
            end = text.find("\n", i) if text[i + 1] == "/" else text.find("*/", i + 2)
            end = n if end < 0 else (end if text[i + 1] == "/" else end + 2)
            for j in range(i, end):
                if out[j] != "\n":
                    out[j] = " "
            i = end
        elif ch in "'\"":
            j = i + 1
            while j < n and text[j] != ch and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            i = j + 1
        elif ch == "`":
            j = i + 1
            while j < n and text[j] != "`":
                j += 2 if text[j] == "\\" else 1
            for k in range(i + 1, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j + 1
        else:
            i += 1
    return "".join(out)


def named_imports(text: str) -> list[tuple[str, str]]:
    """本模块语句层面的相对路径具名导入（导入名, 模块路径）；字符串与注释里的不算。"""
    return _NAMED_IMPORT.findall(_code_only(text))
_EXPORT_NAME = re.compile(r'export\s+(?:async\s+)?(?:function\*?|const|let|var|class)\s+([A-Za-z_$][\w$]*)')
_EXPORT_LIST = re.compile(r'export\s*\{([^}]*)\}')


def js_exports(path: Path) -> set[str] | None:
    """模块的具名导出；含 `export *` 时无法静态确定，返回 None。"""
    text = path.read_text(encoding="utf-8")
    if re.search(r'export\s*\*', text):
        return None
    names = set(_EXPORT_NAME.findall(text))
    for group in _EXPORT_LIST.findall(text):
        for item in group.split(","):
            item = item.strip()
            if item:
                names.add(item.split(" as ")[-1].strip())
    return names


def duplicate_definitions(source: str) -> list[str]:
    """模块顶层与类体内重复的 def/class 名；`@overload`、`@x.setter` 这类装饰器定义不计。"""
    import ast
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found = []
    def scan(body, prefix=""):
        seen = set()
        for node in body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if any(isinstance(d, ast.Attribute) or (isinstance(d, ast.Name) and d.id == "overload")
                   for d in getattr(node, "decorator_list", [])):
                continue
            key = prefix + node.name
            if key in seen:
                found.append(f"{key}（第 {node.lineno} 行）")
            seen.add(key)
            if isinstance(node, ast.ClassDef):
                scan(node.body, key + ".")
    scan(tree.body)
    return found


_DUPLICATE_KEYS_SCRIPT = r"""
import {readFileSync} from 'node:fs';
const [parserUrl, ...files] = process.argv.slice(2);
const {parse} = await import(parserUrl);
const found = [];
const visit = (node, file) => {
  if (!node || typeof node.type !== 'string') return;
  if (node.type === 'ObjectExpression') {
    const seen = new Map();
    for (const prop of node.properties) {
      if (prop.type !== 'Property' || prop.computed) continue;
      const key = prop.key.type === 'Identifier' ? prop.key.name : String(prop.key.value);
      const kinds = seen.get(key) || [];
      if (kinds.includes(prop.kind) || (prop.kind === 'init' && kinds.length) || (prop.kind !== 'init' && kinds.includes('init')))
        found.push(`${file}:${prop.loc.start.line} 键 ${key}`);
      seen.set(key, [...kinds, prop.kind]);
    }
  }
  for (const value of Object.values(node))
    if (Array.isArray(value)) value.forEach(child => visit(child, file));
    else if (value && typeof value.type === 'string') visit(value, file);
};
for (const file of files)
  visit(parse(readFileSync(file, 'utf8'), {ecmaVersion: 'latest', sourceType: 'module', locations: true,
    allowHashBang: true}), file);
process.stdout.write(found.join('\n'));
"""


# 对象字面量重复键不是语法错误，后者静默覆盖前者；手工解合并冲突时两侧各留一份同名键即如此。
def check_js_duplicate_keys(names: list[str]) -> str:
    parser = config.js_parser()
    if not parser:
        return "未配置 merge.js_parser，跳过"
    files = [name for name in names if name and Path(name).suffix in (".js", ".mjs")
             and (REPO / name).is_file()
             and any(name.startswith(prefix) for prefix in config.js_module_prefixes())]
    if not files:
        return "0 文件"
    node = shutil.which("node")
    if not node:
        raise MergeError("找不到 node，无法检查 JS 对象重复键")
    with tempfile.TemporaryDirectory() as temp:
        script = Path(temp) / "duplicate-keys.mjs"
        script.write_text(_DUPLICATE_KEYS_SCRIPT, encoding="utf-8")
        result = subprocess.run([node, str(script), (REPO / parser).as_uri(), *files], cwd=REPO,
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise MergeError(f"JS 对象重复键检查失败：\n{result.stderr.strip()[:600]}")
    if result.stdout.strip():
        raise MergeError("JS 对象字面量重复键（后者静默覆盖前者，多为解冲突时两侧各留一份）：\n  "
                         + result.stdout.strip().replace("\n", "\n  "))
    return f"{len(files)} 文件"


def check_js_imports(names: list[str]) -> None:
    """改动的 JS 模块与引用它们的模块：相对路径的具名导入须在目标模块中导出。

    合并取一边时，另一边留下的导入或被删的导出只有运行时才报错，语法检查看不出来。
    """
    prefixes = config.js_module_prefixes()
    touched = [n for n in names if n.endswith((".js", ".mjs")) and any(n.startswith(p) for p in prefixes)]
    changed = [n for n in touched if (REPO / n).is_file()]
    importers = set(changed)
    # 被删的模块也要找引用方：删了生成器而测试还在导入它，只有这样才查得到。
    for name in touched:
        found = git("grep", "-l", "-F", Path(name).name, "--", *prefixes, check=False).stdout.split()
        importers.update(found)
    missing = []
    for name in sorted(importers):
        path = REPO / name
        if not path.is_file() or path.suffix not in (".js", ".mjs"):
            continue
        for group, target in named_imports(path.read_text(encoding="utf-8")):
            module = (path.parent / target).resolve()
            if not module.is_file():
                missing.append(f"{name} → {target}（模块不存在）")
                continue
            exported = js_exports(module)
            if exported is None:
                continue
            for item in group.split(","):
                item = item.strip()
                imported = item.split(" as ")[0].strip()
                if imported and imported not in exported:
                    missing.append(f"{name} → {target}：{imported}")
    if missing:
        raise MergeError("JS 具名导入在目标模块中没有导出：\n  " + "\n  ".join(missing))


def fast_checks(before: str) -> None:
    """Only structural checks; freshness and semantic correctness belong to verification.

    合并必须保持快速：这里只放语法、配置与产物清单这类秒级结构检查；规则扫描、生成、浏览器等可能卡住的检查一律放常驻验证。
    """
    started = time.monotonic()
    generation_plan()
    # 未归层的测试文件会让常驻验证的生成阶段整体失败；在引入它的那次合并就拦下。
    # 分层是可选声明，与 regen 的判法一致：没声明就不查。
    plan = regen.load_plan()
    if "gate_layers" in plan:
        try:
            regen.validate_layers(plan)
        except (ValueError, KeyError) as exc:
            raise MergeError(f"测试归层检查失败：{exc}；在生成计划（{config.plan_file()}）的对应 gate 中登记后再合并") from exc
    # 只拦改过的已装文档；没装的由 `shiftboss bootstrap --check` 报。
    from shiftboss.docs_install import drift
    if stale := drift(REPO, missing=False):
        raise MergeError("工作流文档与包内不一致（改动在包内 docs/ 做，再 `shiftboss bootstrap` 同步）："
                         + "、".join(stale))
    files = derived_files()
    print(f"快速检查 配置与产物清单: {len(files)} 路径 {time.monotonic()-started:.3f}s", flush=True)
    started = time.monotonic()
    names = git("diff", "--name-only", "--no-renames", "-z", before).stdout.split("\0")
    counts = {".json": 0, ".py": 0, ".js": 0, ".mjs": 0}
    for name in names:
        path = REPO / name
        if not name or not path.is_file() or path.suffix not in counts:
            continue
        if path.is_symlink():
            raise MergeError(f"不检查符号链接源码：{name}")
        if path.suffix == ".json":
            json.loads(path.read_text(encoding="utf-8"))
        elif path.suffix == ".py":
            compile(path.read_bytes(), name, "exec")
            # 工作流代码在 `shiftboss` 包里；旧分支带进来的 `workflow.<模块>`、`workflow.tools.<模块>` 要到运行时才报错。
            duplicated = duplicate_definitions(path.read_text(encoding="utf-8"))
            if duplicated:
                raise MergeError(f"{name} 同一作用域重复定义（后者静默覆盖前者，多为两线各加一份后合并）：{'、'.join(duplicated)}")
            stale = OLD_WORKFLOW_MODULE.search(path.read_text(encoding="utf-8"))
            if stale:
                raise MergeError(f"{name} 引用旧路径 `{stale.group(0)}`，改为 `shiftboss.{stale.group(1)}`"
                                 "（执行方插件在 `shiftboss.workers`）")
        elif any(name.startswith(prefix) for prefix in config.js_module_prefixes()):
            check_js_module(path, name)
        else:
            continue
        counts[path.suffix] += 1
    print(f"快速检查 改动 JSON/Python/JS 语法: {counts} {time.monotonic()-started:.3f}s", flush=True)
    started = time.monotonic()
    check_js_imports(names)
    print(f"快速检查 JS 具名导入: {time.monotonic()-started:.3f}s", flush=True)
    started = time.monotonic()
    duplicate_keys = check_js_duplicate_keys(names)
    print(f"快速检查 JS 对象重复键: {duplicate_keys} {time.monotonic()-started:.3f}s", flush=True)
    check_new_tests(before)
    assertion_delta(before)


def check_new_tests(before: str) -> None:
    """未登记的新增测试项拒绝合并；口径见 `shiftboss.testguard`。"""
    from shiftboss import testguard
    old = git("show", f"{before}:{config.plan_file()}", check=False)
    plan = REPO / config.plan_file()
    found = testguard.find(
        lambda *args: git(*args).stdout, before, None, config.assertion_dirs(),
        (old.stdout if old.returncode == 0 else None, plan.read_text(encoding="utf-8") if plan.is_file() else None),
        set(config.approved_tests()),
        untracked=git("ls-files", "--others", "--exclude-standard").stdout.split())
    if found:
        raise MergeError(testguard.MESSAGE + "\n  " + "\n  ".join(found))


_TOKEN = re.compile(r'"[^"]*"|\'[^\']*\'|\w+|[^\s\w]')


def assertion_delta(before: str) -> None:
    """在合并输出中列出测试断言的增删；不拦合并。

    1. 删除的断言须在交回中说明。
    2. 只增不减的改写不计为删除：旧行的全部记号都保留在同一文件的某一新增断言行中。
    """
    started = time.monotonic()
    dirs = config.assertion_dirs()
    if not dirs:
        return
    diff = git("diff", "--no-renames", "-U0", before, "--", *dirs).stdout
    removed_by_file: dict[str, list[str]] = {}
    added_by_file: dict[str, list[str]] = {}
    path = ""
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:]
        elif line.startswith("---"):
            continue
        elif "assert" in line and line[:1] in "+-":
            bucket = added_by_file if line[0] == "+" else removed_by_file
            bucket.setdefault(path, []).append(line[1:].strip())
    added = sum(len(lines) for lines in added_by_file.values())
    removed: list[str] = []
    extended = 0
    for name, lines in removed_by_file.items():
        candidates = [Counter(_TOKEN.findall(text)) for text in added_by_file.get(name, [])]
        for text in lines:
            need = Counter(_TOKEN.findall(text))
            match = next((c for c in candidates if all(c[t] >= n for t, n in need.items())), None)
            if match is None:
                removed.append(text)
            else:
                candidates.remove(match)
                extended += 1
    print(f"快速检查 测试断言: +{added} -{len(removed)} {time.monotonic()-started:.3f}s", flush=True)
    if extended:
        print(f"    只增不减的改写 {extended} 行（不计入删除）", flush=True)
    for line in removed[:20]:
        print(f"    删除的断言 {line[:120]}", flush=True)
    if len(removed) > 20:
        print(f"    （另有 {len(removed)-20} 行未列）", flush=True)
    if removed:
        print("    **这些要在交回里有交代**：重写、换等值判法、换更强的检查都算；"
              "没有交代就是放宽，按合同退回。", flush=True)


def finish(before: str) -> None:
    """
    仍有冲突会拒绝。
    """
    if git("rev-parse", "--verify", "--quiet", "MERGE_HEAD", check=False).returncode:
        raise MergeError("没有待完成合并；--finish 不提交普通工作树改动")
    if git("ls-files", "--unmerged").stdout:
        raise MergeError("仍有未解决的冲突")
    target = git("rev-parse", "MERGE_HEAD").stdout.strip()
    fast_checks(before)
    git("add", "--all")
    show(git("commit", "--no-edit"))
    if git("status", "--porcelain=v1", "--untracked-files=all").stdout:
        raise MergeError("提交后工作树不干净")
    seal_stale_derived()
    _, summary = regenerate(before)
    print(f"合并完成；{summary}；未跑测试、未推送。", flush=True)
    close_merged_sessions(target)


def regenerate(before: str) -> tuple[str | None, str]:
    """生成计划设 `merge_regen: true` 时，在隔离工作树里生成派生物，提交后快进并入当前分支；返回（生成提交, 结果摘要）。

    **缺省不在合并时生成**：合并即推送，派生物与检查一律交后台批量验证；合并路径上任何耗时步骤都会成为卡住全线的单点。
    生成在隔离工作树里跑，主副本在生成期间不出现写到一半的派生物。
    `merge_regen_skip` 列出的前缀不触发生成。
    """
    from shiftboss.verify import generation_needed
    plan = regen.load_plan()
    if not plan.get("merge_regen"):
        return None, "合并时不生成，派生物由后台验证补齐"
    if not plan.get("path_layers"):
        return None, "生成计划未声明生成输入（path_layers），合并时不生成，派生物由后台验证补齐"
    paths = [p for p in git("diff", "--name-only", "-z", before, "HEAD").stdout.split("\0") if p]
    skip = tuple(plan.get("merge_regen_skip") or ())
    kept = [p for p in paths if not p.startswith(skip)] if skip else paths
    if not generation_needed(plan, kept):
        if len(kept) < len(paths) and generation_needed(plan, paths):
            return None, f"本次合并的生成输入只在 {', '.join(skip)} 下，合并时不生成，派生物由后台验证补齐"
        return None, "本次合并未改生成输入，不需重新生成"
    merged = git("rev-parse", "HEAD").stdout.strip()
    work = Path(tempfile.gettempdir()) / f"shiftboss-merge-regen-{merged[:12]}"
    if work.exists():
        git("worktree", "remove", "--force", str(work), check=False)
        shutil.rmtree(work, ignore_errors=True)
    git("worktree", "prune")
    git("worktree", "add", "--detach", str(work), merged)
    retry = (f"合并提交 {merged[:12]} 留在本地、未推送。修好后运行 `shiftboss merge --regen {before}` 重试；"
             f"放弃本次合并用 `git reset --hard {before}`。")
    try:
        name = config.local_config_file(REPO)
        local = REPO / name if name else None
        if local and local.is_file():
            shutil.copyfile(local, work / local.name)
        catalog = json.loads((work / CATALOG).read_text(encoding="utf-8"))
        old_files = set(catalog_files(catalog, [p["command"] for p in catalog.get("producers") or []]))
        print(f"本次合并改了生成输入，在隔离工作树 {work} 重新生成派生物：", flush=True)
        base = ["--base", before] if plan.get("stages") is not None else []
        result = subprocess.run([sys.executable, "-B", "-m", "shiftboss.regen", "--steps", *base], cwd=work)
        if result.returncode:
            raise MergeError(f"合并后生成失败（退出码 {result.returncode}）。{retry}")
        try:
            generated = commit_generated(work, merged, old_files, plan_commands(plan))
        except (ValueError, subprocess.CalledProcessError) as exc:
            raise MergeError(f"合并后生成的派生物不能提交：{exc}。{retry}") from exc
        if generated is None:
            return None, "派生物已随合并重新生成，与合并提交一致，无需另行提交"
        if git("rev-parse", "HEAD").stdout.strip() != merged:
            raise MergeError(f"生成期间当前分支已离开 {merged[:12]}，不并入生成提交 {generated[:12]}。{retry}")
        show(git("merge", "--ff-only", generated))
        return generated, f"派生物已随合并生成（{generated[:12]}）"
    finally:
        git("worktree", "remove", "--force", str(work), check=False)
        shutil.rmtree(work, ignore_errors=True)
        git("worktree", "prune", check=False)


def archive_after_merge(name: str, book) -> Path:
    """`--done` 时把任务书**直接归档**——不是「核对中」。

    **核对认定结束就是验收动作**，不必等常驻验证跑完再确认一次。
    2026-09-09 用户点出这条：等验证线回话导致 8 份任务书堆在「核对中」，
    而收件箱又把它们算成「在办」，于是同一份活被重复派了出去。

    **后来发现的 bug 不重开旧任务书**：那是一条新的修复线，把这份归档的
    当参考引进去就行。重开等于让一份任务书没有终点。

    只动状态那一格并把文件挪进 `archive/`，正文一个字不碰；
    交回里还留着「待议 / 待定夺」的，**这里只喊一声**——裁定是派活方的事，
    但那也不该把任务书吊在半空。**合同里的勾不作数**：勾是执行方早几轮打的，
    交回改回待议时不会有人回去改勾，两边不一致没有一处会报错。
    """
    import re
    import shutil
    path = book.path
    sha = git("rev-parse", "--short", "HEAD").stdout.strip()
    # **两种交回格式都要认。** 只匹配 `| 2.` 开头的表格行时，写成编号列表的
    # 「2. 合同 3.2 保持待议」整条溜过去——`move-python-to-browser` 就是这么
    # 带着两条待议被归档的，而它合同里的勾还停在早几轮打的「完成」。
    pending = [line.strip() for line in book.delivery_text().splitlines()
               if ("待议" in line or "待定夺" in line)
               and re.match(r"\s*(\||\d+[.、])", line)]
    already = path.parent.name == "archive"
    target = path if already else path.parent / "archive" / path.name
    target.parent.mkdir(parents=True, exist_ok=True)
    if book.is_dir:
        # **目录布局：状态那格在 meta.json，正文一个字不碰。**
        book.set_meta(调度状态=f"已归档（合并 {sha}）")
        if not already:
            shutil.move(str(path), str(target))
            git("add", "--", str(path.relative_to(REPO)))
    else:
        text = path.read_text(encoding="utf-8")
        new = re.sub(r"^\| 状态 \| .*$", f"| 状态 | `已归档`（合并 `{sha}`） |",
                     text, count=1, flags=re.M)
        if new == text and already:
            return path
        target.write_text(new, encoding="utf-8")
        if not already:
            path.unlink()
            git("add", "--", str(path.relative_to(REPO)))
    git("add", "--", str(target.relative_to(REPO)))
    git("commit", "-q", "-m", f"plans: {name} 交付已合并，归档")
    print(f"{name}：已归档（{target.relative_to(REPO)}）。", flush=True)
    if pending:
        print(f"  ⚠ 交回里还有 {len(pending)} 条待议/待定夺，归档不代表裁定过了：\n    "
              + "\n    ".join(line[:110] for line in pending), flush=True)
    return target


def close_merged_sessions(target: str) -> None:
    """合并只把提交带进 main，不关会话、不归档：做没做完由派活方核对后用 `--done` 定。

    任务书不再分长线短线，合并这一刻判断不出「这条线到头了」；
    按表头猜的那几版把还在跑的线掐过，也把没做完的线归档过。
    """
    if git("symbolic-ref", "--short", "HEAD").stdout.strip() != "main":
        return
    refs = git("for-each-ref", "--format=%(objectname)",
               "refs/heads/codex/", "refs/remotes/origin/codex/").stdout.splitlines()
    if target not in refs:
        return
    from shiftboss import dispatch
    for name, entry in dispatch.load_sessions().items():
        branch = entry.get("branch", "")
        if not branch.startswith("codex/"):
            continue
        tip = git("rev-parse", "--verify", f"origin/{branch}", check=False)
        if tip.returncode:
            tip = git("rev-parse", "--verify", branch, check=False)
        if tip.returncode == 0 and tip.stdout.strip() == target:
            from shiftboss.taskbook import Taskbook, execution_status
            book = Taskbook.find(REPO, name)
            signal = execution_status(book.delivery_text() if book else "") or "未标"
            print(f"{name}：已合入，执行状态「{signal}」，会话未动。"
                  f"完成或拆分后用 `shiftboss.merge --done {name}` 收摊归档；否则续派。", flush=True)


def finish_line(name: str, superseded_by: str | None = None) -> None:
    """`--done`：派活方核对认定这份任务书结束（做完，或已拆出新任务书），关会话并归档。

    只收已合进当前分支的线：分支上还有没合的提交就拒，免得把交付丢在分支上。
    """
    from shiftboss import dispatch
    from shiftboss.taskbook import Taskbook
    book = Taskbook.find(REPO, name)
    if book is None or "archive" in book.path.parts:
        raise MergeError(f"{name} 不在在办任务书里")
    branch = book.meta().get("分支", f"codex/{name}")
    for ref in (f"origin/{branch}", branch):
        if git("rev-parse", "--verify", "--quiet", ref, check=False).returncode == 0:
            if git("merge-base", "--is-ancestor", ref, "HEAD", check=False).returncode:
                raise MergeError(f"{ref} 还有没合进当前分支的提交；先合或确认丢弃再 --done")
            break
    from shiftboss.taskbook import execution_status
    # 派活方已把余下的活交给另一份任务书时，执行方停在「进展」也可收；接手方写进台账输出。
    if execution_status(book.delivery_text()) == "进展" and not superseded_by:
        raise MergeError(f"{name} 的执行状态是「进展」，执行方还要接着做；等它标「完成」或「停下」")
    ledger = dispatch.load_sessions()
    entry = ledger.get(name)
    if entry and not entry.get("closed"):
        dispatch.close_line(name, entry["copy"], ledger)
    archive_after_merge(name, book)
    prune_line_evidence(name, (entry or {}).get("copy") or book.meta().get("你的工作副本"))


def prune_line_evidence(name: str, copy: str | None) -> None:
    """归档即删这条线的证据：执行副本里的 `out/<name>`、`out/*/<name>`，与系统临时目录下名字含 `<name>` 的目录。

    证据只为核对存在，核对认定结束就用完了；`out/` 本是可整根删除的目录。
    不留着它，20 个副本轮换承接几百条线，`out/` 会无界增长到上百 GB。
    **只按名删**：一个副本先后承接多条线，按名才不碰在办线的证据。
    临时目录里的干净检出每份约 1 GiB，落在内存盘上；不带名就删不到，十几份就能把它写满。
    """
    import shutil
    import tempfile
    temp = [target for target in Path(tempfile.gettempdir()).glob(f"*{name}*") if target.is_dir()]
    out = Path(copy) / "out" if copy else None
    local = [out / name, *out.glob(f"*/{name}")] if out else []
    for target in [*local, *temp]:
        if target.is_dir():
            size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
            shutil.rmtree(target)
            print(f"  已删证据 {target}（{size / 2**30:.2f} GiB）", flush=True)


def moving_branch_reason(ref: str, target: str) -> str | None:
    """仍在推进的线只许按提交号合，不许按分支名合。

    **验证的是某个提交，合并的却是分支名此刻指向的提交**：自走线在验证与合并之间
    又推了新提交，没验证过的那段就跟着进了 main，后台生成随之变红。
    交回写着「执行状态：进展」的线还会继续推，分支名靠不住；
    把验证时 `git rev-parse` 得到的提交号交给本命令，合的就是验证过的那一个。
    """
    if re.fullmatch(r"[0-9a-f]{7,40}", ref):
        return None
    name = ref.rsplit("/", 1)[-1]
    shown = git("show", f"{target}:shiftboss/plans/{name}/delivery.md", check=False)
    if shown.returncode or not re.search(r"执行状态[：:]\s*进展", shown.stdout):
        return None
    return (f"{ref} 的交回是「执行状态：进展」，这条线还在推进，分支名随时会前进。\n"
            f"按验证过的提交号合：先 `git rev-parse {ref}` 记下提交号、在该提交上验证，"
            f"再 `shiftboss merge <提交号>`（此刻是 {target[:12]}）。")


def report_failure_state() -> None:
    """Make a failed command's remaining Git state and next action explicit."""
    try:
        pending = git("rev-parse", "--verify", "--quiet", "MERGE_HEAD", check=False)
        if pending.returncode == 0:
            conflicts = git("diff", "--name-only", "--diff-filter=U", "-z").stdout
            count = len([name for name in conflicts.split("\0") if name])
            print(f"当前状态：存在 MERGE_HEAD，合并尚未提交；未解决冲突 {count} 个。", file=sys.stderr)
            mark_merge_failed(REPO, "shiftboss merge 失败后留下的合并现场；用 shiftboss merge --finish 或 git merge --abort 收尾")
            print("下一步：未解决冲突时 git merge --abort 撤回并打回该线重新合并（shiftboss/README.md 2.3）；"
                  "快速检查失败时修正后用新进程运行 shiftboss merge --finish HEAD。",
                  file=sys.stderr)
        else:
            dirty = bool(git("status", "--porcelain=v1", "--untracked-files=all").stdout)
            print("当前状态：无 MERGE_HEAD；" + ("索引或工作树有改动，先用 git status 核对已有工作。"
                  if dirty else "索引和工作树干净。"), file=sys.stderr)
    except (MergeError, OSError) as exc:
        print(f"无法读取失败现场：{exc}；先运行 git status 核对，不能视为合并成功。", file=sys.stderr)


def stale_derived_path():
    from shiftboss.inbox import origin_git_dir
    from shiftboss.verify import state_dir
    return state_dir(origin_git_dir(REPO)) / "derived-stale.json"


def note_stale_derived(files) -> None:
    """派生物取了旧侧，把这笔债记到 origin 上，让收件箱看得见。

    **取旧侧本身是对的**：两边都改过的生成物没法逐块解。
    问题是这笔债此前**只活在那一行回显里**——下一个消费者读到的是缺了内容的生成物，
    而重新生成要等后台验证轮到这个提交。2026-09-09 因此丢过两次：
    一次 `battle_message_sequences` 整块消失，一次场景索引从 11 条掉回 9 条，
    后者靠消费端自己加的守卫才发现。

    **这里只让它可见，不负责修**：后台验证验到这个提交（或它的后代）并补齐派生物后，
    收件箱自然不再显示。
    """
    # **记账不许挡住合并。** 这是给收件箱看的附带信息，写不成就说一声继续走：
    # 合并停在半路的代价，比少一条提示大得多。
    try:
        from shiftboss.state import write_state
        write_state(stale_derived_path(), {"commit": None, "files": sorted(files), "at": time.time()})
    except (Exception, SystemExit) as exc:  # noqa: BLE001  origin_git_dir 用 sys.exit 退场
        print(f"（派生物欠账没记下：{exc}）", flush=True)


def seal_stale_derived() -> None:
    """提交之后把这笔债钉在具体提交上。"""
    try:
        from shiftboss.state import read_state, write_state
        path = stale_derived_path()
        note = read_state(path)
        if note and note.get("commit") is None:
            note["commit"] = git("rev-parse", "HEAD").stdout.strip()
            write_state(path, note)
    except (Exception, SystemExit) as exc:  # noqa: BLE001  origin_git_dir 用 sys.exit 退场
        print(f"（派生物欠账没钉到提交上：{exc}）", flush=True)


def resolve_derived(files: list[str], before: str) -> set[str]:
    """
    只认 `derived-artifacts.json` 中生产端报告的精确路径；报告外冲突一律停下

    仅自动处理产物清单（`[regen] catalog`）中精确普通文件的内容冲突；
    有名单外冲突或增删/类型冲突就停止，不自动解任何文件。纯派生物内容冲突
    整组（含无冲突成员及报告本身）取合并前当前侧，保留单一版本。
    """
    entries: dict[str, list[tuple[str, str]]] = {}
    for record in git("ls-files", "--unmerged", "-z").stdout.split("\0"):
        if record:
            meta, name = record.split("\t", 1)
            mode, _oid, stage = meta.split()
            entries.setdefault(name, []).append((mode, stage))
    refused = sorted(set(entries) - set(files))
    # 增删、改名或文件类型冲突不按内容冲突处理；即使命中名单也交给人。
    refused += sorted(name for name, stages in entries.items()
                      if name in files and (set(stage for _, stage in stages) != {"1", "2", "3"}
                                            or any(mode != "100644" for mode, _ in stages)))
    if refused:
        raise MergeError("冲突拒绝：含名单外文件或非普通内容冲突；没有自动解任何文件：\n"
                         + "\n".join(f"  {name}" for name in refused)
                         )
    if not entries:
        return set()
    # 一组文件都取合并前同一个提交，包括这一轮没有冲突的成员。
    # 先核对完整组存在且全是普通文件，再一次性恢复，不能逐个取不同侧。
    tree = {}
    for row in git("ls-tree", "-r", "-z", before).stdout.split("\0"):
        if row:
            meta, name = row.split("\t", 1)
            tree[name] = meta
    for name in files:
        if not tree.get(name, "").startswith("100644 blob "):
            raise MergeError(f"派生物整组无法取当前侧：{name} 不存在或不是普通文件")
    print(f"派生物冲突 {len(entries)} 个；整组 {len(files)} 个文件统一取合并前当前分支：", flush=True)
    for name in sorted(entries):
        print(f"  {name}", flush=True)
    # NUL-delimited exact pathspecs avoid Windows' command-line length limit.
    git("--literal-pathspecs", "restore", f"--source={before}", "--staged", "--worktree",
        "--pathspec-from-file=-", "--pathspec-file-nul", input="\0".join(files) + "\0")
    if git("ls-files", "--unmerged").stdout:
        raise MergeError("仍有未解决的冲突；停止，不运行生成")
    note_stale_derived(files)
    return set(entries)


def main(argv: list[str] | None = None) -> int:
    """
    合并、解纯派生物冲突、快速结构检查后提交

    不 fetch、不 push

    合并路径只留快速结构检查；派生物由后台验证补齐（见 `regenerate`）。

    不跑测试、不 push；

    快速检查保留：Git 操作状态与干净工作树、非空生成/验证命令配置、产物报告格式与
    生成步骤一致、报告内路径安全且普通文件存在，以及改动 JSON/Python 的解析/语法。
    它们挡住不可读取的输入和破损产物清单；不证明产物新鲜或语义正确。

    合并可能更新工作流代码，冲突处理后用新进程执行
    `shiftboss merge --finish <合并前HEAD>`，读取新代码做快速检查并提交。

    失败保留现场，不自动回滚，不报告成功。已有另一进程的合并锁不能被失败入口删除。
    """
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ref", nargs="?", help="合入当前分支的单个 ref（先自行 fetch）")
    ap.add_argument("--finish", metavar="BEFORE", help="用新进程检查并提交已经解决冲突的合并")
    ap.add_argument("--regen", metavar="BEFORE",
                    help="合并时生成失败、修好后重试：按 BEFORE..HEAD 的改动重新生成并快进并入派生物")
    ap.add_argument("--done", metavar="TASKBOOK",
                    help="核对认定该任务书结束（做完或已拆分）：关会话并归档；不合并")
    ap.add_argument("--superseded-by", metavar="TASKBOOK",
                    help="与 --done 同用：余下的活已由该任务书接手，执行状态为「进展」也收")
    args = ap.parse_args(argv)
    from shiftboss.state import require_dispatcher
    require_dispatcher()
    holding = False
    try:
        if args.done:
            preflight()
            acquire_lock(f"done:{args.done}")
            holding = True
            if args.superseded_by:
                print(f"{args.done}：余下的活由 {args.superseded_by} 接手。", flush=True)
            finish_line(args.done, superseded_by=args.superseded_by)
            return 0
        if args.regen:
            preflight()
            acquire_lock("regen")
            holding = True
            _, summary = regenerate(args.regen)
            print(f"{summary}；未推送。", flush=True)
            return 0
        if args.finish:
            # The parent owns the lock; a manual recovery owns its own lock.
            if not os.environ.get("MM_WORKFLOW_MERGE"):
                acquire_lock("finish")
                holding = True
            finish(args.finish)
            clear_merge_failed(REPO)
            return 0
        if not args.ref:
            ap.error("需要 ref、--finish BEFORE 或 --regen BEFORE")
        preflight()
        clear_merge_failed(REPO)
        acquire_lock(args.ref)
        holding = True
        files = derived_files()
        before = git("rev-parse", "HEAD").stdout.strip()
        target = git("rev-parse", "--verify", "--end-of-options", f"{args.ref}^{{commit}}").stdout.strip()
        # 没 fetch 时远端跟踪 ref 可能还停在核对退回的旧 tip 上；退回过的提交一律不合。
        from shiftboss.inbox import load_rejected, origin_git_dir
        try:
            rejected = load_rejected(origin_git_dir(Path.cwd()))
        except SystemExit:
            rejected = {}  # 没有本机 origin 就没有收件箱，也就没有退回记录
        if any(target in tips for tips in rejected.values()):
            raise MergeError(f"{args.ref} 指向核对时已退回的 {target[:12]}；先 fetch 取到新 tip 再合")
        if moving := moving_branch_reason(args.ref, target):
            raise MergeError(moving)
        print(f"合并 {args.ref} ({target[:12]})：", flush=True)
        merged = git("-c", "rerere.enabled=false", "-c", "merge.autoStash=false",
                     "merge", "--no-ff", "--no-commit", "--no-edit", target, check=False)
        show(merged)
        if merged.returncode and not git("ls-files", "--unmerged").stdout:
            raise MergeError(f"git merge 失败（退出码 {merged.returncode}）")
        try:
            resolve_derived(files, before)
        except MergeError:
            # 派生物之外的冲突一律撤回，打回执行方合入 main 后重交（README 2.3）；派活方不解决冲突。
            unmerged = sorted({line.split("\t", 1)[1] for line in
                               git("ls-files", "--unmerged").stdout.splitlines() if "\t" in line})
            if not unmerged:
                raise
            git("merge", "--abort")
            name = args.ref.rsplit("/", 1)[-1] if not re.fullmatch(r"[0-9a-f]{7,40}", args.ref) else "<名字>"
            raise MergeError(
                f"合并冲突 {len(unmerged)} 个，已撤回（git merge --abort）：\n  " + "\n  ".join(unmerged) +
                f"\n下一步：打回该线重新合并——在 shiftboss/plans/{name}/rounds.md 追加本轮交代"
                "（合入 origin/main、解决上列冲突、受影响的判据在合入后重跑），"
                f"提交推送后 `shiftboss dispatch {name} -m ...`；冲突不算返工，不用 --rejected 与 --rework。") from None
        if git("rev-parse", "--verify", "--quiet", "MERGE_HEAD", check=False).returncode:
            print("目标已合入；未生成、未跑测试。", flush=True)
            close_merged_sessions(target)
            return 0
        # A merge may replace this module, its dependencies or the configuration.
        # Never keep evaluating the pre-merge implementation against the new tree.
        result = subprocess.run([sys.executable, "-B", "-m", "shiftboss.merge",
                                 "--finish", before], cwd=REPO)
        if result.returncode:
            raise MergeError(f"合并后快速检查/提交失败（退出码 {result.returncode}）")
        return 0
    except Exception as exc:
        print(f"[合并失败] {exc}", file=sys.stderr, flush=True)
        report_failure_state()
        return 1
    finally:
        if holding:
            release_lock()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    sys.stderr.reconfigure(encoding="utf-8", newline="\n")
    raise SystemExit(main())

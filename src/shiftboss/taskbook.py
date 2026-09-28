#!/usr/bin/env python3
"""任务书的布局。**只有这一个模块知道任务书长什么样，别处一律走它。**

**为什么要有这个模块**：任务书按「谁写」本来就分三块，但物理上挤在同一个
Markdown 命名空间里，于是机器要从排版里**猜**边界——而且是各猜各的：
`dispatch` 用「前 30 行的表格行」拿表头、用「HTML 标记＋章节层级校验＋一套
存量回退」框轮次区，`inbox` 扫勾号加关键词读状态，`compaction` 数标题正则。
**四套启发式没有一套知道另外三套的存在**，改动任何一块排版都可能只打断其中一套，
而报错要等到下一次有人碰这份文件。

**新布局按访问权分，一块一个文件**（用户 2026-09-12 定的方向）：

    shiftboss/plans/<name>/
      meta.json     机器事实（副本、分支、状态、交付模式、来源、轮次号）
                    **只经入口改**，两边都不手写
      contract.md   甲方读写 / 乙方只读
      rounds.md     甲方追加 / 乙方只读     ← 压缩压的就是这一个文件
      delivery.md   乙方读写 / 甲方只读     ← 每轮重写

**边界不再需要算：文件就是边界。** 于是那四套启发式在新布局下一条都用不上，
两边也不再写同一个文件。

**「双方永远只读」那一层不进任务书**——它每份都一样，指向 `shiftboss/README.md`
就够；每份抄一遍只会让它们互相漂移。

**存量布局（单文件 `shiftboss/plans/<name>.md`）继续支持**，两种并存：
在办的线各自在「空闲的那一刻」迁，262 份归档不迁——它们只会被偶尔读一次表头。
"""

from __future__ import annotations
from shiftboss.paths import PLANS_DIR, project_root

import json
import re
from pathlib import Path

META = "meta.json"
CONTRACT = "contract.md"
ROUNDS = "rounds.md"
DELIVERY = "delivery.md"

# 新布局里轮次记录的标题级别。**rounds.md 整份就是轮次区**，
# 没有外层章节可撞，所以固定成 1（`# 第 N 轮…`）下面的 2 级。
ROUND_LEVEL = 2

HEADER_CELL = re.compile(r"^\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$")


def _cell(value: str) -> str:
    """表头格里常写成 `在办`（长期，多轮） —— 只取反引号里的那个词。"""
    quoted = re.match(r"\s*`([^`]+)`", value)
    return quoted.group(1) if quoted else value.strip("` ")


# 表头里派活方写的「调度状态」与交回顶部执行方写的「执行状态」对称（README 2.4）。
# 存量表头的键叫「状态」，读写时都归一到「调度状态」。
DISPATCH_STATUS = "调度状态"
LEGACY_DISPATCH_STATUS = "状态"


def normalize_meta(meta: dict) -> dict:
    """存量键「状态」读成「调度状态」；调用方只认新键。"""
    if meta is not None and LEGACY_DISPATCH_STATUS in meta:
        legacy = meta.pop(LEGACY_DISPATCH_STATUS)
        meta.setdefault(DISPATCH_STATUS, legacy)
    return meta


def parse_header(text: str) -> dict[str, str]:
    """存量布局：从正文前 30 行的表格里读表头。**新布局不用这个，读 meta.json。**"""
    cells = {}
    for line in text.splitlines()[:30]:
        m = HEADER_CELL.match(line)
        if m and m.group(1) not in ("项", "---"):
            cells[m.group(1)] = _cell(m.group(2))
    return cells


class Taskbook:
    """一份任务书，两种布局共一个接口。

    **调用方不该问「这份是哪种布局」**——问了就会在别处再长出一套判断，
    那正是这次重构要消灭的东西。需要什么就调对应的方法。
    """

    def __init__(self, name: str, path: Path, *, is_dir: bool, archived: bool):
        self.name = name
        self.path = path          # 目录布局是目录，单文件布局是那个 .md
        self.is_dir = is_dir
        self.archived = archived

    # ---- 定位 ----

    @classmethod
    def find(cls, repo: Path, name: str) -> "Taskbook | None":
        """在办的先找，`archive/` 再找。

        **归档过的也要找得到**：`--close` 恰恰是最该收摊的时候，
        而那时文件已经挪进 `archive/`。
        """
        plans = repo / PLANS_DIR
        for base, archived in ((plans, False), (plans / "archive", True)):
            directory = base / name
            if (directory / META).exists():
                return cls(name, directory, is_dir=True, archived=archived)
            single = base / f"{name}.md"
            if single.exists():
                return cls(name, single, is_dir=False, archived=archived)
        return None

    @classmethod
    def iter_active(cls, repo: Path):
        """在办的那些（不含 `archive/`）。"""
        plans = repo / PLANS_DIR
        if not plans.exists():
            return
        for entry in sorted(plans.iterdir()):
            if entry.is_dir() and entry.name != "archive" and (entry / META).exists():
                yield cls(entry.name, entry, is_dir=True, archived=False)
            elif entry.is_file() and entry.suffix == ".md":
                yield cls(entry.stem, entry, is_dir=False, archived=False)

    # ---- 读 ----

    def meta(self) -> dict[str, str]:
        if self.is_dir:
            return normalize_meta(json.loads((self.path / META).read_text(encoding="utf-8")))
        return normalize_meta(parse_header(self.path.read_text(encoding="utf-8")))

    def contract_text(self) -> str:
        return (self.path / CONTRACT).read_text(encoding="utf-8") if self.is_dir \
            else self.path.read_text(encoding="utf-8")

    def rounds_text(self) -> str:
        if self.is_dir:
            p = self.path / ROUNDS
            return p.read_text(encoding="utf-8") if p.exists() else ""
        from shiftboss.dispatch import rounds_region
        text = self.path.read_text(encoding="utf-8")
        start, end, _level = rounds_region(text)
        return text[start:end]

    def delivery_text(self) -> str:
        if self.is_dir:
            p = self.path / DELIVERY
            return p.read_text(encoding="utf-8") if p.exists() else ""
        return self.path.read_text(encoding="utf-8")

    def status_text(self) -> str:
        """逐条状态那些勾在哪儿。**目前横跨两块，所以两块都给。**

        **这是新布局暴露出来、还没收干净的一处共写区**：合同第 2 节每条底下带一行
        `- 状态：☐ 未做 ☑ 完成 ☐ 待议`，条目是甲方写的，勾是乙方打的——
        于是 `contract.md`（甲方的文件）里有一处归乙方写。
        **正确的落点是 delivery.md 里一张按条目 id 索引的状态表**，
        写回线与战斗消息线的交回其实早就这么写了；换过去要同时改
        `TEMPLATE.md` 与执行方那一侧的读法，所以先并着读，别让计数在迁移当天变哑。
        """
        return self.contract_text() + "\n" + self.delivery_text() if self.is_dir \
            else self.delivery_text()

    def round_level(self) -> int:
        """轮次记录标题该用几级。"""
        if self.is_dir:
            return ROUND_LEVEL
        from shiftboss.dispatch import rounds_region
        return rounds_region(self.path.read_text(encoding="utf-8"))[2] + 1

    # ---- 写 ----

    def set_meta(self, **fields) -> None:
        """**只有入口改 meta.json**；两边都不手写它。"""
        if not self.is_dir:
            raise ValueError(f"{self.name} 还是存量单文件布局，表头请改正文表格")
        data = self.meta()
        data.update(normalize_meta({k: v for k, v in fields.items() if v is not None}))
        (self.path / META).write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def append_round(self, heading: str, body: str) -> None:
        if self.is_dir:
            p = self.path / ROUNDS
            old = p.read_text(encoding="utf-8") if p.exists() else ""
            p.write_text(old.rstrip("\n") + ("\n\n" if old.strip() else "")
                         + f"{heading}\n\n{body.rstrip()}\n", encoding="utf-8")
            return
        from shiftboss.dispatch import rounds_region
        text = self.path.read_text(encoding="utf-8")
        _start, end, _level = rounds_region(text)
        self.path.write_text(text[:end].rstrip("\n") + "\n\n"
                             + f"{heading}\n\n{body.rstrip()}\n\n" + text[end:],
                             encoding="utf-8")

    # ---- 给 git 用 ----

    def rel(self, repo: Path) -> str:
        """`git add` / `git commit --only --` 用的路径；目录布局给目录。"""
        return self.path.relative_to(repo).as_posix()


# **子类型的骨架在 `shiftboss/templates/<kind>/`**：`meta.json` 是这一类固定的格，
# `contract.md` 是这一类的合同（含「派发与交付」条款）。`new` 从那里拷，骨架只有一份。
# **新增一类＝加一个目录**，这里按目录现数，不写死。
# 骨架取自包内源，不取项目里装的那份：新建任务书不依赖项目文档是否已同步。
TEMPLATES = Path(__file__).resolve().with_name("docs") / "templates"
KINDS = tuple(sorted(d.name for d in TEMPLATES.iterdir()
                     if (d / META).is_file() and (d / CONTRACT).is_file()))

SCAFFOLD_ROUNDS = """# 轮次记录

> 这一节由派活方写，执行方只读。**末尾那一条就是这一轮的交代，照读。**
> 轮次上限与拆分见 [README](../../README.md) 2.4。
"""

EXECUTION_STATUSES = ("进展", "完成", "停下")


def execution_status(text: str | None) -> str | None:
    """交回顶部执行方自报的执行状态（README 2.4）；没写或写错给 None。

    与表头的调度状态对称。存量交回写作「信号：」「交付信号：」，一并认。
    """
    match = re.search(r"^(?:执行状态|交付信号|信号)[:：]\s*(\S+)\s*$", text or "", re.M)
    return match.group(1) if match and match.group(1) in EXECUTION_STATUSES else None


SCAFFOLD_DELIVERY = """# 执行方交回

> 这一份**归执行方写，派活方只读**，每轮重写。

执行状态：进展

## 逐条状态

| 条目 | 状态 | 说明 |
| --- | --- | --- |
| 2.1 | | |

## 做成了什么

## 被什么挡住

## 与任务书的偏差

## 文档冲突
"""


def new(repo: Path, name: str, *, copy: str, kind: str, branch: str = "") -> str:
    """按子类型搭一份新任务书的目录骨架。**新建走这里，不要手抄模板。**

    **子类型必须选**（`task`／`repair`／`conflict`）：它只决定合同骨架，
    生命周期对所有任务书相同（README 2.4）。

    手抄的问题不是麻烦，是四个文件里只要有一个忘了建，报错要等到第一次派活；
    而 `meta.json` 少一格，`dispatch` 只会说「表头缺工作副本」，不会说少了哪一格。
    """
    if kind not in KINDS:
        raise ValueError(f"子类型只能是 {' / '.join(KINDS)}；选哪种见 shiftboss/TEMPLATE.md 开头那张表")
    # --copy 是执行方工作副本目录，不是合同草稿；合同正文写进建好的 contract.md。
    if not Path(copy).is_dir():
        raise ValueError(f"--copy 须是已存在的执行方工作副本目录，收到的是 {copy}")
    template = TEMPLATES / kind
    directory = repo / PLANS_DIR / name
    if directory.exists() or (repo / PLANS_DIR / f"{name}.md").exists():
        raise ValueError(f"{name} 已经存在")
    meta = {
        DISPATCH_STATUS: "编写中",
        "阻塞于": "—",
        "编写时间": __import__("datetime").date.today().isoformat(),
        "分支": branch or f"codex/{name}",
        "你的工作副本": copy,
        "轮次": 0,
    }
    meta.update(json.loads((template / META).read_text(encoding="utf-8")))
    directory.mkdir(parents=True)
    (directory / META).write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    contract = (template / CONTRACT).read_text(encoding="utf-8").replace("{name}", name)
    (directory / CONTRACT).write_text(contract, encoding="utf-8")
    (directory / ROUNDS).write_text(SCAFFOLD_ROUNDS, encoding="utf-8")
    (directory / DELIVERY).write_text(SCAFFOLD_DELIVERY, encoding="utf-8")
    return (f"已按「{kind}」建 {PLANS_DIR}/{name}/：{META} / {CONTRACT} / {ROUNDS} / {DELIVERY}\n"
            f"状态是「编写中」——写完合同再改成「待派」才派得出去。")


def migrate(repo: Path, name: str, *, apply: bool = False) -> str:
    """把一份存量单文件任务书拆成目录布局。**一次性动作，拆完那些启发式就退休了。**

    **只在这条线空闲的那一刻做**——也就是我要派下一轮、正准备改它的时候。
    它干活干到一半时换掉脚下的文件，它下一次合 `origin/main` 会看见
    「文件没了、多出一个目录」，而它手上还改着那个文件。

    切法本身仍要用一次章节启发式（表头表格、轮次区、交回块），
    **但这是最后一次**：切完之后边界是文件，不再有人去猜。
    所以它默认只打印将要怎么切，`--apply` 才落盘。
    """
    from shiftboss.dispatch import markdown_headings, rounds_region
    book = Taskbook.find(repo, name)
    if book is None:
        raise ValueError(f"没有这份任务书：{name}")
    if book.is_dir:
        raise ValueError(f"{name} 已经是目录布局")
    if book.archived:
        raise ValueError(f"{name} 在 archive/ 里；归档的不迁，只给只读兼容")

    text = book.path.read_text(encoding="utf-8")
    headings = markdown_headings(text)
    start, end, level = rounds_region(text)
    owner = max((h for h in headings if h[1] <= start), key=lambda h: h[0])

    def section(pos: int, depth: int) -> tuple[int, int]:
        """从这个标题到下一个同级或更浅的标题；到头就是文末。"""
        return pos, next((p for p, s, d, t in headings if p > pos and d <= depth), len(text))

    # **交回块在哪一节，两种写法都有**：模板是「轮次在前、交回在后」的
    # `## 二 执行方交回`，也有写成「3. 执行方回填 / 4. 轮次记录」的——交回在前。
    # 所以按标题找，不按先后找。
    hit = next((h for h in headings if h[2] <= 3
                and any(w in h[3] for w in ("执行方交回", "执行方回填", "交回"))), None)
    if hit is None:
        raise ValueError(f"{name} 找不到交回块（标题里没有「执行方交回／执行方回填／交回」）")
    d_start, d_end = section(hit[0], hit[2])
    r_start, r_end = section(owner[0], owner[2])

    meta = parse_header(text)
    meta["轮次"] = max((int(m[1]) for pos, stop, depth, title in headings
                      if start <= pos < end and depth == level + 1
                      and (m := re.match(r"第\s*(\d+)\s*轮(?:\D|$)", title))), default=0)

    # 合同 = 整份减去轮次那一节、交回那一节，再减去表头表格：
    # **表头现在的唯一出处是 meta.json**，留一份副本就是留一份会漂移的断言。
    cuts = sorted([(r_start, r_end), (d_start, d_end)])
    kept, cursor = [], 0
    for a, b in cuts:
        kept.append(text[cursor:a])
        cursor = b
    kept.append(text[cursor:])
    contract = "\n".join(
        line for line in "".join(kept).splitlines()
        if not (HEADER_CELL.match(line) or re.fullmatch(r"\|[\s|:-]+\|", line))
    ).rstrip("\n") + "\n"

    # 轮次标题整体挪到 ROUND_LEVEL；区里更深的标题跟着挪同样的量。
    shift = ROUND_LEVEL - (level + 1)
    rounds = "\n".join(
        (("#" * max(2, len(m[1]) + shift) + " " + m[2]) if (m := re.match(r"^(#{1,6})\s+(.+?)\s*$", line))
         else line)
        for line in text[start:end].splitlines()).strip("\n") + "\n"

    delivery = text[d_start:d_end].rstrip("\n") + "\n"

    plan = (f"{name}：单文件 {len(text)} 字符 →\n"
            f"  {META}      {len(meta)} 个字段（轮次={meta['轮次']}，"
            f"调度状态={meta.get(DISPATCH_STATUS, '?')}）\n"
            f"  {CONTRACT}  {len(contract)} 字符（表头表格已移入 meta.json）\n"
            f"  {ROUNDS}    {len(rounds)} 字符（轮次标题 {level + 1} 级 → {ROUND_LEVEL} 级）\n"
            f"  {DELIVERY}  {len(delivery)} 字符")
    if not apply:
        return plan + "\n（未落盘；加 --apply 才真切）"

    directory = book.path.parent / name
    directory.mkdir()
    (directory / META).write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")
    (directory / CONTRACT).write_text(contract, encoding="utf-8")
    (directory / ROUNDS).write_text(rounds, encoding="utf-8")
    (directory / DELIVERY).write_text(delivery, encoding="utf-8")
    book.path.unlink()
    return plan + f"\n已切到 {directory.relative_to(repo).as_posix()}/"


def from_ref(read, name: str):
    """从 git ref 上读一份任务书（`inbox` / `verify` 走这条，它们不看工作树）。

    `read(rel)` 读一个仓库相对路径，读不到返回 None/空。
    → `(meta, delivery_text)`；两种布局都认，找不到返回 `(None, None)`。
    """
    for base in (PLANS_DIR, f"{PLANS_DIR}/archive"):
        raw = read(f"{base}/{name}/{META}")
        if raw:
            # 逐条状态的勾目前横跨合同与交回两块（见 Taskbook.status_text），两块都给。
            body = "\n".join((read(f"{base}/{name}/{CONTRACT}") or "",
                              read(f"{base}/{name}/{DELIVERY}") or ""))
            try:
                return normalize_meta(json.loads(raw)), body
            except ValueError:
                return None, None
        text = read(f"{base}/{name}.md")
        if text:
            return parse_header(text), text
    return None, None


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["new", "set", "migrate", "show"])
    ap.add_argument("name")
    ap.add_argument("--field", action="append", default=[], metavar="键=值",
                    help="set：改 meta.json 的一格；可重复。**这是改它的唯一入口**")
    ap.add_argument("--copy", default="", help="new：工作副本绝对路径")
    ap.add_argument("--branch", default="", help="new：分支名，默认 codex/<名字>")
    ap.add_argument("--kind", choices=KINDS, default=None,
                    help="new：子类型（shiftboss/templates/ 下的目录名）；见 TEMPLATE.md 开头那张表")
    ap.add_argument("--apply", action="store_true", help="migrate：真切；不加只打印怎么切")
    args = ap.parse_args(argv)
    repo = project_root()
    if args.command == "new":
        if not args.copy:
            print("new 要 --copy <工作副本绝对路径>：那一格只有派活方给得出")
            return 2
        if not args.kind:
            print(f"new 要 --kind {'|'.join(KINDS)}：选哪种见 shiftboss/TEMPLATE.md 开头那张表")
            return 2
        print(new(repo, args.name, copy=args.copy, kind=args.kind, branch=args.branch))
        pairs = dict(f.split("=", 1) for f in args.field if "=" in f)
        if pairs:
            Taskbook.find(repo, args.name).set_meta(**pairs)
            print("表头：" + "；".join(f"{k} → {v}" for k, v in pairs.items()))
        return 0
    if args.command == "set":
        book = Taskbook.find(repo, args.name)
        if book is None:
            print(f"没有这份任务书：{args.name}")
            return 2
        pairs = dict(f.split("=", 1) for f in args.field if "=" in f)
        if not pairs:
            print("set 要 --field 键=值")
            return 2
        book.set_meta(**pairs)
        print(f"{args.name}：" + "；".join(f"{k} → {v}" for k, v in pairs.items()))
        return 0
    if args.command == "migrate":
        print(migrate(repo, args.name, apply=args.apply))
        return 0
    book = Taskbook.find(repo, args.name)
    if book is None:
        print(f"没有这份任务书：{args.name}")
        return 2
    print(f"{book.name}  布局={'目录' if book.is_dir else '单文件'}  "
          f"归档={book.archived}  轮次标题级别={book.round_level()}")
    print(json.dumps(book.meta(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    raise SystemExit(main())

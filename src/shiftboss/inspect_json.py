"""有界查看 JSON；完整读取与引用校验复用项目输出协议，不截断原件。"""
from __future__ import annotations

import argparse
from itertools import islice
import json
from pathlib import Path
import sys

from shiftboss.paths import project_root
from shiftboss.process_output import read_log


def short(text: str, limit: int = 240):
    return text if len(text) <= limit else {"text": text[:limit], "characters": len(text), "omitted": len(text) - limit}


def preview(value, depth: int = 1):
    if isinstance(value, dict):
        keys = list(islice(value, 8))
        result = {"type": "object", "length": len(value), "keys": [short(key, 80) for key in keys]}
        if depth:
            result["entries"] = [{"key": short(key, 80), "value": preview(value[key], depth - 1)} for key in keys]
        return result
    if isinstance(value, list):
        return {"type": "array", "length": len(value),
                "sample": [preview(item, max(0, depth - 1)) for item in islice(value, 3)] if depth else [],
                "element_types": sorted({type(item).__name__ for item in islice(value, 12)})}
    return short(value) if isinstance(value, str) else value


def pointer(value, address: str):
    if not address:
        return value
    if not address.startswith("/"):
        raise ValueError("--pointer 须为 JSON Pointer（例如 /result/status）")
    for raw in address[1:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            value = value[key]
        elif isinstance(value, list) and key.isdecimal():
            value = value[int(key)]
        else:
            raise ValueError(f"无法选择 {short(key, 80)}；当前值为 {type(value).__name__}")
    return value


def stage(value, case: str | None, script: str | None):
    """按真实 command 定位，绝不把 gate 序号当作 case 身份。"""
    entries = value.get("stages") if isinstance(value, dict) else None
    if not isinstance(entries, (dict, list)):
        raise ValueError("报告没有 stages 对象或数组")
    entries = entries.items() if isinstance(entries, dict) else enumerate(entries)
    found = []
    for name, row in entries:
        command = row.get("command") if isinstance(row, dict) else None
        if not isinstance(command, list) or not all(isinstance(arg, str) for arg in command):
            continue
        if case and not any(command[i:i + 2] == ["--case", case] for i in range(len(command) - 1)):
            continue
        if script and not any(arg == script or arg.endswith("/" + script) for arg in command):
            continue
        found.append({"stage": name, **row})
    if len(found) != 1:
        raise ValueError(f"真实命令匹配 {len(found)} 项，拒绝猜测阶段")
    return found[0]


def render(value, *, source: str, depth: int = 1, limit: int = 4000) -> str:
    result = {"inspection_only": True, "source": short(source), "preview": preview(value, depth)}
    text = json.dumps(result, ensure_ascii=False)
    if len(text.encode("utf-8")) > limit:
        result = {"inspection_only": True, "source": short(source, 80),
                  "type": type(value).__name__, "length": len(value) if isinstance(value, (str, list, dict)) else None,
                  "preview_omitted": True, "reason": "preview exceeds output byte limit; select a narrower pointer"}
        text = json.dumps(result, ensure_ascii=False)
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--pointer", default="", help="JSON Pointer；阶段筛选后再应用")
    parser.add_argument("--case", help="按 stages.command 中的真实 --case 参数选择")
    parser.add_argument("--script", help="按 stages.command 中的真实脚本路径选择")
    parser.add_argument("--expect-json", help="精确比较所选字段；不相等退出 1，读取/选择错误退出 2")
    parser.add_argument("--depth", type=int, choices=range(4), default=1)
    parser.add_argument("--max-bytes", type=int, default=4000)
    args = parser.parse_args(argv)
    if not 1024 <= args.max_bytes <= 16000:
        parser.error("--max-bytes 范围为 1024..16000")
    try:
        value = json.loads(read_log(args.path, project_root()))
        if args.case or args.script:
            value = stage(value, args.case, args.script)
        value = pointer(value, args.pointer)
        if args.expect_json is not None:
            expected = json.loads(args.expect_json)
            matches = json.dumps(value, sort_keys=True) == json.dumps(expected, sort_keys=True)
            print(render({"actual": value, "expected": expected, "matches": matches},
                         source=str(args.path), depth=args.depth, limit=args.max_bytes))
            return 0 if matches else 1
        print(render(value, source=str(args.path), depth=args.depth, limit=args.max_bytes))
        return 0
    except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
        print(render({"error": str(error)}, source=str(args.path), limit=args.max_bytes), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

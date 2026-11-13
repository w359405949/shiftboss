"""长期写权边界：业务代码开放，检查代码按任务声明授权。"""

import re


CHECKS_KEY = "改检查"


def check_paths(settings: dict) -> list[str]:
    """复用项目登记识别检查，不以业务目录或任务路径划分权限。"""
    merge, verify = settings.get("merge", {}), settings.get("verify", {})
    paths = {arg for item in merge.get("fast_commands", []) for arg in item.get("run", [])
             if "/" in arg and re.search(r"\.(?:[cm]?[jt]s|py)$", arg)}
    paths.update(entry.split("::", 1)[0] for entry in verify.get("approved_tests", []) if "/" in entry)
    paths.update(d.rstrip("/") + "/" for d in merge.get("assertion_dirs", []))
    paths.update(verify.get("check_paths", []))
    paths.update(settings.get("guard", {}).get("layout_paths", []))
    # 逻辑 JS 路径与迁移后的 TS 源码属于同一检查入口。
    for path in list(paths):
        for js, ts in ((".js", ".ts"), (".mjs", ".mts"), (".cjs", ".cts")):
            if path.endswith(js):
                paths.add(path[:-len(js)] + ts)
    return sorted(paths)


def check_hits(paths: list[str], settings: dict) -> list[str]:
    guarded = check_paths(settings)
    return [path for path in paths if (
        any(path == p or (p.endswith("/") and path.startswith(p)) for p in guarded)
        or any(part in ("test", "tests", "__tests__") for part in path.split("/")[:-1])
        or re.search(r"(^|/)[^/]*baseline[^/]*\.json$", path))]

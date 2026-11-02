"""项目声明输出表示，工具只负责加载、排空管道与调用完整读取协议。

未声明适配器的项目保留普通日志；声明后缺失或不兼容均报错，不能退回
原始读取把引用当业务结果。表示格式与归档引用的解析由项目适配器拥有。
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
from functools import lru_cache

from shiftboss.paths import project_root
from shiftboss import config

API = "shiftboss.output-adapter.v1"


@lru_cache(maxsize=16)
def _load(path: Path, digest: str):
    name = "_shiftboss_output_" + hashlib.sha256(os.fsencode(path) + digest.encode()).hexdigest()
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # 执行刚核对的源码，避免同大小、同秒更新被 Python 的 pyc 命中旧版本。
    source = path.read_bytes()
    if hashlib.sha256(source).hexdigest() != digest:
        raise ValueError(f"输出适配器加载期间改变：{path}")
    sys.modules[name] = module
    exec(compile(source, str(path), "exec"), module.__dict__)
    if (getattr(module, "OUTPUT_ADAPTER_API", None) != API
            or not all(callable(getattr(module, key, None))
                       for key in ("BoundedWriter", "read_text", "expand_output"))):
        raise ValueError(f"不兼容的输出适配器：{path}")
    return module


def adapter(root: Path | None = None):
    root = (root or project_root()).resolve()
    name = config.output_adapter(root)
    if name is None:
        return None
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise ValueError("verify.output_adapter 必须是项目内相对路径")
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise ValueError("verify.output_adapter 越过项目根")
    return _load(path, hashlib.sha256(path.read_bytes()).hexdigest())


def expand(text: str, root: Path | None = None, *, path_map: dict | None = None) -> str:
    provider = adapter(root)
    if not provider:
        if path_map:
            raise ValueError("没有输出适配器，不能解析来源映射")
        return text
    return provider.expand_output(text, path_map=path_map) if path_map is not None else provider.expand_output(text)


def read_log(path: Path, root: Path | None = None, *, path_map: dict | None = None) -> str:
    provider = adapter(root)
    if provider:
        text = provider.read_text(path)
        return provider.expand_output(text, path_map=path_map) if path_map is not None else provider.expand_output(text)
    if path_map:
        raise ValueError("没有输出适配器，不能解析来源映射")
    return Path(path).read_text(encoding="utf-8", errors="replace")


class PlainWriter:
    def __init__(self, path):
        self.path = Path(path)

    def __enter__(self):
        self.handle = self.path.open("wb")
        return self

    def write(self, value):
        return self.handle.write(value.encode("utf-8") if isinstance(value, str) else value)

    def flush(self):
        self.handle.flush()

    def __exit__(self, kind, error, traceback):
        self.handle.close()


def writer(path: Path, root: Path):
    provider = adapter(root)
    return provider.BoundedWriter(path) if provider else PlainWriter(path)


def run(command: list[str], cwd: Path, output, env: dict, *, idle_limit: float | None,
        poll_interval: float, idle_message: str) -> int:
    """持续按字节排空；不以根清单尺寸判断输出，保留进程组结束语义。

    有界队列防止慢磁盘时把输出堆入内存。管道异常与存储错误原样上抛；
    结束时读到 EOF 才返回，不能先看到进程退出就丢弃排队的最后诊断。
    """
    output.flush()
    process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, env=env, start_new_session=True)
    chunks = queue.Queue(maxsize=8)
    stopped = threading.Event()

    def put(value):
        while not stopped.is_set():
            try:
                chunks.put(value, timeout=0.1)
                return
            except queue.Full:
                pass

    def drain():
        try:
            while data := process.stdout.read1(65536):
                put(data)
        except Exception as error:
            put(error)
        finally:
            put(None)

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    last_received, timed_out = time.monotonic(), False

    def kill():
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    try:
        while True:
            try:
                data = chunks.get(timeout=poll_interval)
            except queue.Empty:
                if idle_limit is not None and not timed_out and time.monotonic() - last_received > idle_limit:
                    kill()
                    timed_out = True
                continue
            if data is None:
                break
            if isinstance(data, Exception):
                raise data
            last_received = time.monotonic()
            # 兼容原 run_until_idle 的文本文件句柄，真实字节不逐块解码。
            target = output.buffer if isinstance(output, io.TextIOBase) and hasattr(output, "buffer") else output
            target.write(data)
            output.flush()
        code = process.wait()
        if timed_out:
            output.write(idle_message)
            return 124
        return code
    except BaseException:
        kill()
        process.wait()
        raise
    finally:
        stopped.set()
        process.stdout.close()
        thread.join(timeout=5)

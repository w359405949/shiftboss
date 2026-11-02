"""工具的协议边界和实际管道；不运行浏览器或产品构建。"""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from shiftboss import process_output as output


ADAPTER = '''from pathlib import Path
OUTPUT_ADAPTER_API = "shiftboss.output-adapter.v1"
class BoundedWriter:
    def __init__(self, path): self.path = Path(path)
    def __enter__(self):
        self.parts = []; self.index = 0
        self.path.write_text("fixture storage")
        return self
    def write(self, value):
        data = value.encode() if isinstance(value, str) else value
        for i in range(0, len(data), 1024):
            part = self.path.with_name(self.path.name + str(self.index))
            part.write_bytes(data[i:i+1024]); self.parts.append(part); self.index += 1
    def flush(self): pass
    def __exit__(self, *args):
        self.path.write_text("\\n".join(str(p) for p in self.parts))
def read_text(path):
    return b"".join(Path(p).read_bytes() for p in Path(path).read_text().splitlines()).decode()
def expand_output(text):
    return text.replace("fixture-reference", "restored reason")
'''


class OutputProtocol(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "capture.log"

    def install(self, text=ADAPTER):
        (self.root / "adapter.py").write_text(text)
        (self.root / "shiftboss.toml").write_text('[verify]\noutput_adapter = "adapter.py"\n')

    def test_legacy_logs_and_bytes_preserve_default(self):
        with output.writer(self.path, self.root) as stream:
            stream.write("header\n")
            stream.write("中文\n".encode())
        self.assertEqual(self.path.read_bytes(), "header\n中文\n".encode())
        self.assertEqual(output.read_log(self.path, self.root), "header\n中文\n")

    def test_declared_writer_reader_and_expansion_are_used(self):
        self.install()
        text = "中文" * 3000 + "\nfixture-reference\n"
        with output.writer(self.path, self.root) as stream:
            stream.write(text)
        self.assertLess(self.path.stat().st_size, 2000)
        self.assertEqual(output.read_log(self.path, self.root), text.replace("fixture-reference", "restored reason"))
        self.assertTrue(all(p.stat().st_size <= 1024 for p in self.root.glob("capture.log[0-9]*")))

    def test_configuration_and_same_size_source_changes_are_live(self):
        self.assertIsNone(output.adapter(self.root))
        self.install()
        self.assertEqual(output.expand("fixture-reference", self.root), "restored reason")
        stat = (self.root / "adapter.py").stat()
        changed = ADAPTER.replace("restored reason", "different value")
        self.assertEqual(len(changed), len(ADAPTER))
        (self.root / "adapter.py").write_text(changed)
        os.utime(self.root / "adapter.py", ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(output.expand("fixture-reference", self.root), "different value")

    def test_invalid_api_missing_file_and_escape_fail_instead_of_raw_fallback(self):
        self.install(ADAPTER.replace("adapter.v1", "adapter.v0"))
        with self.assertRaisesRegex(ValueError, "不兼容"):
            output.adapter(self.root)
        (self.root / "adapter.py").unlink()
        with self.assertRaises(FileNotFoundError):
            output.adapter(self.root)
        for name in ["../outside.py", "/absolute.py"]:
            (self.root / "shiftboss.toml").write_text('[verify]\noutput_adapter = ' + json.dumps(name) + '\n')
            with self.assertRaises(ValueError):
                output.adapter(self.root)

    def test_corrupt_reference_error_propagates(self):
        self.install(ADAPTER.replace('return text.replace("fixture-reference", "restored reason")',
                                     'raise ValueError("fixture digest mismatch")'))
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            output.expand("fixture-reference", self.root)

    def run_child(self, source, *, idle_limit=1, sink=None):
        sink = sink or io.BytesIO()
        code = output.run([sys.executable, "-u", "-c", source], self.root, sink, os.environ.copy(),
                          idle_limit=idle_limit, poll_interval=0.02, idle_message=b"idle\n")
        return code, sink

    def test_real_pipe_drained_after_exit_and_error_code_kept(self):
        body = ("中文\n" * 30000).encode()
        code, sink = self.run_child("import sys\nsys.stdout.buffer.write(('中文\\n'*30000).encode())\nsys.exit(7)")
        self.assertEqual(code, 7)
        self.assertEqual(hashlib.sha256(sink.getvalue()).digest(), hashlib.sha256(body).digest())

    def test_real_pipe_to_declared_split_writer(self):
        self.install()
        with output.writer(self.path, self.root) as stream:
            code, _ = self.run_child("import sys\nsys.stdout.buffer.write(b'x'*100000)", sink=stream)
        self.assertEqual(code, 0)
        self.assertEqual(output.read_log(self.path, self.root), 'x'*100000)

    def test_output_activity_prevents_idle_timeout(self):
        code, sink = self.run_child("import time\nfor i in range(8):\n print(i,flush=True)\n time.sleep(.04)", idle_limit=.2)
        self.assertEqual(code, 0)
        self.assertEqual(sink.getvalue(), b'0\n1\n2\n3\n4\n5\n6\n7\n')

    def test_idle_kills_and_keeps_last_diagnostic(self):
        started = time.monotonic()
        code, sink = self.run_child("import time\nprint('last diagnostic',flush=True)\ntime.sleep(30)", idle_limit=1)
        self.assertEqual(code, 124)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(sink.getvalue(), b'last diagnostic\nidle\n')

    def test_storage_error_is_same_exception_and_child_cleaned(self):
        error = OSError("fixture storage refused")
        class Rejected:
            def write(self, data): raise error
            def flush(self): pass
        with self.assertRaises(OSError) as caught:
            self.run_child("import time\nprint('diagnostic',flush=True)\ntime.sleep(30)", sink=Rejected())
        self.assertIs(caught.exception, error)

    def test_real_patrol_readers_expand_before_business_filters(self):
        from shiftboss import patrol
        conclusion = {'pages': [{'route': '/example'}], 'route': '/example', 'seconds': 3,
                      'returncode': 1, 'reasons': ['original failure'], 'metrics': {'ready_ms': 4}}
        self.install(ADAPTER.replace('"restored reason"', repr(json.dumps(conclusion))))
        instance = object.__new__(patrol.Patrol)
        instance.repo = instance.workspace = instance.remote = self.root
        instance.state = {'pass': 3}
        with output.writer(self.path, self.root) as stream:
            stream.write('prefix\nfixture-reference\n')
        self.assertEqual(patrol.reason_lines(self.path, self.root), ['original failure'])
        perf = self.root / 'perf.jsonl'
        with patch.object(patrol, 'perf_path', return_value=perf):
            instance.record_perf('/example', self.path, 'abc')
        record = json.loads(perf.read_text())
        self.assertEqual(record['seconds'], 3)
        self.assertEqual(record['metrics']['ready_ms'], 4)
        self.assertEqual(instance.routes([sys.executable, '-c', "print('fixture-reference')"]), ['/example'])
        self.install(ADAPTER.replace('return text.replace("fixture-reference", "restored reason")',
                                     'raise ValueError("fixture digest mismatch")'))
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            patrol.reason_lines(self.path, self.root)

    def test_actual_verifier_command_uses_declared_writer_and_keeps_exit(self):
        from shiftboss.verify import Verifier
        self.install()
        (self.root / 'logs').mkdir()
        instance = object.__new__(Verifier)
        instance.directory = instance.workspace = self.root
        instance.stage = lambda *args: None
        result = instance.command('abc', 'gate-1', ['python', '-c', "import sys; print('fixture-reference'); sys.exit(9)"])
        self.assertEqual(result['returncode'], 9)
        self.assertEqual(output.read_log(Path(result['log']), self.root), 'restored reason\n')

    def test_text_file_handle_keeps_split_utf8_bytes(self):
        from shiftboss import patrol
        with self.path.open('w', encoding='utf-8') as stream:
            stream.write('header\n')
            code = patrol.run_until_idle([sys.executable, '-c', "import sys; sys.stdout.buffer.write('中文'.encode())"],
                                         self.root, stream, os.environ.copy())
        self.assertEqual(code, 0)
        self.assertEqual(self.path.read_text(), 'header\n中文')

    def test_initial_storage_flush_failure_does_not_start_child(self):
        class Rejected:
            def flush(self): raise OSError('fixture refused before spawn')
        with patch.object(output.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(OSError, 'before spawn'):
                self.run_child('print(1)', sink=Rejected())
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()

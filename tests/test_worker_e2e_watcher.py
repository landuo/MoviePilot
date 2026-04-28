"""
mp-watcher 端到端验收测试

启动一个**真实编译的 mp-watcher 二进制子进程**，验证 Python ↔ Go 跨语言契约：
- /api/v1/health /api/v1/info（shared/transport 通用接口）
- /api/v1/configure /api/v1/watch_status（业务接口）
- 文件创建 → 回调推送 → Python HTTP server 收到正确字段的事件

为了让本测试在最小依赖（仅 httpx）下也能跑通，**不依赖 app.* 任何模块**，
直接用 httpx 与 mp-watcher 通信。WorkerClient 的端到端验证由
test_worker_integration.py（用 fake server 模拟 worker）覆盖。

如果本地未编译 mp-watcher 二进制（或平台为 Windows 不支持 UDS），自动 skip。

本地准备：
    cd workers && make build-watcher
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import List, Optional

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # 测试启动时用 skip 处理

REPO_ROOT = Path(__file__).resolve().parents[1]
# 与 workers/Makefile build-watcher 输出路径保持一致
WATCHER_BIN_CANDIDATES = [
    REPO_ROOT / "workers" / "bin" / "mp-watcher",
    Path("/tmp/mp-watcher"),  # 本地 P1-A 验证时构建到这里的兜底
]


def _find_watcher_binary() -> Optional[Path]:
    for p in WATCHER_BIN_CANDIDATES:
        if p.exists() and os.access(p, os.X_OK):
            return p
    return None


@unittest.skipIf(sys.platform == "win32",
                 "Unix Domain Socket 在 Windows 上不可用")
@unittest.skipIf(_find_watcher_binary() is None,
                 "未找到 mp-watcher 二进制，先 cd workers && make build-watcher")
@unittest.skipIf(httpx is None, "未安装 httpx，跳过 mp-watcher 端到端测试")
class WatcherEndToEndTest(unittest.TestCase):
    """
    每个用例独立起一个 mp-watcher 子进程，确保互不污染。
    """

    def setUp(self):
        self.bin_path = _find_watcher_binary()
        self.assertIsNotNone(self.bin_path)

        self.tmp_dir = Path(tempfile.mkdtemp(prefix="mp-e2e-watcher-"))
        self.socket_dir = self.tmp_dir / "sockets"
        self.socket_dir.mkdir()
        self.socket_path = self.socket_dir / "mp-watcher.sock"

        # 收到的回调事件（线程安全，主测试线程读取）
        self._callbacks: List[dict] = []
        self._callbacks_lock = threading.Lock()

        # 起一个 HTTP 回调 server 接收 mp-watcher 推送
        self._callback_server, self._callback_url = self._start_callback_server()

        # 启动 mp-watcher 子进程
        log_file = self.tmp_dir / "watcher.log"
        self._log_fp = open(log_file, "w", encoding="utf-8")
        self._proc = subprocess.Popen(
            [
                str(self.bin_path),
                f"--socket={self.socket_path}",
                f"--callback-url={self._callback_url}",
                "--log-format=text",
                "--log-level=debug",
            ],
            stdout=self._log_fp,
            stderr=subprocess.STDOUT,
        )

        # 等 socket 文件出现（最多 3 秒）
        if not self._wait_for_socket(timeout=3.0):
            self._dump_log_and_fail("mp-watcher 启动后 socket 文件未出现")

        # 准备 httpx UDS client
        transport = httpx.HTTPTransport(uds=str(self.socket_path))
        self.http = httpx.Client(transport=transport,
                                 base_url="http://mp-watcher",
                                 timeout=5)

    def tearDown(self):
        try:
            self.http.close()
        except Exception:
            pass

        # 优雅关 worker：先 /shutdown，给 1 秒；不行再 SIGTERM
        try:
            if self._proc.poll() is None:
                try:
                    transport = httpx.HTTPTransport(uds=str(self.socket_path))
                    with httpx.Client(transport=transport,
                                      base_url="http://x",
                                      timeout=2) as c:
                        c.post("/api/v1/shutdown")
                except Exception:
                    pass
                try:
                    self._proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._proc.terminate()
                    try:
                        self._proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self._proc.kill()
        finally:
            try:
                self._log_fp.close()
            except Exception:
                pass

        try:
            self._callback_server.shutdown()
            self._callback_server.server_close()
        except Exception:
            pass

        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # ---------- 测试辅助 ----------

    def _wait_for_socket(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.socket_path.exists():
                return True
            if self._proc.poll() is not None:
                return False  # 子进程已退出
            time.sleep(0.05)
        return False

    def _dump_log_and_fail(self, msg: str):
        try:
            self._log_fp.flush()
        except Exception:
            pass
        log_path = Path(self._log_fp.name)
        log_text = log_path.read_text(encoding="utf-8", errors="replace") \
            if log_path.exists() else "<no log>"
        self.fail(f"{msg}\n--- mp-watcher log ---\n{log_text}")

    def _start_callback_server(self):
        callbacks = self._callbacks
        callbacks_lock = self._callbacks_lock

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args, **kwargs):  # 静默
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except Exception:
                    payload = {"_raw": raw.decode("utf-8", errors="replace")}
                with callbacks_lock:
                    callbacks.append(payload)
                body = b'{"code":0,"message":"ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever,
                                  name="cb-server", daemon=True)
        thread.start()
        return server, f"http://127.0.0.1:{port}/api/v1/worker_callback/watcher"

    def _wait_callbacks(self, predicate, timeout: float = 3.0):
        """等待满足 predicate(callback) 的事件出现，返回首个匹配项。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._callbacks_lock:
                for cb in self._callbacks:
                    if predicate(cb):
                        return cb
            time.sleep(0.05)
        with self._callbacks_lock:
            snapshot = list(self._callbacks)
        self.fail(f"等待回调超时（{timeout}s），已收: {snapshot}")

    # ---------- 测试辅助：业务调用 ----------

    def _post(self, path: str, payload: Optional[dict] = None) -> dict:
        """POST 业务接口，断言响应 code=0，返回 data 字段。"""
        resp = self.http.post(path, json=payload or {})
        self.assertEqual(resp.status_code, 200,
                         f"{path} HTTP 状态非 200: {resp.text}")
        body = resp.json()
        self.assertEqual(body["code"], 0,
                         f"{path} 业务 code 非 0: {body}")
        return body.get("data") or {}

    def _get(self, path: str) -> dict:
        resp = self.http.get(path)
        self.assertEqual(resp.status_code, 200,
                         f"{path} HTTP 状态非 200: {resp.text}")
        return resp.json()

    # ---------- 用例 ----------

    def test_health_endpoint(self):
        """通用 /api/v1/health 端点（由 shared/transport 提供）"""
        body = self._get("/api/v1/health")
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["status"], "ok")

    def test_info_endpoint(self):
        """通用 /api/v1/info 端点：返回 worker name + version"""
        body = self._get("/api/v1/info")
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["name"], "mp-watcher")
        self.assertTrue(body["data"].get("version"))

    def test_configure_and_status(self):
        """端到端：configure 推目录 → watch_status 能查到"""
        watch_dir = self.tmp_dir / "data"
        watch_dir.mkdir()

        data = self._post("/api/v1/configure", {
            "watches": [
                {"watch_id": "w1", "path": str(watch_dir), "recursive": True},
            ],
        })
        self.assertEqual(data["added"], 1)
        self.assertEqual(data["total"], 1)

        body = self._get("/api/v1/watch_status")
        self.assertEqual(body["data"]["count"], 1)
        watches = body["data"]["watches"]
        self.assertEqual(watches[0]["watch_id"], "w1")
        # Go filepath.Abs 不解析 symlink，与 os.path.abspath 行为一致；
        # 注意不要用 Path.resolve()，那会展开 macOS 上的 /var → /private/var
        self.assertEqual(watches[0]["path"], os.path.abspath(str(watch_dir)))
        self.assertTrue(watches[0]["recursive"])

    def test_create_file_triggers_callback(self):
        """端到端：在被监听目录创建文件 → Python 回调 server 收到事件"""
        watch_dir = self.tmp_dir / "data2"
        watch_dir.mkdir()
        self._post("/api/v1/configure", {
            "watches": [{"watch_id": "wA", "path": str(watch_dir),
                         "recursive": False}],
        })

        # 给 fsnotify 真正订阅一点时间
        time.sleep(0.2)

        target = watch_dir / "movie.mkv"
        target.write_bytes(b"x" * 100)

        cb = self._wait_callbacks(
            lambda c: c.get("src_path") == str(target),
            timeout=3.0,
        )
        # 严格校验字段（Python ↔ Go schema 契约）
        self.assertEqual(cb["watch_id"], "wA")
        self.assertEqual(cb["storage"], "local")
        self.assertIn(cb["event_type"], ("created", "modified"))
        self.assertEqual(cb["is_directory"], False)
        self.assertGreater(cb["file_size"], 0)
        self.assertIsInstance(cb["event_id"], str)
        self.assertGreater(len(cb["event_id"]), 0)

    def test_configure_full_reset(self):
        """configure 是全量重置：第二次推送移除旧 watch_id"""
        d1 = self.tmp_dir / "d1"
        d2 = self.tmp_dir / "d2"
        d1.mkdir()
        d2.mkdir()

        self._post("/api/v1/configure", {
            "watches": [
                {"watch_id": "w1", "path": str(d1)},
                {"watch_id": "w2", "path": str(d2)},
            ],
        })
        # 第二次只保留 w1
        data = self._post("/api/v1/configure", {
            "watches": [{"watch_id": "w1", "path": str(d1)}],
        })
        self.assertEqual(data["added"], 0)
        self.assertEqual(data["removed"], 1)
        self.assertEqual(data["total"], 1)

    def test_configure_partial_failure_returns_warning(self):
        """部分目录不存在时：返回 200/0 + warning，不影响有效目录生效"""
        good = self.tmp_dir / "good"
        good.mkdir()
        bad = self.tmp_dir / "nonexistent-xyz"  # 不创建

        data = self._post("/api/v1/configure", {
            "watches": [
                {"watch_id": "ok", "path": str(good)},
                {"watch_id": "bad", "path": str(bad)},
            ],
        })
        self.assertEqual(data["added"], 1)
        self.assertIn("warning", data)
        self.assertTrue(data["warning"], "warning 字段不应为空")

    def test_recursive_watch_picks_up_new_subdir(self):
        """递归 watch 下，动态新建子目录中的文件也能被监听"""
        root = self.tmp_dir / "rec"
        root.mkdir()
        self._post("/api/v1/configure", {
            "watches": [{"watch_id": "wR", "path": str(root),
                         "recursive": True}],
        })
        time.sleep(0.2)

        sub = root / "season01"
        sub.mkdir()
        # 等子目录被动态加 watch
        self._wait_callbacks(
            lambda c: c.get("src_path") == str(sub) and c.get("is_directory"),
            timeout=3.0,
        )
        time.sleep(0.3)

        deep = sub / "ep01.mkv"
        deep.write_bytes(b"y" * 50)
        cb = self._wait_callbacks(
            lambda c: c.get("src_path") == str(deep),
            timeout=3.0,
        )
        self.assertEqual(cb["watch_id"], "wR")


if __name__ == "__main__":
    unittest.main()

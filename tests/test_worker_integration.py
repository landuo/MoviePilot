"""
WorkerClient 集成测试

不 mock httpx，起一个真实的 Unix Domain Socket HTTP server 模拟 worker，
验证 WorkerClient 与 worker 之间的端到端契约（含 /health、业务路径）。

如果运行平台不支持 UDS（如 Windows），这些用例会被跳过。
"""
import json
import shutil
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest import TestCase

from app.core.config import settings
from app.utils.worker_client import WorkerClient

@unittest.skipIf(sys.platform == "win32",
                 "Unix Domain Socket 在 Windows 上不可用")
class WorkerClientIntegrationTest(TestCase):

    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="mp-integ-test-"))
        self._orig = {
            "WORKER_MODE": settings.WORKER_MODE,
            "WORKER_SOCKET_DIR": settings.WORKER_SOCKET_DIR,
            "WORKER_RPC_TIMEOUT": settings.WORKER_RPC_TIMEOUT,
        }
        settings.WORKER_MODE = "worker"
        settings.WORKER_SOCKET_DIR = str(self.tmp_dir)
        settings.WORKER_RPC_TIMEOUT = 5

        self.client = WorkerClient("watcher")
        self.server = self._start_fake_worker(self.client.socket_path)

    def tearDown(self):
        try:
            self.client.close()
        finally:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
            for k, v in self._orig.items():
                setattr(settings, k, v)
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @staticmethod
    def _start_fake_worker(socket_path: Path):
        """
        起一个简单的 UDS HTTP server 模拟 worker：
        - GET /api/v1/health -> 200 + {"code":0,"data":{"status":"ok"}}
        - POST /api/v1/echo  -> 200 + {"code":0,"data": <body>}
        - POST /api/v1/biz_err -> 200 + {"code":1001,"message":"bad"}
        - POST /api/v1/http_err -> 500
        """

        class Handler(BaseHTTPRequestHandler):

            def log_message(self, *args, **kwargs):  # 静默日志
                pass

            def _write(self, status: int, body: dict | None = None):
                payload = json.dumps(body or {}).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):  # noqa: N802
                if self.path == "/api/v1/health":
                    self._write(200, {"code": 0,
                                      "data": {"status": "ok",
                                               "uptime_sec": 1,
                                               "version": "test"}})
                else:
                    self._write(404, {"code": 404, "message": "not found"})

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except Exception:
                    payload = {}

                if self.path == "/api/v1/echo":
                    self._write(200, {"code": 0, "data": payload})
                elif self.path == "/api/v1/biz_err":
                    self._write(200, {"code": 1001, "message": "bad"})
                elif self.path == "/api/v1/http_err":
                    self._write(500, {"code": 500, "message": "boom"})
                else:
                    self._write(404, {"code": 404, "message": "not found"})

        # 自定义 ThreadingMixIn + UnixStreamServer
        class UDSHTTPServer(socketserver.ThreadingMixIn,
                            socketserver.UnixStreamServer):
            daemon_threads = True
            allow_reuse_address = True

            def get_request(self):
                # 必须返回 (request, client_address) 才能配合 BaseHTTPRequestHandler
                req, _ = super().get_request()
                return req, ("uds", 0)

        socket_path.parent.mkdir(parents=True, exist_ok=True)
        if socket_path.exists():
            socket_path.unlink()

        server = UDSHTTPServer(str(socket_path), Handler)
        thread = threading.Thread(target=server.serve_forever,
                                  name="fake-worker", daemon=True)
        thread.start()

        # 等 socket 文件就绪（最多 1s）
        for _ in range(20):
            if socket_path.exists():
                break
            time.sleep(0.05)
        return server

    def test_health_check_real_uds(self):
        self.assertTrue(self.client.health_check())
        self.assertTrue(self.client.is_available())

    def test_echo_round_trip(self):
        # 健康检查通过才能后续业务调用（is_available 依赖 _healthy）
        self.assertTrue(self.client.health_check())
        data = self.client.call_or_raise("/api/v1/echo",
                                         {"hello": "world", "n": 42})
        self.assertEqual(data, {"hello": "world", "n": 42})

    def test_business_error_propagates(self):
        from app.schemas.worker import WorkerCallError
        self.client.health_check()
        with self.assertRaises(WorkerCallError) as ctx:
            self.client.call_or_raise("/api/v1/biz_err")
        self.assertIn("1001", str(ctx.exception))

    def test_http_error_propagates(self):
        from app.schemas.worker import WorkerCallError
        self.client.health_check()
        with self.assertRaises(WorkerCallError) as ctx:
            self.client.call_or_raise("/api/v1/http_err")
        self.assertIn("500", str(ctx.exception))

    def test_call_returns_none_on_error(self):
        # call() 不抛异常的版本
        self.client.health_check()
        self.assertIsNone(self.client.call("/api/v1/biz_err"))

    def test_full_lifecycle(self):
        """
        完整生命周期：unhealthy → healthy → 调用成功 → 关闭
        """
        # 初始：尚未巡检过，不可用
        self.assertFalse(self.client.is_available())

        # 巡检后可用
        self.assertTrue(self.client.health_check())
        self.assertTrue(self.client.is_available())

        # 业务调用 OK
        data = self.client.call("/api/v1/echo", {"k": "v"})
        self.assertEqual(data, {"k": "v"})

        # 关闭后状态清零
        self.client.close()
        self.assertFalse(self.client.is_available())

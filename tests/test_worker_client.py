"""
WorkerClient 单元测试

策略：
- 通过 monkeypatch settings.WORKER_SOCKET_DIR 指向临时目录，touch 出假 socket 文件
- 通过 unittest.mock.patch 替换 httpx.Client.post / get，避免真实网络
- 不依赖任何运行中的 Go worker 进程
"""
import shutil
import tempfile
import threading
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

import httpx

from app.core.config import settings
from app.schemas.worker import (
    WorkerCallError,
    WorkerNotAvailable,
    WorkerTimeoutError,
)
from app.utils.worker_client import WorkerClient

WORKER_NAME = "watcher"

class _BaseWorkerClientTest(TestCase):
    """
    通用 setUp：创建临时 socket 目录、把 settings 切到 worker mode
    """

    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="mp-worker-test-"))

        # 备份 settings
        self._orig = {
            "WORKER_MODE": settings.WORKER_MODE,
            "WORKER_ENABLED": settings.WORKER_ENABLED,
            "WORKER_SOCKET_DIR": settings.WORKER_SOCKET_DIR,
            "WORKER_RPC_TIMEOUT": settings.WORKER_RPC_TIMEOUT,
            "WORKER_HEALTH_FAIL_THRESHOLD": settings.WORKER_HEALTH_FAIL_THRESHOLD,
        }

        settings.WORKER_MODE = "worker"
        settings.WORKER_ENABLED = []
        settings.WORKER_SOCKET_DIR = str(self.tmp_dir)
        settings.WORKER_RPC_TIMEOUT = 5
        settings.WORKER_HEALTH_FAIL_THRESHOLD = 3

        self.client = WorkerClient(WORKER_NAME)

    def tearDown(self):
        try:
            self.client.close()
        finally:
            for k, v in self._orig.items():
                setattr(settings, k, v)
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _touch_socket(self):
        """
        创建假的 socket 文件，让 socket_path.exists() == True
        """
        self.client.socket_path.touch()

    @staticmethod
    def _mock_response(status_code: int = 200, json_body=None, text: str = ""):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = status_code
        resp.text = text
        resp.json = MagicMock(return_value=json_body if json_body is not None else {})
        return resp

class IsAvailableTest(_BaseWorkerClientTest):

    def test_disabled_when_mode_python(self):
        settings.WORKER_MODE = "python"
        self._touch_socket()
        self.assertFalse(self.client.is_available())

    def test_disabled_when_socket_missing(self):
        # socket 没创建，不可用
        self.assertFalse(self.client.is_available())

    def test_disabled_when_not_yet_healthy(self):
        # 即使 socket 存在，未经过健康检查也不算可用
        self._touch_socket()
        self.assertFalse(self.client.is_available())

    def test_available_when_all_conditions_met(self):
        self._touch_socket()
        # 直接私有方法标健康，模拟巡检通过
        self.client._mark_healthy()
        self.assertTrue(self.client.is_available())

class CallOrRaiseTest(_BaseWorkerClientTest):

    def test_raises_not_available_when_mode_python(self):
        settings.WORKER_MODE = "python"
        with self.assertRaises(WorkerNotAvailable):
            self.client.call_or_raise("/api/v1/x")

    def test_raises_not_available_when_socket_missing(self):
        with self.assertRaises(WorkerNotAvailable):
            self.client.call_or_raise("/api/v1/x")

    def test_success_returns_data(self):
        self._touch_socket()
        with patch.object(httpx.Client, "post",
                          return_value=self._mock_response(
                              200, {"code": 0, "message": "ok",
                                    "data": {"hello": "world"}})):
            data = self.client.call_or_raise("/api/v1/echo", {"a": 1})
        self.assertEqual(data, {"hello": "world"})

    def test_success_with_null_data_returns_empty_dict(self):
        self._touch_socket()
        with patch.object(httpx.Client, "post",
                          return_value=self._mock_response(
                              200, {"code": 0, "message": "ok", "data": None})):
            data = self.client.call_or_raise("/api/v1/x")
        self.assertEqual(data, {})

    def test_business_error_code_raises_call_error(self):
        self._touch_socket()
        with patch.object(httpx.Client, "post",
                          return_value=self._mock_response(
                              200, {"code": 1001, "message": "bad"})):
            with self.assertRaises(WorkerCallError) as ctx:
                self.client.call_or_raise("/api/v1/x")
        self.assertIn("1001", str(ctx.exception))
        self.assertIn("bad", str(ctx.exception))

    def test_http_error_raises_call_error(self):
        self._touch_socket()
        with patch.object(httpx.Client, "post",
                          return_value=self._mock_response(
                              500, text="internal")):
            with self.assertRaises(WorkerCallError) as ctx:
                self.client.call_or_raise("/api/v1/x")
        self.assertIn("500", str(ctx.exception))

    def test_invalid_json_raises_call_error(self):
        self._touch_socket()
        bad_resp = self._mock_response(200)
        bad_resp.json.side_effect = ValueError("not json")
        with patch.object(httpx.Client, "post", return_value=bad_resp):
            with self.assertRaises(WorkerCallError):
                self.client.call_or_raise("/api/v1/x")

    def test_timeout_raises_timeout_error(self):
        self._touch_socket()
        with patch.object(httpx.Client, "post",
                          side_effect=httpx.TimeoutException("slow")):
            with self.assertRaises(WorkerTimeoutError):
                self.client.call_or_raise("/api/v1/x")

    def test_connection_error_raises_call_error(self):
        self._touch_socket()
        with patch.object(httpx.Client, "post",
                          side_effect=httpx.ConnectError("nope")):
            with self.assertRaises(WorkerCallError):
                self.client.call_or_raise("/api/v1/x")

    def test_custom_timeout_overrides_default(self):
        """
        显式传入 timeout 应覆盖 settings.WORKER_RPC_TIMEOUT
        """
        self._touch_socket()
        with patch.object(httpx.Client, "post",
                          return_value=self._mock_response(
                              200, {"code": 0, "data": {}})) as mocked:
            self.client.call_or_raise("/api/v1/x", timeout=99)
        _, kwargs = mocked.call_args
        self.assertEqual(kwargs.get("timeout"), 99)

class CallSwallowsExceptionsTest(_BaseWorkerClientTest):
    """
    call() 是 call_or_raise() 的吞异常版本，失败应返回 None 让调用方降级
    """

    def test_returns_none_on_not_available(self):
        # socket 不存在
        self.assertIsNone(self.client.call("/api/v1/x"))

    def test_returns_none_on_call_error(self):
        self._touch_socket()
        with patch.object(httpx.Client, "post",
                          return_value=self._mock_response(500)):
            self.assertIsNone(self.client.call("/api/v1/x"))

    def test_returns_data_on_success(self):
        self._touch_socket()
        with patch.object(httpx.Client, "post",
                          return_value=self._mock_response(
                              200, {"code": 0, "data": {"k": "v"}})):
            self.assertEqual(self.client.call("/api/v1/x"), {"k": "v"})

class HealthCheckTest(_BaseWorkerClientTest):

    def test_unhealthy_when_socket_missing(self):
        self.assertFalse(self.client.health_check())
        self.assertFalse(self.client.is_available())

    def test_unhealthy_when_mode_python(self):
        settings.WORKER_MODE = "python"
        self._touch_socket()
        self.assertFalse(self.client.health_check())

    def test_healthy_on_200(self):
        self._touch_socket()
        with patch.object(httpx.Client, "get",
                          return_value=self._mock_response(200)):
            self.assertTrue(self.client.health_check())
        self.assertTrue(self.client.is_available())

    def test_unhealthy_on_non_200(self):
        self._touch_socket()
        with patch.object(httpx.Client, "get",
                          return_value=self._mock_response(503)):
            self.assertFalse(self.client.health_check())

    def test_unhealthy_on_exception(self):
        self._touch_socket()
        with patch.object(httpx.Client, "get",
                          side_effect=httpx.ConnectError("nope")):
            self.assertFalse(self.client.health_check())

class FailureThresholdTest(_BaseWorkerClientTest):
    """
    连续失败达到阈值才标记不可用，保护"偶发抖动不掉线"
    """

    def test_marks_unhealthy_only_after_threshold(self):
        settings.WORKER_HEALTH_FAIL_THRESHOLD = 3
        self._touch_socket()
        # 先变成 healthy
        with patch.object(httpx.Client, "get",
                          return_value=self._mock_response(200)):
            self.assertTrue(self.client.health_check())

        # 注入失败
        with patch.object(httpx.Client, "get",
                          side_effect=httpx.ConnectError("nope")):
            # 第 1 次失败：还应该是 healthy（阈值未达）
            self.client.health_check()
            self.assertTrue(self.client.is_available())
            # 第 2 次：还 healthy
            self.client.health_check()
            self.assertTrue(self.client.is_available())
            # 第 3 次：触发降级
            self.client.health_check()
            self.assertFalse(self.client.is_available())

    def test_recovery_resets_fail_count(self):
        settings.WORKER_HEALTH_FAIL_THRESHOLD = 2
        self._touch_socket()

        with patch.object(httpx.Client, "get",
                          side_effect=httpx.ConnectError("nope")):
            self.client.health_check()  # fail 1
        self.assertFalse(self.client.is_available())  # 还没掉线，但首次没 healthy

        # 让它 healthy 一次
        with patch.object(httpx.Client, "get",
                          return_value=self._mock_response(200)):
            self.client.health_check()
        self.assertTrue(self.client.is_available())

        # 再失败 1 次：因为刚才恢复时 fail_count 重置过，所以仍 healthy
        with patch.object(httpx.Client, "get",
                          side_effect=httpx.ConnectError("nope")):
            self.client.health_check()
        self.assertTrue(self.client.is_available())

    def test_unhealthy_releases_httpx_client(self):
        """
        关键：降级时必须释放 httpx 连接池，避免 UDS inode 失效后 BrokenPipe 风暴
        """
        settings.WORKER_HEALTH_FAIL_THRESHOLD = 1
        self._touch_socket()
        # healthy 一次以创建 client
        with patch.object(httpx.Client, "get",
                          return_value=self._mock_response(200)):
            self.client.health_check()
        self.assertIsNotNone(self.client._client)

        # 失败一次（阈值=1）应触发释放
        with patch.object(httpx.Client, "get",
                          side_effect=httpx.ConnectError("nope")):
            self.client.health_check()
        self.assertIsNone(self.client._client)

class CloseTest(_BaseWorkerClientTest):

    def test_close_idempotent(self):
        self.client.close()
        self.client.close()  # 不应抛异常

    def test_close_resets_state(self):
        self._touch_socket()
        with patch.object(httpx.Client, "get",
                          return_value=self._mock_response(200)):
            self.client.health_check()
        self.assertTrue(self.client._healthy)

        self.client.close()
        self.assertFalse(self.client._healthy)
        self.assertEqual(self.client._fail_count, 0)
        self.assertIsNone(self.client._client)

class ConcurrencyTest(_BaseWorkerClientTest):
    """
    多线程并发健康检查不应炸锁、不应崩溃
    """

    def test_concurrent_health_check(self):
        self._touch_socket()
        errors = []

        def _run():
            try:
                for _ in range(20):
                    self.client.health_check()
            except Exception as e:  # pragma: no cover - 并发出错才会进
                errors.append(e)

        with patch.object(httpx.Client, "get",
                          return_value=self._mock_response(200)):
            threads = [threading.Thread(target=_run) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertTrue(self.client.is_available())

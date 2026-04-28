"""
WorkerClientManager 单元测试

覆盖：
- 单例语义（同一进程多次实例化返回同一对象）
- get(name) 按需创建并复用
- start() 在 python 模式下应短路（不起线程）
- start() 在 worker 模式下起后台巡检线程
- stop() 能让线程退出 + 关闭所有 client
- 巡检循环会真正调用每个 client 的 health_check
"""
import shutil
import tempfile
import threading
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from app.core.config import settings
from app.utils.singleton import SingletonClass
from app.utils.worker_client import WorkerClient, WorkerClientManager

class _ManagerTestBase(TestCase):

    def setUp(self):
        # 清理单例，每个用例从干净状态起
        SingletonClass._instances.pop(WorkerClientManager, None)

        self.tmp_dir = Path(tempfile.mkdtemp(prefix="mp-mgr-test-"))
        self._orig = {
            "WORKER_MODE": settings.WORKER_MODE,
            "WORKER_ENABLED": settings.WORKER_ENABLED,
            "WORKER_SOCKET_DIR": settings.WORKER_SOCKET_DIR,
            "WORKER_HEALTH_INTERVAL": settings.WORKER_HEALTH_INTERVAL,
        }
        settings.WORKER_SOCKET_DIR = str(self.tmp_dir)
        # 缩短巡检周期方便测试，但 _health_loop 内部 max(interval, 5) 会兜底为 5
        settings.WORKER_HEALTH_INTERVAL = 1

    def tearDown(self):
        try:
            mgr = WorkerClientManager()
            mgr.stop()
        finally:
            SingletonClass._instances.pop(WorkerClientManager, None)
            for k, v in self._orig.items():
                setattr(settings, k, v)
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

class SingletonTest(_ManagerTestBase):

    def test_returns_same_instance(self):
        a = WorkerClientManager()
        b = WorkerClientManager()
        self.assertIs(a, b)

class GetTest(_ManagerTestBase):

    def test_get_creates_lazily(self):
        mgr = WorkerClientManager()
        c1 = mgr.get("watcher")
        self.assertIsInstance(c1, WorkerClient)
        self.assertEqual(c1.worker_name, "watcher")

    def test_get_reuses_same_client(self):
        mgr = WorkerClientManager()
        c1 = mgr.get("watcher")
        c2 = mgr.get("watcher")
        self.assertIs(c1, c2)

    def test_different_names_get_different_clients(self):
        mgr = WorkerClientManager()
        c1 = mgr.get("watcher")
        c2 = mgr.get("transfer")
        self.assertIsNot(c1, c2)

class StartStopTest(_ManagerTestBase):

    def test_start_noop_in_python_mode(self):
        settings.WORKER_MODE = "python"
        mgr = WorkerClientManager()
        mgr.start()
        self.assertIsNone(mgr._health_thread)

    def test_start_spawns_thread_in_worker_mode(self):
        settings.WORKER_MODE = "worker"
        mgr = WorkerClientManager()
        mgr.start()
        try:
            self.assertIsNotNone(mgr._health_thread)
            self.assertTrue(mgr._health_thread.is_alive())
            self.assertTrue(mgr._health_thread.daemon)
        finally:
            mgr.stop()

    def test_start_is_idempotent(self):
        settings.WORKER_MODE = "worker"
        mgr = WorkerClientManager()
        mgr.start()
        first = mgr._health_thread
        mgr.start()
        self.assertIs(mgr._health_thread, first)
        mgr.stop()

    def test_stop_terminates_thread(self):
        settings.WORKER_MODE = "worker"
        mgr = WorkerClientManager()
        mgr.start()
        thread = mgr._health_thread
        mgr.stop()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())

    def test_stop_closes_all_clients(self):
        settings.WORKER_MODE = "worker"
        mgr = WorkerClientManager()
        c1 = mgr.get("watcher")
        c2 = mgr.get("transfer")
        with patch.object(WorkerClient, "close") as mocked_close:
            mgr.stop()
        self.assertEqual(mocked_close.call_count, 2)
        del c1, c2  # 让 lint 知道我们用过

class HealthLoopTest(_ManagerTestBase):

    def test_loop_calls_health_check_at_least_once(self):
        """
        启动后立即跑一次巡检，使用 Event 同步避免 sleep 不确定性
        """
        settings.WORKER_MODE = "worker"
        mgr = WorkerClientManager()
        called = threading.Event()

        def fake_health_check(self):
            called.set()
            return False

        mgr.get("watcher")  # 注册一个待巡检 client
        with patch.object(WorkerClient, "health_check",
                          new=fake_health_check):
            mgr.start()
            triggered = called.wait(timeout=3)
        mgr.stop()
        self.assertTrue(triggered, "巡检线程未在 3s 内调用 health_check")

    def test_check_all_swallows_exception(self):
        """
        某个 client 抛异常不能让整个巡检线程死掉
        """
        settings.WORKER_MODE = "worker"
        mgr = WorkerClientManager()
        mgr.get("watcher")

        boom_called = threading.Event()

        def boom(self):
            boom_called.set()
            raise RuntimeError("boom")

        with patch.object(WorkerClient, "health_check", new=boom):
            mgr.start()
            self.assertTrue(boom_called.wait(timeout=3))
            # 给线程一点时间确认它没崩
            time.sleep(0.1)
            self.assertTrue(mgr._health_thread.is_alive())
        mgr.stop()

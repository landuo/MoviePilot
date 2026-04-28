"""
Worker 相关配置项与 is_worker_enabled 判定逻辑测试
"""
from pathlib import Path
from unittest import TestCase

from app.core.config import settings

class WorkerConfigDefaultsTest(TestCase):
    """
    确认默认值与"老用户零感知"承诺一致：
    - WORKER_MODE 默认 python
    - WORKER_ENABLED 默认空列表
    - is_worker_enabled() 对任何 worker 都返回 False
    """

    def test_default_mode_is_python(self):
        self.assertEqual(settings.WORKER_MODE, "python")

    def test_default_enabled_is_empty(self):
        self.assertEqual(settings.WORKER_ENABLED, [])

    def test_default_timeout_and_health(self):
        # 仅做 sanity check，避免后续不小心被改成 0
        self.assertGreater(settings.WORKER_RPC_TIMEOUT, 0)
        self.assertGreater(settings.WORKER_HEALTH_INTERVAL, 0)
        self.assertGreaterEqual(settings.WORKER_HEALTH_FAIL_THRESHOLD, 1)

class IsWorkerEnabledTest(TestCase):
    """
    is_worker_enabled 三种 mode 的判定逻辑
    """

    def setUp(self):
        # 保存原值，setUp/tearDown 互为镜像
        self._orig_mode = settings.WORKER_MODE
        self._orig_enabled = settings.WORKER_ENABLED

    def tearDown(self):
        settings.WORKER_MODE = self._orig_mode
        settings.WORKER_ENABLED = self._orig_enabled

    def test_python_mode_disables_all(self):
        settings.WORKER_MODE = "python"
        settings.WORKER_ENABLED = ["watcher"]  # 即使列了也忽略
        self.assertFalse(settings.is_worker_enabled("watcher"))
        self.assertFalse(settings.is_worker_enabled("transfer"))

    def test_worker_mode_enables_all(self):
        settings.WORKER_MODE = "worker"
        settings.WORKER_ENABLED = []
        self.assertTrue(settings.is_worker_enabled("watcher"))
        self.assertTrue(settings.is_worker_enabled("any-name"))

    def test_hybrid_mode_filters_by_enabled_list(self):
        settings.WORKER_MODE = "hybrid"
        settings.WORKER_ENABLED = ["watcher"]
        self.assertTrue(settings.is_worker_enabled("watcher"))
        self.assertFalse(settings.is_worker_enabled("transfer"))

    def test_hybrid_with_empty_enabled_list(self):
        settings.WORKER_MODE = "hybrid"
        settings.WORKER_ENABLED = []
        self.assertFalse(settings.is_worker_enabled("watcher"))

    def test_unknown_mode_disables_all(self):
        # 防御性：拼写错误的 mode 应当全关，避免误启
        settings.WORKER_MODE = "typo-mode"
        settings.WORKER_ENABLED = ["watcher"]
        self.assertFalse(settings.is_worker_enabled("watcher"))

    def test_mode_is_case_insensitive(self):
        settings.WORKER_MODE = "WORKER"
        self.assertTrue(settings.is_worker_enabled("watcher"))

class WorkerSocketPathTest(TestCase):
    """
    WORKER_SOCKET_PATH 属性：显式 dir 优先，否则走 CONFIG_PATH/sockets
    """

    def setUp(self):
        self._orig = settings.WORKER_SOCKET_DIR

    def tearDown(self):
        settings.WORKER_SOCKET_DIR = self._orig

    def test_explicit_dir_takes_precedence(self):
        settings.WORKER_SOCKET_DIR = "/tmp/mp-test-sockets"
        self.assertEqual(settings.WORKER_SOCKET_PATH, Path("/tmp/mp-test-sockets"))

    def test_default_falls_back_to_config_path(self):
        settings.WORKER_SOCKET_DIR = None
        self.assertEqual(settings.WORKER_SOCKET_PATH,
                         settings.CONFIG_PATH / "sockets")

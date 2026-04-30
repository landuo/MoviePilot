"""
SystemUtils 与 mp-transfer worker 协同的单元测试

覆盖 app/utils/system.py 中 4 个静态方法（copy/move/link/softlink）
在以下场景下的行为：
  1. worker 未启用 → 走本地实现（与改造前完全一致）
  2. worker 启用且健康 → 走 worker，且 SystemUtils 仍返回 (0, "")
  3. worker 调用失败 → fallback 到本地实现，对调用方透明
  4. 相对路径 → 不走 worker，直接本地实现

策略：
- 通过 patch app.utils.system.WorkerClientManager / settings 控制 worker 状态
- 用真实临时目录验证本地 fallback 的行为正确（避免 mock shutil 误伤）
"""
import shutil
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

from app.core.config import settings
from app.schemas.worker import TransferMode, WorkerCallError
from app.utils import system as system_module
from app.utils.singleton import SingletonClass
from app.utils.system import SystemUtils
from app.utils.worker_client import WorkerClientManager

class _TransferTestBase(TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="mp-sysutils-test-"))
        self.src = self.tmp_dir / "src.txt"
        self.dst = self.tmp_dir / "dst.txt"
        self.src.write_text("payload", encoding="utf-8")

        self._orig = {
            "WORKER_MODE": settings.WORKER_MODE,
            "WORKER_ENABLED": settings.WORKER_ENABLED,
        }
        # 重置单例，避免不同用例间的 manager 状态污染
        SingletonClass._instances.pop(WorkerClientManager, None)

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(settings, k, v)
        SingletonClass._instances.pop(WorkerClientManager, None)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _enable_transfer_worker(self):
        settings.WORKER_MODE = "hybrid"
        settings.WORKER_ENABLED = ["transfer"]

    def _disable_transfer_worker(self):
        settings.WORKER_MODE = "python"
        settings.WORKER_ENABLED = []

    def _patch_worker_client(self, *, available: bool, raises: Exception = None):
        """
        构造一个 mock client 并把 WorkerClientManager().get(...) 接管。
        返回 (manager_patch, mock_client) 的上下文使用方式见调用处。
        """
        client = MagicMock()
        client.is_available.return_value = available
        if raises:
            client.call_or_raise.side_effect = raises
        else:
            client.call_or_raise.return_value = {}
        return client

class CopyWithWorkerTest(_TransferTestBase):
    def test_worker_disabled_falls_back_to_shutil(self):
        self._disable_transfer_worker()
        ret, msg = SystemUtils.copy(self.src, self.dst)
        self.assertEqual((ret, msg), (0, ""))
        self.assertEqual(self.dst.read_text(encoding="utf-8"), "payload")

    def test_worker_success_returns_zero_without_local_io(self):
        self._enable_transfer_worker()
        client = self._patch_worker_client(available=True)
        with patch.object(system_module, "WorkerClientManager") as mgr:
            mgr.return_value.get.return_value = client
            with patch("shutil.copy2") as local_copy:
                ret, msg = SystemUtils.copy(self.src, self.dst)
        self.assertEqual((ret, msg), (0, ""))
        client.call_or_raise.assert_called_once()
        # worker 成功路径下，绝不应走本地 shutil（防止双写）
        local_copy.assert_not_called()
        # 校验请求体字段对齐 Go 端
        path, payload = client.call_or_raise.call_args.args
        self.assertEqual(path, "/api/v1/transfer")
        self.assertEqual(payload["mode"], TransferMode.COPY)
        self.assertEqual(payload["src"], str(self.src))
        self.assertEqual(payload["dst"], str(self.dst))

    def test_worker_unavailable_falls_back(self):
        self._enable_transfer_worker()
        client = self._patch_worker_client(available=False)
        with patch.object(system_module, "WorkerClientManager") as mgr:
            mgr.return_value.get.return_value = client
            ret, msg = SystemUtils.copy(self.src, self.dst)
        self.assertEqual((ret, msg), (0, ""))
        # 不应触发远程调用
        client.call_or_raise.assert_not_called()
        # 但本地必须真的拷过
        self.assertTrue(self.dst.exists())

    def test_worker_call_error_falls_back(self):
        self._enable_transfer_worker()
        client = self._patch_worker_client(
            available=True, raises=WorkerCallError("boom")
        )
        with patch.object(system_module, "WorkerClientManager") as mgr:
            mgr.return_value.get.return_value = client
            ret, msg = SystemUtils.copy(self.src, self.dst)
        self.assertEqual((ret, msg), (0, ""))
        # worker 被尝试过，但失败后落回本地
        client.call_or_raise.assert_called_once()
        self.assertEqual(self.dst.read_text(encoding="utf-8"), "payload")

    def test_relative_path_skips_worker(self):
        """
        相对路径不能传给 worker（cwd 不一致）。但本地 shutil 接受 Path，
        这里只断言不调用 worker；本地行为由 shutil 自己决定。
        """
        self._enable_transfer_worker()
        client = self._patch_worker_client(available=True)
        with patch.object(system_module, "WorkerClientManager") as mgr:
            mgr.return_value.get.return_value = client
            # 用 cwd 以外的相对路径，shutil.copy2 不会真的成功，所以这里
            # mock 掉本地 IO 仅断言路由判断
            with patch("shutil.copy2") as local_copy:
                SystemUtils.copy(Path("rel/src"), Path("rel/dst"))
        client.call_or_raise.assert_not_called()
        local_copy.assert_called_once()

class MoveWithWorkerTest(_TransferTestBase):
    def test_worker_success(self):
        self._enable_transfer_worker()
        client = self._patch_worker_client(available=True)
        with patch.object(system_module, "WorkerClientManager") as mgr:
            mgr.return_value.get.return_value = client
            with patch("shutil.move") as local_move:
                ret, msg = SystemUtils.move(self.src, self.dst)
        self.assertEqual((ret, msg), (0, ""))
        local_move.assert_not_called()
        payload = client.call_or_raise.call_args.args[1]
        self.assertEqual(payload["mode"], TransferMode.MOVE)

    def test_worker_disabled_uses_shutil(self):
        self._disable_transfer_worker()
        ret, msg = SystemUtils.move(self.src, self.dst)
        self.assertEqual((ret, msg), (0, ""))
        self.assertFalse(self.src.exists())
        self.assertTrue(self.dst.exists())

class LinkWithWorkerTest(_TransferTestBase):
    def test_worker_success(self):
        self._enable_transfer_worker()
        client = self._patch_worker_client(available=True)
        with patch.object(system_module, "WorkerClientManager") as mgr:
            mgr.return_value.get.return_value = client
            ret, msg = SystemUtils.link(self.src, self.dst)
        self.assertEqual((ret, msg), (0, ""))
        payload = client.call_or_raise.call_args.args[1]
        self.assertEqual(payload["mode"], TransferMode.LINK)

    def test_worker_disabled_creates_real_hardlink(self):
        self._disable_transfer_worker()
        ret, msg = SystemUtils.link(self.src, self.dst)
        self.assertEqual((ret, msg), (0, ""))
        # 同一文件系统下硬链接 inode 相同
        self.assertEqual(self.src.stat().st_ino, self.dst.stat().st_ino)

class SoftlinkWithWorkerTest(_TransferTestBase):
    def test_worker_success(self):
        self._enable_transfer_worker()
        client = self._patch_worker_client(available=True)
        with patch.object(system_module, "WorkerClientManager") as mgr:
            mgr.return_value.get.return_value = client
            ret, msg = SystemUtils.softlink(self.src, self.dst)
        self.assertEqual((ret, msg), (0, ""))
        payload = client.call_or_raise.call_args.args[1]
        self.assertEqual(payload["mode"], TransferMode.SOFTLINK)

    def test_worker_disabled_creates_real_symlink(self):
        self._disable_transfer_worker()
        ret, msg = SystemUtils.softlink(self.src, self.dst)
        self.assertEqual((ret, msg), (0, ""))
        self.assertTrue(self.dst.is_symlink())
        self.assertEqual(self.dst.read_text(encoding="utf-8"), "payload")

"""
mp-watcher 适配层单元测试（P1-B）

测试目标：app/utils/monitor_worker.py
    - WorkerFileEvent：鸭子类型字段
    - make_watch_id：watch_id 格式
    - try_configure_watcher：worker 不可用 / call 失败 / 成功 / 过滤无效目录
    - release_watcher：注销回调
    - 回调 handler：事件类型过滤、字段映射、异常吞咽
"""
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


# ----- 在 import monitor_worker 之前 stub 掉它的少量重依赖 -----

def _install_stubs():
    """
    monitor_worker 只依赖：
    - app.api.endpoints.worker_callback.{register_handler, unregister_handler}
    - app.log.logger
    - app.schemas.worker.{WorkerCallError, WorkerNotAvailable}
    - app.utils.worker_client.WorkerClientManager
    其中 worker_callback 会拉 fastapi/schemas，这里用 stub 替代。
    """
    cb_stub = types.ModuleType("app.api.endpoints.worker_callback")
    cb_stub._registered = {}

    def _register(name, fn):
        cb_stub._registered[name] = fn

    def _unregister(name):
        cb_stub._registered.pop(name, None)

    cb_stub.register_handler = _register
    cb_stub.unregister_handler = _unregister
    sys.modules["app.api.endpoints.worker_callback"] = cb_stub


_install_stubs()

from app.utils import monitor_worker  # noqa: E402
from app.schemas.worker import WorkerCallError  # noqa: E402


# ----- fixtures -----

class _FakeMonDir:
    """模拟 schemas.TransferDirectoryConf 的最小子集"""

    def __init__(self, storage="local", download_path="/data/movies",
                 library_path="/library/movies", monitor_type="monitor"):
        self.storage = storage
        self.download_path = download_path
        self.library_path = library_path
        self.monitor_type = monitor_type


# ----- 测试用例 -----

class WorkerFileEventTest(unittest.TestCase):

    def test_fields(self):
        ev = monitor_worker.WorkerFileEvent(
            src_path="/a", is_directory=True, event_type="created"
        )
        self.assertEqual(ev.src_path, "/a")
        self.assertTrue(ev.is_directory)
        self.assertEqual(ev.event_type, "created")

    def test_slots_blocks_unknown_attrs(self):
        ev = monitor_worker.WorkerFileEvent(
            src_path="/a", is_directory=False, event_type="created"
        )
        with self.assertRaises(AttributeError):
            ev.dest_path = "/b"  # __slots__ 限制


class MakeWatchIdTest(unittest.TestCase):

    def test_format(self):
        d = _FakeMonDir(storage="local", download_path="/a/b")
        self.assertEqual(monitor_worker.make_watch_id(d), "local::/a/b")

    def test_distinguishes_storage(self):
        d1 = _FakeMonDir(storage="local", download_path="/a")
        d2 = _FakeMonDir(storage="rclone", download_path="/a")
        self.assertNotEqual(monitor_worker.make_watch_id(d1),
                            monitor_worker.make_watch_id(d2))


class IsValidLocalMonitorDirTest(unittest.TestCase):

    def test_accepts_normal_local(self):
        d = _FakeMonDir()
        self.assertTrue(monitor_worker._is_valid_local_monitor_dir(d))

    def test_rejects_non_local(self):
        d = _FakeMonDir(storage="rclone")
        self.assertFalse(monitor_worker._is_valid_local_monitor_dir(d))

    def test_rejects_non_monitor_type(self):
        d = _FakeMonDir(monitor_type="downloader")
        self.assertFalse(monitor_worker._is_valid_local_monitor_dir(d))

    def test_rejects_no_library_path(self):
        d = _FakeMonDir(library_path="")
        self.assertFalse(monitor_worker._is_valid_local_monitor_dir(d))

    def test_rejects_library_inside_download(self):
        # library_path 是 download_path 子目录 → 拒绝（防循环触发）
        d = _FakeMonDir(download_path="/data", library_path="/data/lib")
        self.assertFalse(monitor_worker._is_valid_local_monitor_dir(d))


class TryConfigureWatcherTest(unittest.TestCase):

    def setUp(self):
        self.events = []
        self.on_event = lambda text, path, size, ev: self.events.append(
            (text, path, size, ev)
        )
        # 每个用例开始时清空 stub 注册表
        sys.modules["app.api.endpoints.worker_callback"]._registered.clear()

    def test_returns_empty_when_no_local_dirs(self):
        with patch.object(monitor_worker, "WorkerClientManager") as mgr:
            result = monitor_worker.try_configure_watcher(
                [_FakeMonDir(storage="rclone")], self.on_event
            )
        self.assertEqual(result, set())
        mgr.assert_not_called()

    def test_returns_empty_when_worker_unavailable(self):
        client = MagicMock()
        client.is_available.return_value = False
        client.health_check.return_value = False
        with patch.object(monitor_worker, "WorkerClientManager") as mgr:
            mgr.return_value.get.return_value = client
            result = monitor_worker.try_configure_watcher(
                [_FakeMonDir()], self.on_event
            )
        self.assertEqual(result, set())
        cb_mod = sys.modules["app.api.endpoints.worker_callback"]
        self.assertEqual(cb_mod._registered, {})

    def test_returns_empty_when_call_raises(self):
        client = MagicMock()
        client.is_available.return_value = True
        client.call_or_raise.side_effect = WorkerCallError("boom")
        with patch.object(monitor_worker, "WorkerClientManager") as mgr:
            mgr.return_value.get.return_value = client
            result = monitor_worker.try_configure_watcher(
                [_FakeMonDir()], self.on_event
            )
        self.assertEqual(result, set())
        cb_mod = sys.modules["app.api.endpoints.worker_callback"]
        self.assertEqual(cb_mod._registered, {})

    def test_success_returns_watch_ids_and_registers_handler(self):
        client = MagicMock()
        client.is_available.return_value = True
        client.call_or_raise.return_value = {
            "added": 2, "removed": 0, "total": 2,
        }
        dirs = [
            _FakeMonDir(download_path="/data/movies"),
            _FakeMonDir(download_path="/data/tv"),
        ]
        with patch.object(monitor_worker, "WorkerClientManager") as mgr:
            mgr.return_value.get.return_value = client
            result = monitor_worker.try_configure_watcher(dirs, self.on_event)

        self.assertEqual(result, {"local::/data/movies", "local::/data/tv"})

        # 校验 configure 收到的 payload
        call_args = client.call_or_raise.call_args
        self.assertEqual(call_args.args[0], "/api/v1/configure")
        watches = call_args.args[1]["watches"]
        self.assertEqual(len(watches), 2)
        self.assertTrue(all(w["recursive"] for w in watches))

        # 注册了回调
        cb_mod = sys.modules["app.api.endpoints.worker_callback"]
        self.assertIn("watcher", cb_mod._registered)

    def test_health_check_triggered_when_initially_unavailable(self):
        # 首次 is_available=False，health_check 后变 True
        client = MagicMock()
        client.is_available.side_effect = [False, True]
        client.health_check.return_value = True
        client.call_or_raise.return_value = {"added": 1, "total": 1}

        with patch.object(monitor_worker, "WorkerClientManager") as mgr:
            mgr.return_value.get.return_value = client
            result = monitor_worker.try_configure_watcher(
                [_FakeMonDir()], self.on_event
            )

        client.health_check.assert_called_once()
        self.assertEqual(len(result), 1)

    def test_filters_invalid_dirs_in_payload(self):
        client = MagicMock()
        client.is_available.return_value = True
        client.call_or_raise.return_value = {"added": 1, "total": 1}
        dirs = [
            _FakeMonDir(storage="rclone"),
            _FakeMonDir(monitor_type="downloader"),
            _FakeMonDir(library_path=""),
            _FakeMonDir(download_path="/data/keep"),
        ]
        with patch.object(monitor_worker, "WorkerClientManager") as mgr:
            mgr.return_value.get.return_value = client
            monitor_worker.try_configure_watcher(dirs, self.on_event)

        watches = client.call_or_raise.call_args.args[1]["watches"]
        self.assertEqual(len(watches), 1)
        self.assertEqual(watches[0]["watch_id"], "local::/data/keep")


class ReleaseWatcherTest(unittest.TestCase):

    def test_unregisters(self):
        cb_mod = sys.modules["app.api.endpoints.worker_callback"]
        cb_mod._registered["watcher"] = lambda _: None
        monitor_worker.release_watcher()
        self.assertNotIn("watcher", cb_mod._registered)

    def test_safe_when_not_registered(self):
        cb_mod = sys.modules["app.api.endpoints.worker_callback"]
        cb_mod._registered.clear()
        # 不应抛
        monitor_worker.release_watcher()


class CallbackHandlerTest(unittest.TestCase):
    """通过完整链路测试 _make_callback_handler：try_configure_watcher 注册后触发"""

    def setUp(self):
        self.events = []
        self.on_event = lambda text, path, size, ev: self.events.append({
            "text": text, "path": path, "size": size,
            "event_type": ev.event_type, "is_directory": ev.is_directory,
        })

        client = MagicMock()
        client.is_available.return_value = True
        client.call_or_raise.return_value = {"added": 1, "total": 1}
        with patch.object(monitor_worker, "WorkerClientManager") as mgr:
            mgr.return_value.get.return_value = client
            monitor_worker.try_configure_watcher(
                [_FakeMonDir()], self.on_event
            )
        cb_mod = sys.modules["app.api.endpoints.worker_callback"]
        self.handler = cb_mod._registered["watcher"]

    def test_created_event_forwarded(self):
        self.handler({
            "event_type": "created",
            "src_path": "/data/foo.mkv",
            "is_directory": False,
            "file_size": 1234,
        })
        self.assertEqual(len(self.events), 1)
        e = self.events[0]
        self.assertEqual(e["text"], "创建")
        self.assertEqual(e["path"], "/data/foo.mkv")
        self.assertEqual(e["size"], 1234)
        self.assertEqual(e["event_type"], "created")

    def test_modified_event_forwarded(self):
        self.handler({
            "event_type": "modified",
            "src_path": "/data/x.mkv",
        })
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]["text"], "修改")

    def test_moved_event_ignored(self):
        # moved 由后续 created 关联
        self.handler({"event_type": "moved", "src_path": "/a"})
        self.assertEqual(self.events, [])

    def test_deleted_event_ignored(self):
        self.handler({"event_type": "deleted", "src_path": "/a"})
        self.assertEqual(self.events, [])

    def test_empty_src_path_ignored(self):
        self.handler({"event_type": "created", "src_path": ""})
        self.assertEqual(self.events, [])

    def test_unknown_event_type_ignored(self):
        self.handler({"event_type": "weird", "src_path": "/a"})
        self.assertEqual(self.events, [])

    def test_handler_swallows_exception(self):
        # 即使下游 on_event 抛错，也不应影响后续回调
        bad_handler = monitor_worker._make_callback_handler(
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        # 不应抛
        bad_handler({
            "event_type": "created", "src_path": "/x",
        })


if __name__ == "__main__":
    unittest.main()

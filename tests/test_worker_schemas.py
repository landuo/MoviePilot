"""
Worker schema 单元测试：异常体系、统一响应、回调载荷
"""
from unittest import TestCase

from app.schemas.worker import (
    WatcherFileEvent,
    WorkerCallError,
    WorkerError,
    WorkerHealth,
    WorkerNotAvailable,
    WorkerResponse,
    WorkerTimeoutError,
)

class WorkerExceptionHierarchyTest(TestCase):
    """
    异常类继承关系：所有 worker 异常都应能被 WorkerError 捕获
    WorkerTimeoutError 必须是 WorkerCallError 的子类
    """

    def test_all_inherit_from_worker_error(self):
        self.assertTrue(issubclass(WorkerNotAvailable, WorkerError))
        self.assertTrue(issubclass(WorkerCallError, WorkerError))
        self.assertTrue(issubclass(WorkerTimeoutError, WorkerError))

    def test_timeout_inherits_from_call_error(self):
        # 设计意图：调用方只 catch WorkerCallError 也能拦到超时
        self.assertTrue(issubclass(WorkerTimeoutError, WorkerCallError))

    def test_can_be_caught_as_worker_error(self):
        for exc_cls in (WorkerNotAvailable, WorkerCallError, WorkerTimeoutError):
            with self.subTest(exc=exc_cls):
                try:
                    raise exc_cls("boom")
                except WorkerError as e:
                    self.assertIn("boom", str(e))

class WorkerResponseTest(TestCase):

    def test_default_values(self):
        resp = WorkerResponse()
        self.assertEqual(resp.code, 0)
        self.assertEqual(resp.message, "ok")
        self.assertIsNone(resp.data)

    def test_with_data(self):
        resp = WorkerResponse(code=1001, message="bad", data={"x": 1})
        self.assertEqual(resp.code, 1001)
        self.assertEqual(resp.message, "bad")
        self.assertEqual(resp.data, {"x": 1})

class WorkerHealthTest(TestCase):

    def test_minimal(self):
        health = WorkerHealth(status="ok")
        self.assertEqual(health.status, "ok")
        self.assertEqual(health.uptime_sec, 0)
        self.assertEqual(health.version, "")

class WatcherFileEventTest(TestCase):
    """
    回调载荷：必填字段缺失应抛 ValidationError
    """

    def test_minimal_valid(self):
        ev = WatcherFileEvent(
            event_id="e1",
            watch_id="w1",
            event_type="created",
            src_path="/data/a.mkv",
        )
        self.assertEqual(ev.event_id, "e1")
        self.assertEqual(ev.storage, "local")
        self.assertEqual(ev.dest_path, "")
        self.assertFalse(ev.is_directory)

    def test_full_payload(self):
        ev = WatcherFileEvent(
            event_id="e2",
            watch_id="w2",
            event_type="moved",
            storage="alist",
            src_path="/src/a.mkv",
            dest_path="/dst/a.mkv",
            file_size=1024,
            mtime_unix=1700000000,
            is_directory=False,
        )
        self.assertEqual(ev.dest_path, "/dst/a.mkv")
        self.assertEqual(ev.file_size, 1024)

    def test_missing_required_field_raises(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            WatcherFileEvent(event_id="x")  # 缺少 watch_id / event_type / src_path

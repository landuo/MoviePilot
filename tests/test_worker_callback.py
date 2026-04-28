"""
Worker 回调路由单元测试

覆盖：
- register_handler / unregister_handler 基本语义
- 重复注册警告但仍然覆盖
- 未注册的 worker_name 返回 404
- handler 抛异常返回 success=False
- 同步 handler 正常执行
- 异步 handler 被正确 await
- 非法 JSON 返回 400
- 多线程并发注册不崩
"""
import asyncio
import threading
from unittest import TestCase

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.endpoints import worker_callback

class _CallbackTestBase(TestCase):

    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(worker_callback.router,
                                prefix="/api/v1/worker_callback")
        self.client = TestClient(self.app)
        # 清理可能的残留
        for name in ("watcher", "transfer", "search", "boom",
                     "sync-x", "async-x", "concurrent"):
            worker_callback.unregister_handler(name)

    def tearDown(self):
        for name in ("watcher", "transfer", "search", "boom",
                     "sync-x", "async-x", "concurrent"):
            worker_callback.unregister_handler(name)

class RegisterUnregisterTest(_CallbackTestBase):

    def test_register_then_unregister(self):
        called = []
        worker_callback.register_handler("watcher", lambda p: called.append(p))
        # 注册后能调用到
        resp = self.client.post("/api/v1/worker_callback/watcher",
                                json={"x": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["success"], True)
        self.assertEqual(called, [{"x": 1}])

        # 注销后 404
        worker_callback.unregister_handler("watcher")
        resp = self.client.post("/api/v1/worker_callback/watcher",
                                json={"x": 2})
        self.assertEqual(resp.status_code, 404)

    def test_unregister_unknown_does_not_raise(self):
        # 不应抛异常
        worker_callback.unregister_handler("never-registered")

    def test_register_overwrites_existing(self):
        results = []
        worker_callback.register_handler("watcher",
                                         lambda p: results.append(("v1", p)))
        worker_callback.register_handler("watcher",
                                         lambda p: results.append(("v2", p)))
        self.client.post("/api/v1/worker_callback/watcher", json={"a": 1})
        self.assertEqual(results, [("v2", {"a": 1})])

class HandlerExecutionTest(_CallbackTestBase):

    def test_unknown_worker_returns_404(self):
        resp = self.client.post("/api/v1/worker_callback/unknown",
                                json={})
        self.assertEqual(resp.status_code, 404)

    def test_invalid_json_returns_400(self):
        worker_callback.register_handler("watcher", lambda p: None)
        resp = self.client.post("/api/v1/worker_callback/watcher",
                                content=b"not-json",
                                headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 400)

    def test_sync_handler_success(self):
        worker_callback.register_handler("sync-x", lambda p: None)
        resp = self.client.post("/api/v1/worker_callback/sync-x",
                                json={"k": "v"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["success"], True)

    def test_async_handler_awaited(self):
        flag = {"called": False}

        async def handler(payload):
            await asyncio.sleep(0)
            flag["called"] = True
            flag["payload"] = payload

        worker_callback.register_handler("async-x", handler)
        resp = self.client.post("/api/v1/worker_callback/async-x",
                                json={"hello": "world"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(flag["called"])
        self.assertEqual(flag["payload"], {"hello": "world"})

    def test_handler_exception_returns_success_false(self):
        def bad(_payload):
            raise RuntimeError("intentional")

        worker_callback.register_handler("boom", bad)
        resp = self.client.post("/api/v1/worker_callback/boom", json={})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["success"])
        self.assertIn("intentional", body["message"])

    def test_async_handler_exception_returns_success_false(self):
        async def bad(_payload):
            raise ValueError("async-fail")

        worker_callback.register_handler("boom", bad)
        resp = self.client.post("/api/v1/worker_callback/boom", json={})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["success"])
        self.assertIn("async-fail", body["message"])

class ConcurrentRegisterTest(_CallbackTestBase):
    """
    register/unregister 自带 lock，并发注册不应崩溃或丢失最终值
    """

    def test_concurrent_register(self):
        errors = []

        def _run(idx):
            try:
                for _ in range(20):
                    worker_callback.register_handler(
                        "concurrent", lambda p, i=idx: ("v", i))
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=_run, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [])
        # 至少能正常调用，不抛异常
        resp = self.client.post("/api/v1/worker_callback/concurrent",
                                json={})
        self.assertEqual(resp.status_code, 200)

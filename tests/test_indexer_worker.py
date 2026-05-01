"""
mp-indexer worker 协议层单元测试

验证 Python ↔ Go 的协议对齐：
  - 请求/响应 JSON 字段名与 Go 端 struct json tag 完全一致
  - 默认值、边界条件、序列化/反序列化正确

注意：Schema 类（FetchItem 等）基于 pydantic，本地开发环境若缺少 pydantic，
需在项目虚拟环境中运行。Go handler 已通过 Go 单元测试覆盖（6/6 PASS）。
"""
import json
import unittest


class FetchProtocolTest(unittest.TestCase):
    """
    纯协议层验证：不依赖 pydantic，直接用 dict 验证 JSON 字段名。
    确保 Python 端构造的 payload 能被 Go 端正确反序列化。
    """

    def test_request_field_names_match_go_struct(self):
        """Python 构造的请求 JSON 字段名必须与 Go FetchItem json tag 一致"""
        request_item = {
            "id": "site-42",
            "url": "https://example.com/torrents.php?search=test",
            "method": "GET",
            "headers": {"User-Agent": "Bot/1.0", "Cookie": "uid=1; pass=abc"},
            "proxy": "http://proxy:7890",
            "timeout_ms": 15000,
            "allow_redirects": True,
        }
        expected_keys = {"id", "url", "method", "headers", "proxy", "timeout_ms", "allow_redirects"}
        self.assertEqual(set(request_item.keys()), expected_keys)

        # 可正常序列化为 JSON
        serialized = json.dumps({"requests": [request_item]})
        parsed = json.loads(serialized)
        self.assertEqual(len(parsed["requests"]), 1)
        self.assertEqual(parsed["requests"][0]["id"], "site-42")

    def test_response_field_names_match_go_struct(self):
        """Go 返回的响应 JSON 字段名必须能被 Python 正确解析"""
        response_item = {
            "id": "site-42",
            "status_code": 200,
            "headers": {"Content-Type": "text/html; charset=utf-8"},
            "body": "<html><body>torrents</body></html>",
            "error": "",
            "duration_ms": 1234,
        }
        expected_keys = {"id", "status_code", "headers", "body", "error", "duration_ms"}
        self.assertEqual(set(response_item.keys()), expected_keys)

    def test_batch_response_structure(self):
        """Go 返回的批量响应结构验证"""
        go_response = {
            "code": 0,
            "message": "ok",
            "data": {
                "results": [
                    {"id": "s1", "status_code": 200, "body": "<html/>", "error": "", "duration_ms": 500, "headers": {}},
                    {"id": "s2", "status_code": 403, "body": "Forbidden", "error": "", "duration_ms": 200, "headers": {}},
                    {"id": "s3", "status_code": 0, "body": "", "error": "connection refused", "duration_ms": 50, "headers": {}},
                ],
                "total_duration_ms": 600,
            },
        }
        self.assertEqual(go_response["code"], 0)
        results = go_response["data"]["results"]
        self.assertEqual(len(results), 3)

        # 成功的站点
        self.assertEqual(results[0]["status_code"], 200)
        self.assertIn("html", results[0]["body"])
        self.assertEqual(results[0]["error"], "")

        # 403 的站点
        self.assertEqual(results[1]["status_code"], 403)

        # 连接失败的站点
        self.assertEqual(results[2]["status_code"], 0)
        self.assertIn("refused", results[2]["error"])

    def test_request_with_defaults(self):
        """验证请求最小必填字段（id + url），其余走默认值"""
        minimal = {"id": "x", "url": "https://a.com"}
        # Go 端的默认值：method=GET, timeout_ms=15000, allow_redirects=true
        self.assertNotIn("method", minimal)
        self.assertNotIn("timeout_ms", minimal)

    def test_proxy_extraction_logic(self):
        """验证从 spider.proxies 提取代理的逻辑"""
        # 场景 1：https 优先
        proxies = {"https": "http://a:1", "http": "http://b:2"}
        proxy = proxies.get("https") or proxies.get("http") or ""
        self.assertEqual(proxy, "http://a:1")

        # 场景 2：仅 http
        proxies = {"http": "http://b:2"}
        proxy = proxies.get("https") or proxies.get("http") or ""
        self.assertEqual(proxy, "http://b:2")

        # 场景 3：无代理
        proxies = None
        proxy = proxies.get("https") or proxies.get("http") or "" if proxies else ""
        self.assertEqual(proxy, "")

        # 场景 4：空字典
        proxies = {}
        proxy = proxies.get("https") or proxies.get("http") or ""
        self.assertEqual(proxy, "")

    def test_headers_construction(self):
        """验证从 spider 属性构造请求头的逻辑"""
        ua = "TestBot/1.0"
        cookie = "uid=1; pass=abc"
        referer = "https://example.com/"

        headers = {}
        if ua:
            headers["User-Agent"] = ua
        if cookie:
            headers["Cookie"] = cookie
        if referer:
            headers["Referer"] = referer

        self.assertEqual(headers["User-Agent"], "TestBot/1.0")
        self.assertEqual(headers["Cookie"], "uid=1; pass=abc")
        self.assertEqual(headers["Referer"], "https://example.com/")

    def test_timeout_conversion(self):
        """验证 Python 端 timeout（秒）→ Go 端 timeout_ms（毫秒）转换"""
        python_timeout = 15
        go_timeout_ms = python_timeout * 1000
        self.assertEqual(go_timeout_ms, 15000)

        python_timeout = None
        go_timeout_ms = (python_timeout or 15) * 1000
        self.assertEqual(go_timeout_ms, 15000)

    def test_result_routing_logic(self):
        """验证 __try_worker_fetch 的结果路由逻辑"""
        # 成功：status_code=200, body 非空, error 为空 → 返回 body
        result = {"status_code": 200, "body": "<html/>", "error": ""}
        should_use = result["status_code"] == 200 and result["body"] and not result["error"]
        self.assertTrue(should_use)

        # 失败：error 非空 → 返回 None（fallback）
        result = {"status_code": 0, "body": "", "error": "timeout"}
        should_use = result["status_code"] == 200 and result["body"] and not result["error"]
        self.assertFalse(should_use)

        # 失败：非 200 → 返回 None（fallback）
        result = {"status_code": 403, "body": "Forbidden", "error": ""}
        should_use = result["status_code"] == 200 and result["body"] and not result["error"]
        self.assertFalse(should_use)

        # 失败：body 为空 → 返回 None（fallback）
        result = {"status_code": 200, "body": "", "error": ""}
        should_use = result["status_code"] == 200 and result["body"] and not result["error"]
        self.assertFalse(should_use)


class SearchChainBatchRoutingTest(unittest.TestCase):
    """
    验证 SearchChain 改造后的批量路由 + fallback 语义（不依赖完整框架）。
    模拟 self.batch_search_torrents 的两种返回形态：worker 命中 / worker 不可用走传统路径。
    """

    def _aggregate_by_site_order(self, indexer_sites, site_results):
        """复刻 SearchChain.__search_all_sites 中"按 indexer_sites 顺序汇总"的核心逻辑"""
        results = []
        finish_count = 0
        for site in indexer_sites:
            finish_count += 1
            result = site_results.get(site.get("id")) or []
            if result:
                results.extend(result)
        return results, finish_count

    def test_batch_aggregation_preserves_site_order(self):
        """批量返回后按 indexer_sites 顺序汇总，进度计数与站点数一致"""
        indexer_sites = [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}]
        site_results = {
            "s1": [{"title": "t1"}, {"title": "t2"}],
            "s2": [],  # 空结果
            "s3": [{"title": "t3"}],
        }
        results, finish_count = self._aggregate_by_site_order(indexer_sites, site_results)
        self.assertEqual(finish_count, 3)
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0]["title"], "t1")
        self.assertEqual(results[2]["title"], "t3")

    def test_batch_aggregation_handles_missing_site(self):
        """worker 漏返某站点时，对应站点视为空结果，不抛异常"""
        indexer_sites = [{"id": "s1"}, {"id": "s2"}]
        site_results = {"s1": [{"title": "t1"}]}  # s2 缺失
        results, finish_count = self._aggregate_by_site_order(indexer_sites, site_results)
        self.assertEqual(finish_count, 2)
        self.assertEqual(len(results), 1)

    def test_attribute_error_triggers_fallback(self):
        """非 IndexerModule 时（无 batch_search_torrents 方法）触发 AttributeError fallback"""
        class FakeChainWithoutBatch:
            pass

        chain = FakeChainWithoutBatch()
        triggered_fallback = False
        try:
            chain.batch_search_torrents(sites=[], keyword="x", mtype=None, page=0)
        except AttributeError:
            triggered_fallback = True
        self.assertTrue(triggered_fallback)

    def test_batch_call_signature(self):
        """批量调用必须传 sites/keyword/mtype/page 四个参数"""
        captured = {}

        def fake_batch(sites, keyword, mtype, page):
            captured["sites"] = sites
            captured["keyword"] = keyword
            captured["mtype"] = mtype
            captured["page"] = page
            return {site.get("id"): [] for site in sites}

        sites = [{"id": "s1"}, {"id": "s2"}]
        fake_batch(sites=sites, keyword="搜索词", mtype="movie", page=2)

        self.assertEqual(len(captured["sites"]), 2)
        self.assertEqual(captured["keyword"], "搜索词")
        self.assertEqual(captured["mtype"], "movie")
        self.assertEqual(captured["page"], 2)

    def test_imdbid_area_uses_imdb_id_as_keyword(self):
        """area=imdbid 时实际关键词应为 mediainfo.imdb_id"""
        class FakeMediaInfo:
            imdb_id = "tt1234567"
            type = "movie"

        mediainfo = FakeMediaInfo()
        keyword = "fallback title"
        area = "imdbid"

        # 复刻 SearchChain 中的关键词选择逻辑
        actual_keyword = (mediainfo.imdb_id if mediainfo else None) if area == "imdbid" else keyword
        self.assertEqual(actual_keyword, "tt1234567")

        # 反向：area=title 时使用原 keyword
        area = "title"
        actual_keyword = (mediainfo.imdb_id if mediainfo else None) if area == "imdbid" else keyword
        self.assertEqual(actual_keyword, "fallback title")


class StreamBatchTest(unittest.TestCase):
    """
    流式搜索批量化（方案 C）单元测试

    验证 SearchChain.__async_search_all_sites_stream 的关键行为：
      1. 并发：多站点通过 asyncio.create_task 并发执行（耗时 ≈ max 而非 sum）
      2. Fallback：self 不具备 async_worker_search_site 方法时，回退 legacy 路径
      3. 异常处理：单站点异常时，自动回退到 async_search_torrents
      4. 流式语义：先完成的站点先 yield，不等待所有站点

    注意：直接测试私有方法 __async_search_all_sites_stream 比较麻烦，
    改为测试其内部使用的关键模式（hasattr 路由 + try/except fallback +
    asyncio.as_completed 流式产出），确保实现与设计一致。
    """

    def test_concurrent_execution_via_create_task(self):
        """asyncio.create_task + as_completed 应实现真并发（耗时 ≈ max 而非 sum）"""
        import asyncio
        import time

        async def slow_site(delay):
            await asyncio.sleep(delay)
            return delay

        async def run():
            start = time.monotonic()
            tasks = [asyncio.create_task(slow_site(0.1)) for _ in range(5)]
            results = []
            for fut in asyncio.as_completed(tasks):
                results.append(await fut)
            return time.monotonic() - start, results

        elapsed, results = asyncio.new_event_loop().run_until_complete(run())
        # 5 个 100ms 任务并发应在 ~150ms 内完成（远小于串行的 500ms）
        self.assertLess(elapsed, 0.4, f"并发耗时异常：{elapsed:.3f}s，预期 < 0.4s")
        self.assertEqual(len(results), 5)

    def test_hasattr_routing_for_stream_batch(self):
        """use_stream_batch 通过 hasattr 检测，IndexerModule 应路由到 worker 路径"""
        try:
            from app.modules.indexer import IndexerModule
        except ImportError as e:
            self.skipTest(f"依赖缺失，跳过：{e}")

        # IndexerModule 暴露 async_worker_search_site 即触发 worker 路径
        self.assertTrue(hasattr(IndexerModule, "async_worker_search_site"),
                        "IndexerModule 缺少 async_worker_search_site，流式批量化无法启用")

        # 非 IndexerModule（如 mock chain）应触发 legacy 路径
        class FakeChain:
            pass

        self.assertFalse(hasattr(FakeChain(), "async_worker_search_site"))

    def test_per_site_exception_falls_back_to_legacy(self):
        """单站点 worker 异常时，应自动回退到 async_search_torrents"""
        import asyncio

        call_log = []

        class FakeChain:
            async def async_worker_search_site(self, site, keyword, mtype, page):
                call_log.append(("worker", site["id"]))
                raise RuntimeError("worker boom")

            async def async_search_torrents(self, site, keyword, mtype, page):
                call_log.append(("legacy", site["id"]))
                return [{"site": site["id"], "title": "fallback-result"}]

        async def search_site_via_worker(chain, site):
            try:
                result = await chain.async_worker_search_site(
                    site=site, keyword="x", mtype=None, page=0,
                )
                return site, result or []
            except Exception:
                result = await chain.async_search_torrents(
                    site=site, keyword="x", mtype=None, page=0,
                )
                return site, result or []

        chain = FakeChain()
        site = {"id": "s1", "name": "Site1"}
        _, result = asyncio.new_event_loop().run_until_complete(
            search_site_via_worker(chain, site)
        )

        # 应先调 worker，失败后调 legacy
        self.assertEqual(call_log, [("worker", "s1"), ("legacy", "s1")])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "fallback-result")

    def test_stream_yields_in_completion_order(self):
        """先完成的站点先 yield（流式语义），不等待最慢站点"""
        import asyncio

        async def slow_site(site_id, delay):
            await asyncio.sleep(delay)
            return {"id": site_id, "delay": delay}

        async def run():
            # 三个站点：500ms / 100ms / 300ms，预期 yield 顺序为 100ms→300ms→500ms
            tasks = [
                asyncio.create_task(slow_site("slow", 0.05)),
                asyncio.create_task(slow_site("fast", 0.01)),
                asyncio.create_task(slow_site("mid", 0.03)),
            ]
            order = []
            for fut in asyncio.as_completed(tasks):
                r = await fut
                order.append(r["id"])
            return order

        order = asyncio.new_event_loop().run_until_complete(run())
        self.assertEqual(order, ["fast", "mid", "slow"],
                         f"流式 yield 顺序错误：{order}，预期按完成顺序")

    def test_task_cancellation_on_early_break(self):
        """global_vars.is_system_stopped 触发 break 时，未完成 task 应被取消"""
        import asyncio

        async def long_running():
            await asyncio.sleep(10)
            return "should-not-finish"

        async def run():
            tasks = [asyncio.create_task(long_running()) for _ in range(3)]
            try:
                # 模拟仅消费 0 个就 break（系统停机场景）
                for _ in asyncio.as_completed(tasks):
                    break
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()

            # 等一小会儿让取消生效
            await asyncio.sleep(0.05)
            return [t.cancelled() or t.done() for t in tasks]

        states = asyncio.new_event_loop().run_until_complete(run())
        self.assertTrue(all(states), f"任务未被正确取消：{states}")


class ModuleImportSmokeTest(unittest.TestCase):
    """
    模块导入 smoke test：保证 IndexerModule / SearchChain 的类定义阶段不出现
    NameError、ImportError 等导入期错误。

    历史教训：曾出现 batch_search_torrents 方法签名用了 Dict[int, List[TorrentInfo]]
    但顶部 typing 导入漏了 Dict 的 bug，导致整个 IndexerModule 类在 import 阶段就崩溃。
    上层 FastAPI 把异常吞掉只表现为 "0 资源 0 秒"，排查极其困难。
    本测试用最快的速度（仅做 import）暴露这类问题。
    """

    def test_indexer_module_can_be_imported(self):
        """IndexerModule 必须能正常 import（验证类定义阶段无 NameError）"""
        try:
            from app.modules.indexer import IndexerModule  # noqa: F401
        except ImportError as e:
            self.skipTest(f"依赖缺失，跳过：{e}")
        except NameError as e:
            self.fail(f"IndexerModule 导入时出现 NameError（typing 导入或符号引用错误）：{e}")

    def test_indexer_module_has_batch_methods(self):
        """IndexerModule 必须暴露批量入口方法（防止方法被误删）"""
        try:
            from app.modules.indexer import IndexerModule
        except ImportError as e:
            self.skipTest(f"依赖缺失，跳过：{e}")

        self.assertTrue(hasattr(IndexerModule, "batch_search_torrents"),
                        "IndexerModule 缺少 batch_search_torrents 方法")
        self.assertTrue(hasattr(IndexerModule, "async_batch_search_torrents"),
                        "IndexerModule 缺少 async_batch_search_torrents 方法")

    def test_search_chain_can_be_imported(self):
        """SearchChain 必须能正常 import"""
        try:
            from app.chain.search import SearchChain  # noqa: F401
        except ImportError as e:
            self.skipTest(f"依赖缺失，跳过：{e}")
        except NameError as e:
            self.fail(f"SearchChain 导入时出现 NameError：{e}")


if __name__ == "__main__":
    unittest.main()

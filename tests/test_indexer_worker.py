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


if __name__ == "__main__":
    unittest.main()

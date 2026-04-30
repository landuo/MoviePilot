#!/usr/bin/env python3
"""
mp-indexer 批量调用 vs 串行调用 端到端基准测试

测试目标：
  量化"1 次 IPC 批量请求 N 个 URL" 相对 "N 次 IPC 各请求 1 个 URL" 的实际收益。

测试架构：
  [Bench] --(UDS)--> [mp-indexer worker] --(HTTP)--> [本地 mock server]

  - mock server：本地 http.server，可控延迟，模拟真实站点 RTT
  - worker：复用项目内的 mp-indexer 二进制（workers/bin/mp-indexer）
  - bench：本脚本，分别用批量 / 串行模式发请求，对比耗时

用法：
  python3 scripts/bench_indexer_batch.py                        # 默认 20 站点 × 200ms 延迟
  python3 scripts/bench_indexer_batch.py --sites 30 --delay 100 # 自定义
  python3 scripts/bench_indexer_batch.py --rounds 5             # 多轮采样

输出：
  批量模式 / 串行模式 各自的 P50 / P95 / 平均 / 最大耗时，以及收益百分比。
"""
import argparse
import json
import socket
import statistics
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKER_BIN = ROOT / "workers" / "bin" / "mp-indexer"


# ---------- mock HTTP server（模拟站点） ----------

class _DelayedHandler(BaseHTTPRequestHandler):
    """可控延迟的 HTTP 处理器，返回固定大小的伪 HTML"""

    delay_ms = 200
    body_size = 8192  # 模拟真实站点 HTML 大小

    def do_GET(self):  # noqa: N802
        time.sleep(self.delay_ms / 1000.0)
        body = ("<html><body>" + "x" * self.body_size + "</body></html>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args, **_kwargs):  # 静默
        pass


def start_mock_server(delay_ms: int) -> tuple[ThreadingHTTPServer, str]:
    """启动 mock server 监听随机端口（多线程，允许并发响应），返回 (server, base_url)"""
    handler_cls = type("Handler", (_DelayedHandler,), {"delay_ms": delay_ms})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


# ---------- worker 启动 / 通信 ----------

def start_worker(socket_path: str) -> subprocess.Popen:
    """启动 mp-indexer worker，返回进程句柄"""
    if not WORKER_BIN.exists():
        sys.exit(f"未找到 worker 二进制：{WORKER_BIN}\n请先执行：cd workers && make build-indexer")

    proc = subprocess.Popen(
        [str(WORKER_BIN), "--socket", socket_path, "--log-level", "warn"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    # 等 socket 就绪
    for _ in range(50):
        if Path(socket_path).exists():
            time.sleep(0.05)  # 多给一点时间让 listener 注册完成
            return proc
        time.sleep(0.1)
    proc.terminate()
    sys.exit("worker 启动超时（5s 内未创建 socket）")


def uds_post(socket_path: str, path: str, payload: dict) -> dict:
    """通过 UDS 发起 POST 请求，返回解析后的 JSON 响应"""
    body = json.dumps(payload).encode()
    request = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: localhost\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode() + body

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(socket_path)
    try:
        sock.sendall(request)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        sock.close()

    raw = b"".join(chunks)
    # 拆 HTTP header 和 body
    header_end = raw.find(b"\r\n\r\n")
    if header_end < 0:
        raise RuntimeError("非法 HTTP 响应（无 header 结束符）")
    headers_blob = raw[:header_end].decode("latin-1").lower()
    body_bytes = raw[header_end + 4:]

    # Go net/http 默认走 chunked transfer-encoding，需手工解码
    if "transfer-encoding: chunked" in headers_blob:
        body_bytes = _decode_chunked(body_bytes)

    return json.loads(body_bytes.decode().strip())


def _decode_chunked(data: bytes) -> bytes:
    """简易 chunked transfer-encoding 解码"""
    out = bytearray()
    pos = 0
    while pos < len(data):
        crlf = data.find(b"\r\n", pos)
        if crlf < 0:
            break
        size_line = data[pos:crlf].split(b";", 1)[0].strip()
        try:
            size = int(size_line, 16)
        except ValueError:
            break
        pos = crlf + 2
        if size == 0:
            break
        out.extend(data[pos:pos + size])
        pos += size + 2  # skip \r\n after chunk
    return bytes(out)


# ---------- 两种调用模式 ----------

def call_batch(socket_path: str, urls: list[str]) -> float:
    """批量模式：1 次 IPC 请求 N 个 URL，返回耗时（毫秒）"""
    payload = {
        "requests": [
            {"id": f"site-{i}", "url": url, "timeout_ms": 30000}
            for i, url in enumerate(urls)
        ]
    }
    t0 = time.perf_counter()
    resp = uds_post(socket_path, "/api/v1/fetch", payload)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if resp.get("code") != 0:
        raise RuntimeError(f"worker 返回错误：{resp}")
    results = resp["data"]["results"]
    if len(results) != len(urls):
        raise RuntimeError(f"返回结果数不匹配：期望 {len(urls)}，实际 {len(results)}")
    return elapsed_ms


def call_serial_threaded(socket_path: str, urls: list[str]) -> float:
    """
    串行模式（线程池版）：N 次 IPC 各请求 1 个 URL，但用线程池模拟原 ThreadPoolExecutor 路径，
    返回总耗时（毫秒）。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _one(url: str) -> dict:
        return uds_post(
            socket_path,
            "/api/v1/fetch",
            {"requests": [{"id": "x", "url": url, "timeout_ms": 30000}]},
        )

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(urls)) as ex:
        futures = [ex.submit(_one, u) for u in urls]
        for f in as_completed(futures):
            resp = f.result()
            if resp.get("code") != 0:
                raise RuntimeError(f"worker 返回错误：{resp}")
    return (time.perf_counter() - t0) * 1000


# ---------- 统计 ----------

def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def print_stats(label: str, samples: list[float]) -> None:
    print(f"  {label}:")
    print(f"    平均: {statistics.mean(samples):8.1f} ms")
    print(f"    P50 : {percentile(samples, 0.50):8.1f} ms")
    print(f"    P95 : {percentile(samples, 0.95):8.1f} ms")
    print(f"    最大: {max(samples):8.1f} ms")
    print(f"    最小: {min(samples):8.1f} ms")


# ---------- 主流程 ----------

def main() -> None:
    parser = argparse.ArgumentParser(description="mp-indexer 批量 vs 串行 基准测试")
    parser.add_argument("--sites", type=int, default=20, help="模拟站点数（默认 20）")
    parser.add_argument("--delay", type=int, default=200, help="单站点延迟 ms（默认 200）")
    parser.add_argument("--rounds", type=int, default=5, help="采样轮数（默认 5）")
    parser.add_argument("--warmup", type=int, default=2, help="预热轮数（默认 2，结果不计入）")
    args = parser.parse_args()

    print(f"配置：{args.sites} 站点 × {args.delay}ms 延迟，{args.warmup} 预热 + {args.rounds} 采样")
    print()

    # 启动 mock server
    mock_server, base_url = start_mock_server(args.delay)
    urls = [f"{base_url}/site{i}" for i in range(args.sites)]
    print(f"mock server 已启动：{base_url}")

    # 启动 worker
    socket_path = "/tmp/mp-indexer-bench.sock"
    Path(socket_path).unlink(missing_ok=True)
    worker = start_worker(socket_path)
    print(f"worker 已启动 PID={worker.pid}, socket={socket_path}")
    print()

    try:
        # 预热
        print(f"预热中（{args.warmup} 轮）...")
        for _ in range(args.warmup):
            call_batch(socket_path, urls)
            call_serial_threaded(socket_path, urls)

        # 采样
        print(f"采样中（{args.rounds} 轮）...")
        batch_samples = []
        serial_samples = []
        for i in range(args.rounds):
            b = call_batch(socket_path, urls)
            s = call_serial_threaded(socket_path, urls)
            batch_samples.append(b)
            serial_samples.append(s)
            print(f"  round {i + 1}: batch={b:7.1f}ms  serial(threaded)={s:7.1f}ms")

        print()
        print("=" * 60)
        print("结果统计")
        print("=" * 60)
        print_stats("批量模式 (1 IPC × N URL)", batch_samples)
        print_stats("串行模式 (N IPC × 1 URL, 线程池并发)", serial_samples)

        avg_batch = statistics.mean(batch_samples)
        avg_serial = statistics.mean(serial_samples)
        diff = avg_serial - avg_batch
        pct = (diff / avg_serial * 100) if avg_serial > 0 else 0
        print()
        print(f"  绝对节省：{diff:+.1f} ms / 次")
        print(f"  相对收益：{pct:+.1f}%")
        ipc_overhead_per_call = diff / max(args.sites - 1, 1)
        print(f"  推算单次 IPC 净开销：~{ipc_overhead_per_call:.2f} ms")

    finally:
        worker.terminate()
        worker.wait(timeout=5)
        Path(socket_path).unlink(missing_ok=True)
        mock_server.shutdown()


if __name__ == "__main__":
    main()

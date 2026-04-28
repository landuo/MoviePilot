"""
Worker 客户端 SDK

负责通过 Unix Domain Socket 与外部 Go worker 通信，提供：
1. 单 Worker 的同步调用接口（call / call_or_raise）
2. 健康检查与自动降级判定
3. 全局 WorkerClientManager：管理所有 worker 客户端 + 后台健康巡检
"""
import threading
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from app.core.config import settings
from app.log import logger
from app.schemas.worker import (
    WorkerCallError,
    WorkerNotAvailable,
    WorkerTimeoutError,
)
from app.utils.singleton import SingletonClass

# 健康巡检线程的 join 超时（秒），避免主进程退出时被卡住
_HEALTH_THREAD_JOIN_TIMEOUT = 5

class WorkerClient:
    """
    单个 worker 的客户端

    - 内部缓存一个 httpx.Client（带 UDS transport），按需懒加载并在
      worker 标记为不可用或显式 close() 时释放
    - 健康状态由 WorkerClientManager 后台巡检维护
    - 调用方通过 is_available() 判断是否可用，不可用时应主动降级到 Python 实现
    """

    def __init__(self, worker_name: str):
        self._worker_name = worker_name
        self._healthy = False
        self._fail_count = 0
        self._lock = threading.RLock()
        self._client: Optional[httpx.Client] = None

    @property
    def worker_name(self) -> str:
        return self._worker_name

    @property
    def socket_path(self) -> Path:
        """
        Unix Socket 文件路径
        """
        return settings.WORKER_SOCKET_PATH / f"mp-{self._worker_name}.sock"

    def _ensure_client(self) -> httpx.Client:
        """
        懒加载 httpx 客户端，配置 UDS transport
        """
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:
                return self._client
            transport = httpx.HTTPTransport(uds=str(self.socket_path))
            # base_url 必须是合法 URL，主机名仅作占位
            self._client = httpx.Client(
                transport=transport,
                base_url="http://mp-worker",
                timeout=settings.WORKER_RPC_TIMEOUT,
            )
            return self._client

    def is_available(self) -> bool:
        """
        worker 是否可用（综合配置开关 + 健康状态）

        调用方应在每次业务调用前判断此方法，不可用时降级到 Python 实现
        """
        if not settings.is_worker_enabled(self._worker_name):
            return False
        if not self.socket_path.exists():
            return False
        return self._healthy

    def call(
        self,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        同步调用 worker 接口

        :return: 成功返回响应 data 字段；失败返回 None（让调用方降级）
        """
        try:
            return self.call_or_raise(path, payload, timeout)
        except WorkerNotAvailable:
            return None
        except WorkerCallError as e:
            logger.warning(f"Worker {self._worker_name} 调用 {path} 失败：{e}")
            return None

    def call_or_raise(
        self,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        同步调用 worker 接口，失败抛异常

        :raises WorkerNotAvailable: worker 未启用或 socket 不存在
        :raises WorkerTimeoutError: 调用超时
        :raises WorkerCallError: 其他错误（连接失败、HTTP 错误、业务错误码）
        """
        if not settings.is_worker_enabled(self._worker_name):
            raise WorkerNotAvailable(f"Worker {self._worker_name} 未启用")
        if not self.socket_path.exists():
            raise WorkerNotAvailable(
                f"Worker {self._worker_name} socket 不存在：{self.socket_path}"
            )

        client = self._ensure_client()
        request_timeout = timeout if timeout is not None else settings.WORKER_RPC_TIMEOUT

        try:
            response = client.post(path, json=payload or {}, timeout=request_timeout)
        except httpx.TimeoutException as e:
            raise WorkerTimeoutError(f"调用 {path} 超时：{e}") from e
        except httpx.RequestError as e:
            raise WorkerCallError(f"调用 {path} 连接失败：{e}") from e

        if response.status_code != 200:
            raise WorkerCallError(
                f"调用 {path} HTTP 错误：{response.status_code} {response.text[:200]}"
            )

        try:
            body = response.json()
        except ValueError as e:
            raise WorkerCallError(f"调用 {path} 响应非 JSON：{e}") from e

        code = body.get("code", 0)
        if code != 0:
            raise WorkerCallError(
                f"调用 {path} 业务错误：code={code} message={body.get('message')}"
            )
        return body.get("data") or {}

    def health_check(self) -> bool:
        """
        主动健康检查，更新内部健康状态
        """
        if not settings.is_worker_enabled(self._worker_name):
            self._mark_unhealthy()
            return False
        if not self.socket_path.exists():
            self._mark_unhealthy()
            return False
        try:
            client = self._ensure_client()
            response = client.get("/api/v1/health", timeout=3)
            if response.status_code == 200:
                self._mark_healthy()
                return True
        except Exception as e:
            logger.debug(f"Worker {self._worker_name} 健康检查异常：{e}")
        self._mark_unhealthy()
        return False

    def _mark_healthy(self) -> None:
        with self._lock:
            if not self._healthy:
                logger.info(f"Worker {self._worker_name} 已上线")
            self._healthy = True
            self._fail_count = 0

    def _mark_unhealthy(self) -> None:
        """
        累加失败计数，超过阈值后标记为不可用并主动释放底层连接。

        释放连接的原因：httpx.Client 内部维护 keep-alive 连接池，
        指向的是某个 UDS inode；若该 socket 文件被 worker 重启时重新创建，
        旧连接不会自动失效，会持续报 BrokenPipe。下次健康检查会重建连接。
        """
        with self._lock:
            self._fail_count += 1
            if self._healthy and self._fail_count >= settings.WORKER_HEALTH_FAIL_THRESHOLD:
                logger.warning(
                    f"Worker {self._worker_name} 连续 {self._fail_count} 次健康检查失败，"
                    f"标记为不可用，业务将降级到 Python 实现"
                )
                self._healthy = False
                self._close_client_locked()

    def close(self) -> None:
        """
        关闭底层 httpx 连接并重置健康状态。
        """
        with self._lock:
            self._close_client_locked()
            self._healthy = False
            self._fail_count = 0

    def _close_client_locked(self) -> None:
        """
        前置条件：已持有 self._lock。
        """
        if self._client is None:
            return
        try:
            self._client.close()
        except Exception as e:
            logger.debug(f"关闭 worker {self._worker_name} 客户端异常：{e}")
        self._client = None

class WorkerClientManager(metaclass=SingletonClass):
    """
    全局 worker 客户端管理器

    职责：
    - 维护各个 worker 的 WorkerClient 单例
    - 后台线程定期健康巡检
    - 应用启停时统一启动/关闭
    """

    def __init__(self):
        self._clients: Dict[str, WorkerClient] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._health_thread: Optional[threading.Thread] = None

    def get(self, worker_name: str) -> WorkerClient:
        """
        获取指定名称的 worker 客户端，按需创建
        """
        with self._lock:
            client = self._clients.get(worker_name)
            if client is None:
                client = WorkerClient(worker_name)
                self._clients[worker_name] = client
            return client

    def start(self) -> None:
        """
        启动后台健康巡检线程
        """
        if (settings.WORKER_MODE or "python").lower() == "python":
            logger.debug("WORKER_MODE=python，跳过启动 WorkerClientManager")
            return
        with self._lock:
            if self._health_thread is not None and self._health_thread.is_alive():
                return
            self._stop_event.clear()
            self._health_thread = threading.Thread(
                target=self._health_loop,
                name="WorkerHealthChecker",
                daemon=True,
            )
            self._health_thread.start()
            logger.info(
                f"WorkerClientManager 已启动，健康巡检间隔 "
                f"{settings.WORKER_HEALTH_INTERVAL} 秒"
            )

    def stop(self) -> None:
        """
        停止健康巡检并关闭所有客户端
        """
        self._stop_event.set()
        thread = self._health_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=_HEALTH_THREAD_JOIN_TIMEOUT)
        self._health_thread = None
        with self._lock:
            for client in self._clients.values():
                client.close()
            logger.debug("所有 worker 客户端已关闭")

    def _health_loop(self) -> None:
        """
        健康巡检主循环
        """
        # 启动后立即跑一次，缩短首次可用时间
        self._check_all()
        while not self._stop_event.is_set():
            interval = max(settings.WORKER_HEALTH_INTERVAL, 5)
            if self._stop_event.wait(timeout=interval):
                break
            self._check_all()

    def _check_all(self) -> None:
        """
        遍历当前已注册的客户端做一次健康检查
        """
        with self._lock:
            clients = list(self._clients.values())
        for client in clients:
            try:
                client.health_check()
            except Exception as e:
                logger.debug(f"Worker {client.worker_name} 健康检查异常：{e}")

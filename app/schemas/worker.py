from typing import Any, Optional

from pydantic import BaseModel, Field

class WorkerError(Exception):
    """Worker 调用相关异常基类"""

class WorkerNotAvailable(WorkerError):
    """Worker 未启用或不可用（配置关闭、socket 不存在、健康检查失败）"""

class WorkerCallError(WorkerError):
    """Worker 调用失败（连接错误、HTTP 错误、业务错误码）"""

class WorkerTimeoutError(WorkerCallError):
    """Worker 调用超时"""

class WorkerResponse(BaseModel):
    """Worker 统一响应格式"""

    code: int = 0
    message: str = "ok"
    data: Optional[Any] = None

class WorkerHealth(BaseModel):
    """Worker 健康检查响应"""

    status: str
    uptime_sec: int = 0
    version: str = ""

class WatcherFileEvent(BaseModel):
    """mp-watcher 推送的文件事件回调载荷"""

    event_id: str
    watch_id: str
    event_type: str = Field(description="created / moved / modified")
    storage: str = "local"
    src_path: str
    dest_path: str = ""
    file_size: int = 0
    mtime_unix: int = 0
    is_directory: bool = False

class TransferMode:
    """mp-transfer 支持的整理模式（与 workers/mp-transfer/internal/ops/ops.go 对齐）"""

    COPY = "copy"
    MOVE = "move"
    LINK = "link"
    SOFTLINK = "softlink"

    ALL = (COPY, MOVE, LINK, SOFTLINK)

class TransferRequest(BaseModel):
    """mp-transfer 的 /api/v1/transfer 请求载荷"""

    mode: str = Field(description="copy / move / link / softlink")
    src: str = Field(description="源文件绝对路径")
    dst: str = Field(description="目标文件绝对路径")

class TransferResult(BaseModel):
    """mp-transfer 的 /api/v1/transfer 响应数据"""

    mode: str
    bytes: int = 0
    duration_ms: int = 0


# ---------- mp-indexer ----------

class FetchItem(BaseModel):
    """mp-indexer 批量 HTTP 请求中的单项"""

    id: str = Field(description="请求标识，用于关联响应")
    url: str
    method: str = "GET"
    headers: dict = Field(default_factory=dict)
    proxy: str = ""
    timeout_ms: int = 15000
    allow_redirects: bool = True

class FetchRequest(BaseModel):
    """mp-indexer 的 POST /api/v1/fetch 请求载荷"""

    requests: list[FetchItem]

class FetchResultItem(BaseModel):
    """mp-indexer 批量 HTTP 响应中的单项"""

    id: str
    status_code: int = 0
    headers: dict = Field(default_factory=dict)
    body: str = ""
    error: str = ""
    duration_ms: int = 0

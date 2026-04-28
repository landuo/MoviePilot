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

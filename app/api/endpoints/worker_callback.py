"""
Worker → Python 内部回调路由

接收 Go worker 子进程通过 HTTP 推送过来的事件。

设计说明：
- 仅监听 127.0.0.1，依赖本机进程隔离，不做 token 鉴权
- 路由前缀 /api/v1/worker_callback/{worker_name}
- 各 worker 的具体处理逻辑由对应模块在启动时通过 register_handler 注册
"""
import inspect
import threading
from typing import Any, Callable, Dict

from fastapi import APIRouter, HTTPException, Request

from app import schemas
from app.log import logger

router = APIRouter()

# worker_name -> handler，handler 接收 dict payload，可同步或异步
_handlers: Dict[str, Callable[[Dict[str, Any]], Any]] = {}
# 保护 _handlers 的注册/注销过程；读取在请求路径上为 dict.get，本身是线程安全的原子操作
_handlers_lock = threading.Lock()

def register_handler(worker_name: str,
                     handler: Callable[[Dict[str, Any]], Any]) -> None:
    """
    注册指定 worker 的回调处理函数

    :param worker_name: worker 名称（如 "watcher"）
    :param handler: 处理函数，接收 payload dict；可以是同步或异步函数
    """
    with _handlers_lock:
        if worker_name in _handlers:
            logger.warning(f"Worker 回调处理函数被覆盖：{worker_name}")
        _handlers[worker_name] = handler

def unregister_handler(worker_name: str) -> None:
    """
    注销指定 worker 的回调处理函数（用于优雅停机）
    """
    with _handlers_lock:
        _handlers.pop(worker_name, None)

@router.post("/{worker_name}", summary="Worker 回调接收（仅内部）",
             response_model=schemas.Response)
async def receive_callback(worker_name: str, request: Request) -> Any:
    """
    接收 worker 推送的事件
    """
    handler = _handlers.get(worker_name)
    if handler is None:
        raise HTTPException(status_code=404,
                            detail=f"未注册 worker={worker_name} 的回调处理函数")

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"非法 JSON 载荷：{e}") from e

    try:
        if inspect.iscoroutinefunction(handler):
            await handler(payload)
        else:
            result = handler(payload)
            # 极少数场景下同步函数仍返回 awaitable（如装饰器包装），统一 await 一次
            if inspect.isawaitable(result):
                await result
    except Exception as e:
        logger.error(f"Worker {worker_name} 回调处理异常：{e}", exc_info=True)
        return schemas.Response(success=False, message=str(e))

    return schemas.Response(success=True)

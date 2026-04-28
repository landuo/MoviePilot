"""
Monitor 模块的 mp-watcher worker 适配层

把"调 worker 推送配置 / 注册回调 / 解析事件"这部分逻辑独立成模块，
让 app/monitor.py 主流程更清晰，也让单元测试不必拉起整个 monitor
依赖链（chain.transfer / cache / scheduler 等）。

外部 API：
    try_configure_watcher(monitor_dirs, on_event) -> set[str]
        - monitor_dirs：DirectoryHelper.get_download_dirs() 返回的列表
        - on_event：成功接管后，回调推送来的事件会被转换成
                    (text, src_path, file_size, fake_event) 调用 on_event
        - 返回成功被 worker 接管的 watch_id 集合；失败返回空 set 让上游回退
    release_watcher() -> None
        - 注销回调 handler，stop() 时调用
"""
from pathlib import Path
from typing import Any, Callable, Dict, List, Set

from app.api.endpoints.worker_callback import register_handler, unregister_handler
from app.log import logger
from app.schemas.worker import WorkerCallError, WorkerNotAvailable
from app.utils.worker_client import WorkerClientManager

# worker_callback 中的 worker 名（也是 socket 文件名 mp-{name}.sock 的 name 部分）
WATCHER_WORKER_NAME = "watcher"


class WorkerFileEvent:
    """
    mp-watcher 回调事件适配对象。

    用最小字段模拟 watchdog 的 FileSystemEvent，使现有 event_handler
    无需区分事件来源。当前 event_handler 仅消费 is_directory，
    其他字段保留供未来扩展或调试。
    """

    __slots__ = ("src_path", "is_directory", "event_type")

    def __init__(self, src_path: str, is_directory: bool, event_type: str):
        self.src_path = src_path
        self.is_directory = is_directory
        self.event_type = event_type


def make_watch_id(mon_dir) -> str:
    """生成 worker 用的 watch_id：storage 加 download_path，便于回调反查。"""
    return f"{mon_dir.storage}::{mon_dir.download_path}"


def _is_valid_local_monitor_dir(d) -> bool:
    """筛选出可被 worker 接管的本地监控目录。"""
    if d.storage != "local":
        return False
    if d.monitor_type != "monitor":
        return False
    if not d.library_path:
        return False
    # library 在 download 子目录会形成循环触发，过滤掉
    try:
        if Path(d.library_path).is_relative_to(Path(d.download_path)):
            return False
    except (TypeError, ValueError):
        return False
    return True


def try_configure_watcher(
    monitor_dirs: List,
    on_event: Callable[[str, str, int, WorkerFileEvent], None],
) -> Set[str]:
    """
    尝试通过 mp-watcher worker 接管所有本地监控目录。

    :param monitor_dirs: DirectoryHelper.get_download_dirs() 返回的目录列表
    :param on_event: 收到回调事件后调用的函数，签名 (text, src_path, file_size, event)
    :return: 成功接管的 watch_id 集合；失败返回空 set，调用方应继续走 watchdog
    """
    local_dirs = [d for d in monitor_dirs if _is_valid_local_monitor_dir(d)]
    if not local_dirs:
        return set()

    client = WorkerClientManager().get(WATCHER_WORKER_NAME)

    # init 时机早于第一次后台健康巡检，主动触发一次以缩短首次可用时间
    if not client.is_available():
        client.health_check()
    if not client.is_available():
        logger.debug("mp-watcher 不可用，本地目录监控走 watchdog 路径")
        return set()

    watches = [
        {
            "watch_id": make_watch_id(d),
            "path": str(Path(d.download_path)),
            "recursive": True,
        }
        for d in local_dirs
    ]
    try:
        data = client.call_or_raise("/api/v1/configure",
                                    {"watches": watches})
    except (WorkerNotAvailable, WorkerCallError) as e:
        logger.warning(f"mp-watcher 配置推送失败，回退到 watchdog：{e}")
        return set()

    data = data or {}
    if data.get("warning"):
        logger.warning(f"mp-watcher 部分目录失败：{data['warning']}")
    logger.info(
        f"mp-watcher 已接管本地目录监控：成功 "
        f"{data.get('added', 0)}/{data.get('total', len(watches))} 个"
    )

    register_handler(WATCHER_WORKER_NAME,
                     _make_callback_handler(on_event))

    return {w["watch_id"] for w in watches}


def release_watcher() -> None:
    """注销 mp-watcher 的回调处理器。stop() 时调用，重复调用安全。"""
    try:
        unregister_handler(WATCHER_WORKER_NAME)
    except Exception as e:
        logger.debug(f"注销 mp-watcher 回调异常: {e}")


def _make_callback_handler(
    on_event: Callable[[str, str, int, WorkerFileEvent], None],
) -> Callable[[Dict[str, Any]], None]:
    """构造回调入口：解析 payload，过滤事件类型，构造 WorkerFileEvent 后转发。"""

    def handler(payload: Dict[str, Any]) -> None:
        try:
            event_type = payload.get("event_type") or ""
            # 仅处理 created / modified；moved 事件 dest 由后续 created 关联
            if event_type not in ("created", "modified"):
                return

            src_path = payload.get("src_path") or ""
            if not src_path:
                return

            event = WorkerFileEvent(
                src_path=src_path,
                is_directory=bool(payload.get("is_directory")),
                event_type=event_type,
            )
            text = "创建" if event_type == "created" else "修改"
            file_size = payload.get("file_size") or 0
            on_event(text, src_path, file_size, event)
        except Exception as e:
            logger.error(f"mp-watcher 回调处理异常: {e}")

    return handler

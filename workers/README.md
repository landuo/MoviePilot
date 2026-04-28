# MoviePilot Workers

外置高性能子进程集合，用 Go 实现。每个 worker 是独立的二进制，通过 Unix Domain Socket + HTTP/JSON 与 Python 主进程通信。

## 设计目标

- **降低 NAS 资源占用**：把吃 CPU/内存/IO 的"肌肉活"剥离 Python 主进程
- **零侵入**：Python 端通过 `WORKER_MODE` 配置切换，默认 `python` 模式与改造前完全一致
- **崩溃可降级**：worker 不可用时主进程自动 fallback 到 Python 实现

## 现有 worker

| 名称 | 职责 | 状态 |
|---|---|---|
| `mp-watcher` | 本地目录文件监控（fsnotify） | P1-A 已交付 |
| `mp-mover`   | 大文件转移（硬链/拷贝/校验） | P2（规划） |
| `mp-indexer` | 多站点搜索调度 | P3（规划） |

## 目录结构

```
workers/
├── go.work               # Go workspace 多模块声明
├── shared/               # 跨 worker 共享库（transport / log / lifecycle）
├── mp-watcher/           # P1
├── mp-mover/             # P2
└── mp-indexer/           # P3
```

## 通信协议

- **传输**：Unix Domain Socket（路径：`${CONFIG_DIR}/sockets/mp-{name}.sock`）
- **应用层**：HTTP/1.1 + JSON
- **响应格式**：`{"code": 0, "message": "ok", "data": ...}`
- **公共接口**：所有 worker 必须实现 `GET /api/v1/health`、`GET /api/v1/info`、`POST /api/v1/shutdown`

## 启用方式

在 `config/app.env` 中：

```env
# 启用全部 worker
WORKER_MODE=worker

# 或按需启用部分 worker
WORKER_MODE=hybrid
WORKER_ENABLED=watcher
```

默认 `WORKER_MODE=python`，老用户升级零感知。

## 构建

```bash
cd workers
make build           # 构建当前平台所有 worker，产物在 workers/bin/
make build-watcher   # 仅构建 mp-watcher
make release         # 通过 goreleaser 多平台构建，产物在 workers/dist/
```

`make build` 仅用于本地开发。容器内运行时由 `docker/entrypoint.sh` 在
`/app/bin/mp-{name}` 路径加载，二者**不会**自动同步——若要在本地容器里
测试自编译版本，需要手动把产物 cp 到容器内或在 Dockerfile 中改用本地
COPY 替代 GitHub Releases 下载。

## 部署

Docker 镜像构建时从 GitHub Releases 下载预编译二进制到 `/app/bin/`，详见 `docker/Dockerfile`。

---

## mp-watcher

基于 [fsnotify](https://github.com/fsnotify/fsnotify) 的本地目录监控，替代 Python 端 `watchdog`。

### 启动参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `--socket` | ✅ | UDS 文件路径，例：`/config/sockets/mp-watcher.sock` |
| `--callback-url` | ✅ | Python 回调 URL，例：`http://127.0.0.1:3001/api/v1/worker_callback/watcher` |
| `--log-level` | ❌ | `debug` / `info`（默认）/ `warn` / `error` |
| `--log-format` | ❌ | `json`（默认）/ `text` |

### 业务接口

除 shared 自带的 `/health` `/info` `/shutdown` 外，额外暴露：

#### `POST /api/v1/configure`

全量重置监听目录。Python 端在启动 / 配置变更时调用。

请求：
```json
{
  "watches": [
    {"watch_id": "w1", "path": "/data/movies", "recursive": true},
    {"watch_id": "w2", "path": "/data/tv",     "recursive": true}
  ]
}
```

响应：
```json
{"code": 0, "message": "ok",
 "data": {"added": 2, "removed": 0, "total": 2}}
```

部分目录失败时仍返回 `code=0`，`data.warning` 字段会记录失败明细，由 Python 端处理。

#### `GET /api/v1/watch_status`

返回当前监听快照。

响应：
```json
{"code": 0, "data": {
  "count": 2,
  "watches": [
    {"watch_id": "w1", "path": "/data/movies", "recursive": true,
     "dir_count": 142, "event_count": 37}
  ]
}}
```

### 事件回调载荷

mp-watcher 主动推送给 `--callback-url` 的事件：

```json
{
  "event_id": "abcd-1",
  "watch_id": "w1",
  "event_type": "created",
  "storage": "local",
  "src_path": "/data/movies/foo.mkv",
  "dest_path": "",
  "file_size": 1234567,
  "mtime_unix": 1700000000,
  "is_directory": false
}
```

字段含义与 Python 端 `app/schemas/worker.py:WatcherFileEvent` 严格对齐，**修改任一端必须同步另一端**。

### 设计要点

- **不做去抖、不做扩展名过滤**——原始事件透传给 Python，复用现有 `TTLCache` 与扩展名过滤逻辑，避免双端规则维护
- **递归 watch 动态扩展**：新建子目录时自动加入 watch，避免漏听
- **回调背压保护**：in-flight 推送数限制为 8，超限时丢弃事件并 `warn` 日志（防止 Python 慢响应撑爆内存）
- **`Configure` 是全量重置**：Python 端不需要维护增量协议，每次推送完整列表，Go 端做 diff
- **REMOVE / CHMOD 事件不上报**：Python 端目前不消费

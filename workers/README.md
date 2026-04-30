# MoviePilot Workers

外置高性能子进程集合，用 Go 实现。每个 worker 是独立的二进制，通过 Unix Domain Socket + HTTP/JSON 与 Python 主进程通信。

## 设计目标

- **降低 NAS 资源占用**：把吃 CPU/内存/IO 的"肌肉活"剥离 Python 主进程
- **零侵入**：Python 端通过 `WORKER_MODE` 配置切换，默认 `python` 模式与改造前完全一致
- **崩溃可降级**：worker 不可用时主进程自动 fallback 到 Python 实现

## 现有 worker

| 名称 | 职责 | 状态 |
|---|---|---|
| `mp-watcher`  | 本地目录文件监控（fsnotify） | P1-A 已交付 |
| `mp-transfer` | 物理 IO 加速（copy/move/link/softlink，Linux 零拷贝） | P1-C 已交付 |
| `mp-indexer`  | HTTP 代理加速器（并发站点索引请求） | P3 已交付 |

## 目录结构

```
workers/
├── go.work               # Go workspace 多模块声明
├── shared/               # 跨 worker 共享库（transport / log / lifecycle / config）
├── mp-watcher/           # P1-A
├── mp-transfer/          # P1-C
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
make build            # 构建当前平台所有 worker，产物在 workers/bin/
make build-watcher    # 仅构建 mp-watcher
make build-transfer   # 仅构建 mp-transfer
make build-indexer    # 仅构建 mp-indexer
make release          # 通过 goreleaser 多平台构建，产物在 workers/dist/
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

---

## mp-transfer

物理 IO 加速器，接管 `app/utils/system.py` 中 `SystemUtils.copy / move / link / softlink` 4 个静态方法。
Linux 下使用 `copy_file_range` 走内核侧零拷贝；其他平台 fallback `io.Copy`。
**只做 local→local 的物理操作**——识别、命名、刮削、远端存储、历史记录、伴生文件配对全留 Python。

### 启动参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `--socket` | ✅ | UDS 文件路径，例：`/config/sockets/mp-transfer.sock` |
| `--callback-url` | ❌ | **不使用**（纯请求-响应模式，无主动推送） |
| `--log-level` | ❌ | `debug` / `info`（默认）/ `warn` / `error` |
| `--log-format` | ❌ | `json`（默认）/ `text` |

### 业务接口

#### `POST /api/v1/transfer`

执行一次物理 IO 操作。

请求：
```json
{"mode": "copy", "src": "/data/src/foo.mkv", "dst": "/data/dst/foo.mkv"}
```

`mode` 取值：`copy` / `move` / `link`（硬链接）/ `softlink`（软链接）。
`src` 与 `dst` **必须是绝对路径**（worker 进程 cwd 不一定与 Python 一致）。

响应：
```json
{"code": 0, "message": "ok",
 "data": {"mode": "copy", "bytes": 1234567, "duration_ms": 42}}
```

错误码分流（用于 Python 侧分类降级）：
- `1000` 参数错误（mode 非法 / 路径相对 / 路径相同）—— Python 不应 fallback
- `3000` IO 错误（源不存在 / 目标无权限 / 跨设备失败等）—— Python 自动 fallback shutil

#### `GET /api/v1/transfer_stats`

返回累计计数器，便于运维巡检。

响应：
```json
{"code": 0, "data": {
  "total": 1234, "failed": 5, "bytes_total": 9876543210,
  "by_mode": {"copy": 100, "move": 50, "link": 1000, "softlink": 84}
}}
```

### 设计要点

- **行为与 Python 端 100% 对齐**：单元测试覆盖到 inode 共享、空文件、跨设备等边界，
  worker 调用失败 fallback 到 `shutil` 时业务结果一致
- **零拷贝退化策略**：`copy_file_range` 遇到 `EXDEV / ENOSYS / EINVAL / EOPNOTSUPP`
  自动降级为 `io.Copy`，保证任何 Linux 环境（NFS / 旧内核 / tmpfs）都能成功
- **硬链接 tmp + rename**：与原 `SystemUtils.link` 一致使用 `dst.mp` 中间名，
  避免目录监控感知到半成品文件
- **不创建父目录**：与原 `SystemUtils` 行为一致，由调用方（`TransHandler`）保证 `dst` 父目录存在
- **不抛异常给调用方**：`SystemUtils` 4 个方法的对外契约（返回 `(int, str)`）保持不变，
  worker 错误一律收敛到 fallback 分支

---

## mp-indexer

HTTP 代理加速器，接管 `IndexerModule.__spider_search()` 中通用 SiteSpider 的 HTTP 请求。
Go 端用 goroutine 并发执行多个 HTTP 请求（连接池 + TLS 会话复用），把 HTML 原文返回给 Python 解析。
**只做 HTTP 请求代理**——URL 拼装、HTML 解析、TorrentInfo 构造全留 Python。

### 启动参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `--socket` | ✅ | UDS 文件路径，例：`/config/sockets/mp-indexer.sock` |
| `--callback-url` | ❌ | **不使用**（纯请求-响应模式，无主动推送） |
| `--log-level` | ❌ | `debug` / `info`（默认）/ `warn` / `error` |
| `--log-format` | ❌ | `json`（默认）/ `text` |

### 业务接口

#### `POST /api/v1/fetch`

批量执行 HTTP 请求。Python 端将多个站点的搜索 URL + Cookie/UA/Proxy 打包发送，
Go 端 goroutine 并发请求，所有请求完成后一次性返回结果。

请求：
```json
{
  "requests": [
    {
      "id": "site-123",
      "url": "https://example.com/torrents.php?search=test",
      "method": "GET",
      "headers": {"User-Agent": "...", "Cookie": "...", "Referer": "..."},
      "proxy": "http://proxy:port",
      "timeout_ms": 15000,
      "allow_redirects": true
    }
  ]
}
```

响应：
```json
{
  "code": 0, "message": "ok",
  "data": {
    "results": [
      {
        "id": "site-123",
        "status_code": 200,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": "<html>...</html>",
        "error": "",
        "duration_ms": 1234
      }
    ],
    "total_duration_ms": 3456
  }
}
```

字段说明：
- `id`：请求标识，用于关联请求和响应
- `method`：HTTP 方法（默认 `GET`）
- `proxy`：代理地址（空字符串表示不使用代理）
- `timeout_ms`：单请求超时毫秒（默认 15000）
- `allow_redirects`：是否跟随重定向（默认 `true`）
- `error`：非空表示该请求失败，Python 端应 fallback

#### `GET /api/v1/fetch_stats`

返回累计请求统计。

响应：
```json
{"code": 0, "data": {
  "total_requests": 1234, "failed_requests": 5, "bytes_total": 9876543,
  "by_status": {"200": 1200, "403": 20, "0": 5}
}}
```

### 设计要点

- **方案 A（HTTP 代理加速器）**：Go 端只做并发 HTTP 请求调度 + 连接池管理，
  不做任何业务逻辑（不解析 HTML、不构造搜索 URL、不处理分类映射）
- **仅处理通用 SiteSpider**：特殊 Spider（TNode/TorrentLeech/MTorrent/Yema/Haidan/HDDolby/Rousi）
  有各自独立的 HTTP 请求逻辑，仍走 Python 原有路径
- **逐站点调用**：当前实现在 `IndexerModule.__spider_search()` 层面拦截，
  每次调用发一个请求给 worker。虽然没有批量并发的优势，但释放了 Python GIL，
  Go HTTP client 的连接池和 TLS 复用仍有收益
- **Fallback 透明**：worker 不可用或请求失败时，自动退回到 Python 端 `RequestUtils.get_res()`，
  对 `SearchChain` 和上层调用方完全透明
- **TLS 证书验证跳过**：与 Python 端 `RequestUtils(verify=False)` 行为一致，
  私有站点自签证书不影响请求

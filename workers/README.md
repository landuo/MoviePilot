# MoviePilot Workers

外置高性能子进程集合，用 Go 实现。每个 worker 是独立的二进制，通过 Unix Domain Socket + HTTP/JSON 与 Python 主进程通信。

## 设计目标

- **降低 NAS 资源占用**：把吃 CPU/内存/IO 的"肌肉活"剥离 Python 主进程
- **零侵入**：Python 端通过 `WORKER_MODE` 配置切换，默认 `python` 模式与改造前完全一致
- **崩溃可降级**：worker 不可用时主进程自动 fallback 到 Python 实现

## 现有 worker

| 名称 | 职责 | 状态 |
|---|---|---|
| `mp-watcher` | 本地目录文件监控 | P1（开发中） |
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

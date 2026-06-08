APP_VERSION = 'v2.13.6'
FRONTEND_VERSION = 'v2.13.6'

# Worker 二进制版本，对应 release tag（默认仓库 landuo/MoviePilot，
# 可通过 docker build --build-arg WORKERS_REPO=<owner/repo> 覆盖到任意 fork）
# 留空时跳过下载，Python 端会自动 fallback 到原生实现
WORKERS_VERSION = 'workers/v0.1.0'

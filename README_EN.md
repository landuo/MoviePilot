# MoviePilot

[简体中文](README.md) | English

![GitHub Repo stars](https://img.shields.io/github/stars/jxxghp/MoviePilot?style=for-the-badge)
![GitHub forks](https://img.shields.io/github/forks/jxxghp/MoviePilot?style=for-the-badge)
![GitHub contributors](https://img.shields.io/github/contributors/jxxghp/MoviePilot?style=for-the-badge)
![GitHub repo size](https://img.shields.io/github/repo-size/jxxghp/MoviePilot?style=for-the-badge)
![GitHub issues](https://img.shields.io/github/issues/jxxghp/MoviePilot?style=for-the-badge)
![Docker Pulls](https://img.shields.io/docker/pulls/jxxghp/moviepilot?style=for-the-badge)
![Docker Pulls V2](https://img.shields.io/docker/pulls/jxxghp/moviepilot-v2?style=for-the-badge)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20Synology-blue?style=for-the-badge)

Redesigned from parts of [NAStool](https://github.com/NAStool/nas-tools), with a stronger focus on core automation scenarios while reducing issues and making the project easier to extend and maintain.

# For learning and personal communication only. Please do not promote this project on platforms in mainland China.

Release channel: https://t.me/moviepilot_channel


## Key Features

- Frontend/backend separation based on FastApi + Vue3.
- Focuses on core needs, simplifies features and settings, and allows some options to work well with sensible defaults.
- Reworked user interface for a cleaner and more practical experience.


## Installation

Official wiki: https://wiki.movie-pilot.org

## Go Worker Acceleration (Enabled by Default)

To accelerate high-frequency tasks such as site searching, filesystem watching and file transfers, this project ships with several Go-based worker sub-processes (`mp-watcher` / `mp-transfer` / `mp-indexer`). They communicate with the Python main process via Unix Domain Sockets, significantly reducing GIL contention and improving concurrency.

**Container defaults (since v2)**:

- `WORKER_MODE` defaults to `hybrid`
- `WORKER_ENABLED` defaults to `watcher,transfer,indexer`

The container will start the matching Go processes automatically. If a binary is missing or a worker crashes, the Python side **transparently falls back** to its original implementation — existing users experience zero disruption when upgrading.

### How to Switch Back to Pure Python Mode

To disable all Go workers and force the original Python paths, explicitly set `WORKER_MODE=python`:

**Option 1: `docker run` flag**

```shell
docker run -d \
  -e WORKER_MODE=python \
  ...other flags... \
  jxxghp/moviepilot-v2:latest
```

**Option 2: `docker-compose.yml`**

```yaml
services:
  moviepilot:
    image: jxxghp/moviepilot-v2:latest
    environment:
      - WORKER_MODE=python
```

**Option 3: `${CONFIG_DIR}/app.env` config file**

```env
WORKER_MODE='python'
```

### Enabling Workers Selectively

To enable only a subset of workers, keep `hybrid` mode and list the desired workers in `WORKER_ENABLED`:

```shell
# Search acceleration only
-e WORKER_MODE=hybrid -e WORKER_ENABLED=indexer

# Search + filesystem watcher
-e WORKER_MODE=hybrid -e WORKER_ENABLED=indexer,watcher
```

For detailed worker architecture and development docs see [`workers/README.md`](workers/README.md).

## Local CLI

One-command bootstrap script:

```shell
curl -fsSL https://raw.githubusercontent.com/landuo/MoviePilot/v2/scripts/bootstrap-local.sh | bash
```

Manage MoviePilot with the `moviepilot` command. Full CLI documentation: [`docs/cli.md`](docs/cli.md)


## Add Skills for AI Agents
```shell
npx skills add https://github.com/jxxghp/MoviePilot
```

## Development

API documentation: https://api.movie-pilot.org

MCP tool API documentation: see [docs/mcp-api.md](docs/mcp-api.md)

Development environment setup and local source-run guide: [`docs/development-setup.md`](docs/development-setup.md)

Plugin development guide: <https://wiki.movie-pilot.org/zh/plugindev>

## Related Projects

- [MoviePilot-Frontend](https://github.com/jxxghp/MoviePilot-Frontend)
- [MoviePilot-Resources](https://github.com/jxxghp/MoviePilot-Resources)
- [MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins)
- [MoviePilot-Server](https://github.com/jxxghp/MoviePilot-Server)
- [MoviePilot-Wiki](https://github.com/jxxghp/MoviePilot-Wiki)

## Disclaimer

- This software is for learning and personal communication only. It must not be used for commercial purposes or illegal activities. The software does not know how users choose to use it, and all responsibility rests with the user.
- The source code is open source and derived from other open-source code. If someone removes the relevant restrictions and redistributes or publishes modified versions that lead to liability events, the publisher of those modifications bears full responsibility. Public releases that bypass or alter the user authentication mechanism are not recommended.
- This project does not accept donations and has not published any donation page anywhere. The software itself is free of charge and does not provide paid services. Please verify information carefully to avoid being misled.

## Contributors

<a href="https://github.com/landuo/MoviePilot/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=jxxghp/MoviePilot" />
</a>

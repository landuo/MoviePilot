# MoviePilot Workers 顶层构建入口
#
# 用法：
#   make build                      构建本机平台所有 worker
#   make build-watcher              仅构建 mp-watcher（本机平台）
#   make build-linux                交叉编译所有 worker 的 linux/amd64 + linux/arm64 版本
#   make build-linux-amd64          仅构建所有 worker 的 linux/amd64 版本（NAS x86 部署）
#   make build-linux-arm64          仅构建所有 worker 的 linux/arm64 版本（NAS ARM 部署）
#   make clean                      清理构建产物
#   make release                    通过 goreleaser 多平台构建（需要安装 goreleaser）

WORKERS := mp-watcher mp-transfer mp-indexer
BIN_DIR := bin
GO_BUILD_FLAGS := -trimpath -ldflags "-s -w"

.PHONY: build clean release \
        $(addprefix build-,$(WORKERS)) \
        build-linux build-linux-amd64 build-linux-arm64

build: $(addprefix build-,$(WORKERS))

build-watcher:
	@echo "→ Building mp-watcher..."
	@mkdir -p $(BIN_DIR)
	cd mp-watcher && go build $(GO_BUILD_FLAGS) -o ../$(BIN_DIR)/mp-watcher .

build-transfer:
	@echo "→ Building mp-transfer..."
	@mkdir -p $(BIN_DIR)
	cd mp-transfer && go build $(GO_BUILD_FLAGS) -o ../$(BIN_DIR)/mp-transfer .

build-indexer:
	@echo "→ Building mp-indexer..."
	@mkdir -p $(BIN_DIR)
	cd mp-indexer && go build $(GO_BUILD_FLAGS) -o ../$(BIN_DIR)/mp-indexer .

# ---------- 交叉编译（NAS / Docker 部署用） ----------
# 产物命名：bin/<worker>-linux-<arch>，与 Dockerfile 中 GitHub Release 命名约定一致。
# Dockerfile 当前期望本地编译产物在 bin/<worker>（裸名），如需直接喂给本机 Docker，
# 可手动 cp bin/mp-indexer-linux-amd64 bin/mp-indexer 后再 docker build。

build-linux: build-linux-amd64 build-linux-arm64

build-linux-amd64:
	@echo "→ Cross-compiling all workers for linux/amd64..."
	@mkdir -p $(BIN_DIR)
	@for w in $(WORKERS); do \
		echo "  - $$w-linux-amd64"; \
		(cd $$w && GOOS=linux GOARCH=amd64 CGO_ENABLED=0 \
			go build $(GO_BUILD_FLAGS) -o ../$(BIN_DIR)/$$w-linux-amd64 .) || exit 1; \
	done

build-linux-arm64:
	@echo "→ Cross-compiling all workers for linux/arm64..."
	@mkdir -p $(BIN_DIR)
	@for w in $(WORKERS); do \
		echo "  - $$w-linux-arm64"; \
		(cd $$w && GOOS=linux GOARCH=arm64 CGO_ENABLED=0 \
			go build $(GO_BUILD_FLAGS) -o ../$(BIN_DIR)/$$w-linux-arm64 .) || exit 1; \
	done

clean:
	@echo "→ Cleaning build outputs..."
	rm -rf $(BIN_DIR) dist

release:
	@command -v goreleaser >/dev/null 2>&1 || { \
		echo "goreleaser 未安装，参考 https://goreleaser.com/install/"; exit 1; \
	}
	goreleaser release --snapshot --clean

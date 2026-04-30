# MoviePilot Workers 顶层构建入口
#
# 用法：
#   make build           构建本机平台所有 worker
#   make build-watcher   仅构建 mp-watcher
#   make clean           清理构建产物
#   make release         通过 goreleaser 多平台构建（需要安装 goreleaser）

WORKERS := mp-watcher mp-transfer mp-indexer
BIN_DIR := bin

.PHONY: build clean release $(addprefix build-,$(WORKERS))

build: $(addprefix build-,$(WORKERS))

build-watcher:
	@echo "→ Building mp-watcher..."
	@mkdir -p $(BIN_DIR)
	cd mp-watcher && go build -trimpath -ldflags "-s -w" -o ../$(BIN_DIR)/mp-watcher .

build-transfer:
	@echo "→ Building mp-transfer..."
	@mkdir -p $(BIN_DIR)
	cd mp-transfer && go build -trimpath -ldflags "-s -w" -o ../$(BIN_DIR)/mp-transfer .

build-indexer:
	@echo "→ Building mp-indexer..."
	@mkdir -p $(BIN_DIR)
	cd mp-indexer && go build -trimpath -ldflags "-s -w" -o ../$(BIN_DIR)/mp-indexer .

clean:
	@echo "→ Cleaning build outputs..."
	rm -rf $(BIN_DIR) dist

release:
	@command -v goreleaser >/dev/null 2>&1 || { \
		echo "goreleaser 未安装，参考 https://goreleaser.com/install/"; exit 1; \
	}
	goreleaser release --snapshot --clean

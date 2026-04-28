// Command mp-watcher 是 MoviePilot 的目录监控 worker。
//
// 设计要点参见 workers/README.md 与 workers/mp-watcher/internal/watcher/manager.go。
//
// 启动示例：
//
//	mp-watcher \
//	  --socket=/config/sockets/mp-watcher.sock \
//	  --callback-url=http://127.0.0.1:3001/api/v1/worker_callback/watcher \
//	  --log-format=json
package main

import (
	"context"
	"errors"
	"net/http"
	"os"
	"time"

	"github.com/jxxghp/MoviePilot/workers/mp-watcher/internal/handler"
	"github.com/jxxghp/MoviePilot/workers/mp-watcher/internal/watcher"
	"github.com/jxxghp/MoviePilot/workers/shared/config"
	"github.com/jxxghp/MoviePilot/workers/shared/lifecycle"
	"github.com/jxxghp/MoviePilot/workers/shared/log"
	"github.com/jxxghp/MoviePilot/workers/shared/transport"
)

const (
	workerName    = "mp-watcher"
	workerVersion = "0.1.0"
)

func main() {
	cf := config.MustParseDefault(workerName)
	if err := cf.Validate(); err != nil {
		// 参数错误退出，由 entrypoint 重启或人工介入
		os.Stderr.WriteString("启动参数错误：" + err.Error() + "\n")
		os.Exit(2)
	}

	log.Init(cf.LogLevel, log.Format(cf.LogFormat), workerName)
	logger := log.L()
	logger.Info("启动",
		"version", workerVersion,
		"socket", cf.SocketPath,
		"callback_url", cf.CallbackURL)

	// 回调推送 sink：底层用 shared/transport.CallbackClient
	callback := transport.NewCallbackClient(cf.CallbackURL, 5*time.Second)
	sink := &callbackSink{client: callback}

	// 事件管理器
	mgr := watcher.NewManager(logger, sink)
	if err := mgr.Start(context.Background()); err != nil {
		logger.Error("启动 watcher 失败", "error", err.Error())
		os.Exit(1)
	}

	// HTTP 服务（含通用 /health /info /shutdown）
	server := transport.NewServer(workerName, workerVersion, cf.SocketPath)
	handler.New(logger, mgr).Register(server)

	// 异步监听
	serveErr := make(chan error, 1)
	go func() {
		if err := server.ListenAndServe(); err != nil &&
			!errors.Is(err, http.ErrServerClosed) {
			serveErr <- err
			return
		}
		serveErr <- nil
	}()

	// 等待退出：系统信号 / 远程 /shutdown 调用 / 监听异常
	select {
	case err := <-serveErr:
		if err != nil {
			logger.Error("HTTP 监听失败", "error", err.Error())
			_ = mgr.Stop()
			os.Exit(1)
		}
		logger.Info("HTTP 服务自然退出")
	case <-shutdownChan(server):
		logger.Info("收到退出信号，开始优雅停机")
	}

	// 优雅停机：先停 HTTP（不再接新请求）→ 再停 watcher（停止事件推送）
	ctx, cancel := lifecycle.ShutdownContext(10 * time.Second)
	defer cancel()
	if err := server.Shutdown(ctx); err != nil {
		logger.Warn("server.Shutdown 异常", "error", err.Error())
	}
	if err := mgr.Stop(); err != nil {
		logger.Warn("manager.Stop 异常", "error", err.Error())
	}
	logger.Info("退出完成")
}

// shutdownChan 把"系统信号"和"远程 /shutdown"合并成一个事件源，
// 任意一个触发即返回。
func shutdownChan(server *transport.Server) <-chan struct{} {
	ch := make(chan struct{})
	go func() {
		lifecycle.WaitShutdown(server.ShutdownChan())
		close(ch)
	}()
	return ch
}

// callbackSink 把 watcher.EventSink 桥接到 transport.CallbackClient。
type callbackSink struct {
	client *transport.CallbackClient
}

// Push 把事件以 JSON 推送给 Python。当 callback url 未配置时静默丢弃，
// 便于本地调试单跑 worker。
func (s *callbackSink) Push(ctx context.Context, ev watcher.Event) error {
	if s.client == nil {
		return nil
	}
	return s.client.Post(ctx, ev)
}

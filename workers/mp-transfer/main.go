// Command mp-transfer 是 MoviePilot 的物理 IO 加速器 worker。
//
// 设计要点参见 workers/README.md 与 workers/mp-transfer/internal/ops/ops.go。
//
// 启动示例：
//
//	mp-transfer \
//	  --socket=/config/sockets/mp-transfer.sock \
//	  --log-format=json
//
// 与 mp-watcher 不同，本 worker 是纯请求-响应模式（不主动回调 Python），
// 因此 --callback-url 参数留空即可。
package main

import (
	"errors"
	"net/http"
	"os"
	"time"

	"github.com/landuo/MoviePilot/workers/mp-transfer/internal/handler"
	"github.com/landuo/MoviePilot/workers/shared/config"
	"github.com/landuo/MoviePilot/workers/shared/lifecycle"
	"github.com/landuo/MoviePilot/workers/shared/log"
	"github.com/landuo/MoviePilot/workers/shared/transport"
)

const (
	workerName    = "mp-transfer"
	workerVersion = "0.1.0"
)

func main() {
	cf := config.MustParseDefault(workerName)
	if err := cf.Validate(); err != nil {
		os.Stderr.WriteString("启动参数错误：" + err.Error() + "\n")
		os.Exit(2)
	}

	log.Init(cf.LogLevel, log.Format(cf.LogFormat), workerName)
	logger := log.L()
	logger.Info("启动",
		"version", workerVersion,
		"socket", cf.SocketPath)

	server := transport.NewServer(workerName, workerVersion, cf.SocketPath)
	handler.New(logger).Register(server)

	serveErr := make(chan error, 1)
	go func() {
		if err := server.ListenAndServe(); err != nil &&
			!errors.Is(err, http.ErrServerClosed) {
			serveErr <- err
			return
		}
		serveErr <- nil
	}()

	select {
	case err := <-serveErr:
		if err != nil {
			logger.Error("HTTP 监听失败", "error", err.Error())
			os.Exit(1)
		}
		logger.Info("HTTP 服务自然退出")
	case <-shutdownChan(server):
		logger.Info("收到退出信号，开始优雅停机")
	}

	ctx, cancel := lifecycle.ShutdownContext(5 * time.Second)
	defer cancel()
	if err := server.Shutdown(ctx); err != nil {
		logger.Warn("server.Shutdown 异常", "error", err.Error())
	}
	logger.Info("退出完成")
}

// shutdownChan 把"系统信号"和"远程 /shutdown"合并成一个事件源。
func shutdownChan(server *transport.Server) <-chan struct{} {
	ch := make(chan struct{})
	go func() {
		lifecycle.WaitShutdown(server.ShutdownChan())
		close(ch)
	}()
	return ch
}

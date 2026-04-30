package main

import (
	"time"

	"github.com/jxxghp/MoviePilot-Workers/mp-indexer/internal/handler"
	"github.com/jxxghp/MoviePilot-Workers/shared/config"
	"github.com/jxxghp/MoviePilot-Workers/shared/lifecycle"
	"github.com/jxxghp/MoviePilot-Workers/shared/log"
	"github.com/jxxghp/MoviePilot-Workers/shared/transport"
)

const workerName = "mp-indexer"
const workerVersion = "0.1.0"

func main() {
	flags := config.MustParseDefault(workerName)
	log.Init(flags.LogLevel, log.Format(flags.LogFormat), workerName)

	server := transport.NewServer(workerName, workerVersion, flags.SocketPath)
	handler.Register(server)

	go server.ListenAndServe()

	log.L().Info("启动",
		"version", workerVersion,
		"socket", flags.SocketPath,
	)

	lifecycle.WaitShutdown(server.ShutdownChan())
	ctx, cancel := lifecycle.ShutdownContext(5 * time.Second)
	defer cancel()
	server.Shutdown(ctx)
	log.L().Info("退出完成")
}

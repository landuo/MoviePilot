package transport

import (
	"context"
	"errors"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sync/atomic"
	"time"
)

// Server 封装基于 Unix Domain Socket 的 HTTP 服务端。
//
// 提供统一的生命周期管理（启动 / 优雅停机）以及内置的公共接口
// （/api/v1/health、/api/v1/info、/api/v1/shutdown）。
type Server struct {
	socketPath string
	name       string
	version    string
	mux        *http.ServeMux
	httpServer *http.Server

	startTime    time.Time
	shutdownChan chan struct{}
	shutdownOnce atomic.Bool
}

// NewServer 创建一个新的 Server，socketPath 为 UDS 文件路径。
func NewServer(name, version, socketPath string) *Server {
	mux := http.NewServeMux()
	s := &Server{
		socketPath:   socketPath,
		name:         name,
		version:      version,
		mux:          mux,
		startTime:    time.Now(),
		shutdownChan: make(chan struct{}),
	}
	s.registerCommonHandlers()
	return s
}

// Handle 注册业务处理函数，pattern 应使用完整路径如 "/api/v1/configure"。
func (s *Server) Handle(pattern string, handler http.HandlerFunc) {
	s.mux.HandleFunc(pattern, handler)
}

// ShutdownChan 返回一个会在外部 /shutdown 接口被调用时关闭的 channel，
// main 函数可 select 此 channel 实现协调退出。
func (s *Server) ShutdownChan() <-chan struct{} {
	return s.shutdownChan
}

// ListenAndServe 创建 socket 文件并阻塞监听。
// 调用方负责处理返回的错误（http.ErrServerClosed 视为正常退出）。
func (s *Server) ListenAndServe() error {
	if err := s.prepareSocketPath(); err != nil {
		return err
	}

	listener, err := net.Listen("unix", s.socketPath)
	if err != nil {
		return err
	}
	// UDS 文件权限：owner + group 可读写，避免 Python 主进程因 umask 不一致访问失败
	_ = os.Chmod(s.socketPath, 0o660)

	s.httpServer = &http.Server{
		Handler:           s.mux,
		ReadHeaderTimeout: 10 * time.Second,
	}
	return s.httpServer.Serve(listener)
}

// Shutdown 优雅停止 HTTP 服务，并清理 socket 文件。
func (s *Server) Shutdown(ctx context.Context) error {
	if s.httpServer == nil {
		_ = os.Remove(s.socketPath)
		return nil
	}
	err := s.httpServer.Shutdown(ctx)
	_ = os.Remove(s.socketPath)
	return err
}

// prepareSocketPath 确保 socket 所在目录存在，且没有残留文件。
func (s *Server) prepareSocketPath() error {
	dir := filepath.Dir(s.socketPath)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	if _, err := os.Stat(s.socketPath); err == nil {
		// 残留文件可能是上次崩溃留下的，直接清理
		if rmErr := os.Remove(s.socketPath); rmErr != nil {
			return rmErr
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

// registerCommonHandlers 注册所有 worker 必备的公共接口。
func (s *Server) registerCommonHandlers() {
	s.mux.HandleFunc("/api/v1/health", func(w http.ResponseWriter, r *http.Request) {
		WriteOK(w, map[string]interface{}{
			"status":     "ok",
			"uptime_sec": int(time.Since(s.startTime).Seconds()),
			"version":    s.version,
		})
	})

	s.mux.HandleFunc("/api/v1/info", func(w http.ResponseWriter, r *http.Request) {
		WriteOK(w, map[string]interface{}{
			"name":    s.name,
			"version": s.version,
		})
	})

	s.mux.HandleFunc("/api/v1/shutdown", func(w http.ResponseWriter, r *http.Request) {
		WriteOK(w, map[string]string{"status": "shutting_down"})
		// 仅触发一次，避免重复 close
		if s.shutdownOnce.CompareAndSwap(false, true) {
			close(s.shutdownChan)
		}
	})
}

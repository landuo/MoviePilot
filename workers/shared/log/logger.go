// Package log 提供 worker 通用的结构化日志封装。
//
// 输出走 stdout，由父进程（Python）的日志系统统一接管。
// 默认 JSON 格式便于解析；可通过 SetFormat 切换 text 格式便于本地调试。
package log

import (
	"log/slog"
	"os"
	"strings"
)

// Format 表示日志输出格式。
type Format string

const (
	FormatJSON Format = "json"
	FormatText Format = "text"
)

var defaultLogger = newLogger("info", FormatJSON, "worker")

// Init 初始化默认 logger。在 main 启动早期调用一次。
//
// level: debug / info / warn / error
// format: json / text
// name: worker 名称，作为顶层 logger 的固定字段
func Init(level string, format Format, name string) {
	defaultLogger = newLogger(level, format, name)
}

// L 获取默认 logger。
func L() *slog.Logger {
	return defaultLogger
}

// With 派生带额外字段的子 logger。
func With(args ...any) *slog.Logger {
	return defaultLogger.With(args...)
}

func newLogger(level string, format Format, name string) *slog.Logger {
	opts := &slog.HandlerOptions{Level: parseLevel(level)}

	var handler slog.Handler
	switch format {
	case FormatText:
		handler = slog.NewTextHandler(os.Stdout, opts)
	default:
		handler = slog.NewJSONHandler(os.Stdout, opts)
	}
	return slog.New(handler).With("worker", name)
}

func parseLevel(s string) slog.Level {
	switch strings.ToLower(strings.TrimSpace(s)) {
	case "debug":
		return slog.LevelDebug
	case "warn", "warning":
		return slog.LevelWarn
	case "error":
		return slog.LevelError
	default:
		return slog.LevelInfo
	}
}

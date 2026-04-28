// Package config 提供 worker 通用的命令行参数解析。
package config

import (
	"flag"
	"fmt"
	"os"
)

// CommonFlags 是所有 worker 共有的启动参数。
type CommonFlags struct {
	SocketPath  string
	CallbackURL string
	LogLevel    string
	LogFormat   string
}

// NewFlagSet 创建一个独立的 flag.FlagSet 并注册公共参数。
//
// 业务 worker 可继续在返回的 FlagSet 上注册自己的额外参数，
// 然后调用 Parse(fs) 完成解析。这样避免了 flag 包全局状态带来的
// "调用顺序敏感" 问题。
//
// 用法：
//
//	cf, fs := config.NewFlagSet("mp-watcher")
//	myFlag := fs.String("extra", "", "...")
//	if err := config.Parse(fs, os.Args[1:]); err != nil { ... }
func NewFlagSet(workerName string) (*CommonFlags, *flag.FlagSet) {
	fs := flag.NewFlagSet(workerName, flag.ContinueOnError)
	cf := &CommonFlags{}

	fs.StringVar(&cf.SocketPath, "socket", "",
		"Unix Domain Socket 路径，例：/config/sockets/mp-watcher.sock")
	fs.StringVar(&cf.CallbackURL, "callback-url", "",
		"事件回调到 Python 主进程的 URL")
	fs.StringVar(&cf.LogLevel, "log-level", "info",
		"日志级别：debug / info / warn / error")
	fs.StringVar(&cf.LogFormat, "log-format", "json",
		"日志格式：json / text")

	fs.Usage = func() {
		fmt.Fprintf(fs.Output(), "%s - MoviePilot worker\n\n用法：\n", workerName)
		fs.PrintDefaults()
	}
	return cf, fs
}

// Parse 用给定参数解析指定 FlagSet，便于业务 worker 自定义入参。
//
// 通常业务 worker 直接传 os.Args[1:] 即可。
func Parse(fs *flag.FlagSet, args []string) error {
	return fs.Parse(args)
}

// MustParseDefault 是给只需要公共参数、没有额外 flag 的 worker 提供的便捷入口。
//
// 内部使用 os.Args[1:]，解析失败时打印 usage 并退出。
func MustParseDefault(workerName string) *CommonFlags {
	cf, fs := NewFlagSet(workerName)
	if err := fs.Parse(os.Args[1:]); err != nil {
		// flag.ContinueOnError 模式下 fs.Parse 已经打印了错误，这里直接退出
		os.Exit(2)
	}
	return cf
}

// Validate 校验必填参数。
func (cf *CommonFlags) Validate() error {
	if cf.SocketPath == "" {
		return fmt.Errorf("--socket 不能为空")
	}
	return nil
}

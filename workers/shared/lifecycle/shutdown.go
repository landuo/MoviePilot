// Package lifecycle 提供 worker 通用的生命周期辅助函数。
package lifecycle

import (
	"context"
	"os"
	"os/signal"
	"reflect"
	"syscall"
	"time"
)

// WaitShutdown 阻塞等待，任意一个事件触发即返回：
//   - 进程收到 SIGINT 或 SIGTERM
//   - extraSignals 中任意一个 channel 被关闭或收到值
//
// 返回时调用方应立即开始优雅停机流程。
//
// 用法：
//
//	lifecycle.WaitShutdown(server.ShutdownChan())
func WaitShutdown(extraSignals ...<-chan struct{}) {
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)
	defer signal.Stop(sigChan)

	if len(extraSignals) == 0 {
		<-sigChan
		return
	}

	// 用 reflect.Select 支持任意数量的 extraSignals
	cases := make([]reflect.SelectCase, 0, len(extraSignals)+1)
	cases = append(cases, reflect.SelectCase{
		Dir:  reflect.SelectRecv,
		Chan: reflect.ValueOf(sigChan),
	})
	for _, ch := range extraSignals {
		if ch == nil {
			continue
		}
		cases = append(cases, reflect.SelectCase{
			Dir:  reflect.SelectRecv,
			Chan: reflect.ValueOf(ch),
		})
	}
	reflect.Select(cases)
}

// ShutdownContext 创建一个带超时的 context，用于优雅停机的 deadline 控制。
func ShutdownContext(timeout time.Duration) (context.Context, context.CancelFunc) {
	return context.WithTimeout(context.Background(), timeout)
}

// Package watcher 实现基于 fsnotify 的多目录文件监控。
package watcher

import (
	"strconv"
	"sync/atomic"
	"time"
)

// EventType 与 Python 端 schemas/worker.py:WatcherFileEvent.event_type 对齐。
type EventType string

const (
	EventCreated  EventType = "created"
	EventModified EventType = "modified"
	EventMoved    EventType = "moved"
)

// Event 是 mp-watcher 推送给 Python 的事件载荷。
//
// 字段命名必须与 app/schemas/worker.py 中 WatcherFileEvent 严格一致，
// 修改前请同步检查 Python 端。
type Event struct {
	EventID     string    `json:"event_id"`
	WatchID     string    `json:"watch_id"`
	EventType   EventType `json:"event_type"`
	Storage     string    `json:"storage"`
	SrcPath     string    `json:"src_path"`
	DestPath    string    `json:"dest_path"`
	FileSize    int64     `json:"file_size"`
	MtimeUnix   int64     `json:"mtime_unix"`
	IsDirectory bool      `json:"is_directory"`
}

// 事件 ID 使用进程内单调递增计数器 + 启动时间戳，避免重启后 ID 复用。
var eventCounter uint64

var startTime = time.Now().UnixNano()

func nextEventID() string {
	n := atomic.AddUint64(&eventCounter, 1)
	return strconv.FormatInt(startTime, 36) + "-" + strconv.FormatUint(n, 36)
}

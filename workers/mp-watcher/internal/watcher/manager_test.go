package watcher

import (
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"runtime"
	"sync"
	"testing"
	"time"
)

// ----- 公共测试辅助 -----

// fakeSink 把推送的事件按顺序记录下来，并支持等待事件到达。
type fakeSink struct {
	mu     sync.Mutex
	events []Event
	ch     chan Event
}

func newFakeSink(buf int) *fakeSink {
	return &fakeSink{ch: make(chan Event, buf)}
}

func (s *fakeSink) Push(_ context.Context, ev Event) error {
	s.mu.Lock()
	s.events = append(s.events, ev)
	s.mu.Unlock()
	select {
	case s.ch <- ev:
	default:
		// channel 满时不阻塞，避免单个测试卡死
	}
	return nil
}

func (s *fakeSink) snapshot() []Event {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Event, len(s.events))
	copy(out, s.events)
	return out
}

// waitFor 等待 timeout 内出现满足 pred 的事件，返回事件副本。
func (s *fakeSink) waitFor(t *testing.T, timeout time.Duration,
	pred func(Event) bool) Event {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for {
		select {
		case ev := <-s.ch:
			if pred(ev) {
				return ev
			}
		case <-time.After(time.Until(deadline)):
			t.Fatalf("等待事件超时，已收事件: %+v", s.snapshot())
			return Event{}
		}
		if time.Now().After(deadline) {
			t.Fatalf("等待事件超时，已收事件: %+v", s.snapshot())
			return Event{}
		}
	}
}

func newTestLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, &slog.HandlerOptions{
		Level: slog.LevelError,
	}))
}

func newStartedManager(t *testing.T, sink EventSink) *Manager {
	t.Helper()
	mgr := NewManager(newTestLogger(), sink)
	if err := mgr.Start(context.Background()); err != nil {
		t.Fatalf("Start 失败: %v", err)
	}
	t.Cleanup(func() { _ = mgr.Stop() })
	return mgr
}

// ----- Configure 单元测试（不依赖真实事件） -----

func TestConfigure_AddNewWatch(t *testing.T) {
	mgr := newStartedManager(t, newFakeSink(0))
	dir := t.TempDir()

	added, removed, err := mgr.Configure([]WatchSpec{
		{WatchID: "w1", Path: dir, Recursive: false},
	})
	if err != nil {
		t.Fatalf("Configure: %v", err)
	}
	if added != 1 || removed != 0 {
		t.Fatalf("added=%d removed=%d, want 1/0", added, removed)
	}
	if got := mgr.Status(); len(got) != 1 || got[0].WatchID != "w1" {
		t.Fatalf("Status = %+v", got)
	}
}

func TestConfigure_RemoveMissing(t *testing.T) {
	mgr := newStartedManager(t, newFakeSink(0))
	d1, d2 := t.TempDir(), t.TempDir()

	_, _, err := mgr.Configure([]WatchSpec{
		{WatchID: "w1", Path: d1},
		{WatchID: "w2", Path: d2},
	})
	if err != nil {
		t.Fatalf("初始 Configure: %v", err)
	}
	added, removed, err := mgr.Configure([]WatchSpec{
		{WatchID: "w1", Path: d1},
	})
	if err != nil {
		t.Fatalf("第二次 Configure: %v", err)
	}
	if added != 0 || removed != 1 {
		t.Fatalf("added=%d removed=%d, want 0/1", added, removed)
	}
	statuses := mgr.Status()
	if len(statuses) != 1 || statuses[0].WatchID != "w1" {
		t.Fatalf("Status 应只剩 w1，实际: %+v", statuses)
	}
}

func TestConfigure_NoOpWhenIdentical(t *testing.T) {
	mgr := newStartedManager(t, newFakeSink(0))
	dir := t.TempDir()

	specs := []WatchSpec{{WatchID: "w1", Path: dir, Recursive: true}}
	if _, _, err := mgr.Configure(specs); err != nil {
		t.Fatalf("初始: %v", err)
	}
	added, removed, err := mgr.Configure(specs)
	if err != nil {
		t.Fatalf("重复: %v", err)
	}
	if added != 0 || removed != 0 {
		t.Fatalf("相同配置应 0/0，实际 %d/%d", added, removed)
	}
}

func TestConfigure_PathChangeIsRemoveAdd(t *testing.T) {
	mgr := newStartedManager(t, newFakeSink(0))
	d1, d2 := t.TempDir(), t.TempDir()

	if _, _, err := mgr.Configure([]WatchSpec{
		{WatchID: "w1", Path: d1},
	}); err != nil {
		t.Fatalf("初始: %v", err)
	}
	added, removed, err := mgr.Configure([]WatchSpec{
		{WatchID: "w1", Path: d2},
	})
	if err != nil {
		t.Fatalf("改路径: %v", err)
	}
	if added != 1 || removed != 1 {
		t.Fatalf("改路径应 1/1，实际 %d/%d", added, removed)
	}
	if got := mgr.Status(); len(got) != 1 || got[0].Path != mustAbs(t, d2) {
		t.Fatalf("Status path 未更新: %+v", got)
	}
}

func TestConfigure_RejectInvalidSpec(t *testing.T) {
	mgr := newStartedManager(t, newFakeSink(0))

	_, _, err := mgr.Configure([]WatchSpec{{WatchID: "", Path: "/tmp"}})
	if err == nil {
		t.Fatal("空 watch_id 应报错")
	}
	_, _, err = mgr.Configure([]WatchSpec{{WatchID: "w1", Path: ""}})
	if err == nil {
		t.Fatal("空 path 应报错")
	}
}

func TestConfigure_AddNonExistingPath_ReturnsError(t *testing.T) {
	mgr := newStartedManager(t, newFakeSink(0))
	added, removed, err := mgr.Configure([]WatchSpec{
		{WatchID: "w1", Path: "/this/path/should/not/exist/abc123"},
	})
	if err == nil {
		t.Fatal("不存在的路径应返回错误")
	}
	if added != 0 || removed != 0 {
		t.Fatalf("失败时不应计入 added/removed: %d/%d", added, removed)
	}
}

func TestConfigure_AddFileNotDir_ReturnsError(t *testing.T) {
	mgr := newStartedManager(t, newFakeSink(0))
	dir := t.TempDir()
	file := filepath.Join(dir, "a.txt")
	if err := os.WriteFile(file, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, _, err := mgr.Configure([]WatchSpec{{WatchID: "w1", Path: file}})
	if err == nil {
		t.Fatal("传入文件路径应返回错误")
	}
}

func TestConfigure_BeforeStart_ReturnsError(t *testing.T) {
	mgr := NewManager(newTestLogger(), newFakeSink(0))
	defer mgr.Stop()
	_, _, err := mgr.Configure([]WatchSpec{{WatchID: "w1", Path: "/tmp"}})
	if err == nil {
		t.Fatal("未 Start 时 Configure 应返回错误")
	}
}

// ----- Status / Stop -----

func TestStatus_EmptyWhenNoWatches(t *testing.T) {
	mgr := newStartedManager(t, newFakeSink(0))
	if got := mgr.Status(); len(got) != 0 {
		t.Fatalf("初始 Status 应为空: %+v", got)
	}
}

func TestStatus_SortedByWatchID(t *testing.T) {
	mgr := newStartedManager(t, newFakeSink(0))
	d1, d2, d3 := t.TempDir(), t.TempDir(), t.TempDir()
	_, _, err := mgr.Configure([]WatchSpec{
		{WatchID: "wc", Path: d1},
		{WatchID: "wa", Path: d2},
		{WatchID: "wb", Path: d3},
	})
	if err != nil {
		t.Fatal(err)
	}
	got := mgr.Status()
	if len(got) != 3 {
		t.Fatalf("len = %d", len(got))
	}
	if got[0].WatchID != "wa" || got[1].WatchID != "wb" || got[2].WatchID != "wc" {
		t.Fatalf("未按 watch_id 排序: %+v", got)
	}
}

func TestStop_Idempotent(t *testing.T) {
	mgr := newStartedManager(t, newFakeSink(0))
	if err := mgr.Stop(); err != nil {
		t.Fatalf("第一次 Stop: %v", err)
	}
	if err := mgr.Stop(); err != nil {
		t.Fatalf("第二次 Stop: %v", err)
	}
}

func TestStart_DoubleStart_ReturnsError(t *testing.T) {
	mgr := NewManager(newTestLogger(), newFakeSink(0))
	defer mgr.Stop()
	if err := mgr.Start(context.Background()); err != nil {
		t.Fatalf("第一次 Start: %v", err)
	}
	if err := mgr.Start(context.Background()); err == nil {
		t.Fatal("重复 Start 应返回错误")
	}
}

// ----- 集成测试：真实 fsnotify -----
//
// fsnotify 的事件递达受 OS 影响：
//   - macOS/Linux 上文件创建会先 CREATE 再可能 WRITE
//   - 事件可能合并、重复，因此测试用 waitFor 等待"任意一个匹配事件"，
//     不严格断言事件序列

const waitTimeout = 3 * time.Second

func TestIntegration_CreateFile_EmitsCreated(t *testing.T) {
	if testing.Short() {
		t.Skip("跳过 fsnotify 集成测试")
	}
	sink := newFakeSink(64)
	mgr := newStartedManager(t, sink)
	dir := t.TempDir()
	if _, _, err := mgr.Configure([]WatchSpec{
		{WatchID: "w1", Path: dir, Recursive: false},
	}); err != nil {
		t.Fatal(err)
	}
	// 给 watcher 一点时间真正订阅
	time.Sleep(100 * time.Millisecond)

	target := filepath.Join(dir, "hello.txt")
	if err := os.WriteFile(target, []byte("hi"), 0o644); err != nil {
		t.Fatal(err)
	}

	ev := sink.waitFor(t, waitTimeout, func(e Event) bool {
		return e.SrcPath == target &&
			(e.EventType == EventCreated || e.EventType == EventModified)
	})
	if ev.WatchID != "w1" {
		t.Fatalf("watch_id = %q, want w1", ev.WatchID)
	}
	if ev.Storage != "local" {
		t.Fatalf("storage = %q, want local", ev.Storage)
	}
	if ev.EventID == "" {
		t.Fatal("event_id 为空")
	}
}

func TestIntegration_RecursiveWatch_PicksUpSubdir(t *testing.T) {
	if testing.Short() {
		t.Skip("跳过 fsnotify 集成测试")
	}
	if runtime.GOOS == "windows" {
		t.Skip("Windows 上 fsnotify 行为差异较大，跳过")
	}
	sink := newFakeSink(64)
	mgr := newStartedManager(t, sink)
	root := t.TempDir()
	if _, _, err := mgr.Configure([]WatchSpec{
		{WatchID: "w1", Path: root, Recursive: true},
	}); err != nil {
		t.Fatal(err)
	}
	time.Sleep(100 * time.Millisecond)

	// 1. 新建子目录（应触发 CREATED + 自动加入 watch）
	subDir := filepath.Join(root, "sub")
	if err := os.Mkdir(subDir, 0o755); err != nil {
		t.Fatal(err)
	}
	sink.waitFor(t, waitTimeout, func(e Event) bool {
		return e.SrcPath == subDir && e.IsDirectory
	})

	// 2. 给动态 watch 一点时间生效
	time.Sleep(200 * time.Millisecond)

	// 3. 在新子目录下创建文件，应能被监听到
	target := filepath.Join(subDir, "deep.txt")
	if err := os.WriteFile(target, []byte("y"), 0o644); err != nil {
		t.Fatal(err)
	}
	sink.waitFor(t, waitTimeout, func(e Event) bool {
		return e.SrcPath == target
	})
}

func TestIntegration_NonRecursiveIgnoresSubdir(t *testing.T) {
	if testing.Short() {
		t.Skip("跳过 fsnotify 集成测试")
	}
	sink := newFakeSink(64)
	mgr := newStartedManager(t, sink)
	root := t.TempDir()

	// 预创建子目录
	subDir := filepath.Join(root, "child")
	if err := os.Mkdir(subDir, 0o755); err != nil {
		t.Fatal(err)
	}

	if _, _, err := mgr.Configure([]WatchSpec{
		{WatchID: "w1", Path: root, Recursive: false},
	}); err != nil {
		t.Fatal(err)
	}
	time.Sleep(100 * time.Millisecond)

	// 子目录里新建文件——非递归 watch 不应收到事件
	target := filepath.Join(subDir, "x.txt")
	if err := os.WriteFile(target, []byte("z"), 0o644); err != nil {
		t.Fatal(err)
	}

	// 等 1 秒确认没收到（用宽松超时给 OS 时间，事件确实不该来）
	deadline := time.After(1 * time.Second)
	for {
		select {
		case ev := <-sink.ch:
			if ev.SrcPath == target {
				t.Fatalf("非递归 watch 不应收到子目录事件: %+v", ev)
			}
		case <-deadline:
			return
		}
	}
}

func TestIntegration_StatusEventCountIncreases(t *testing.T) {
	if testing.Short() {
		t.Skip("跳过 fsnotify 集成测试")
	}
	sink := newFakeSink(64)
	mgr := newStartedManager(t, sink)
	dir := t.TempDir()
	if _, _, err := mgr.Configure([]WatchSpec{
		{WatchID: "w1", Path: dir},
	}); err != nil {
		t.Fatal(err)
	}
	time.Sleep(100 * time.Millisecond)

	target := filepath.Join(dir, "a.txt")
	if err := os.WriteFile(target, []byte("hi"), 0o644); err != nil {
		t.Fatal(err)
	}
	sink.waitFor(t, waitTimeout, func(e Event) bool {
		return e.SrcPath == target
	})

	// 给原子计数一点点时间增长
	time.Sleep(50 * time.Millisecond)
	statuses := mgr.Status()
	if len(statuses) != 1 {
		t.Fatalf("len = %d", len(statuses))
	}
	if statuses[0].EventCount == 0 {
		t.Fatal("event_count 应大于 0")
	}
}

// ----- 辅助 -----

func mustAbs(t *testing.T, p string) string {
	t.Helper()
	abs, err := filepath.Abs(p)
	if err != nil {
		t.Fatalf("filepath.Abs(%s): %v", p, err)
	}
	return abs
}

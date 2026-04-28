package watcher

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"sync/atomic"
	"time"

	"github.com/fsnotify/fsnotify"
)

// WatchSpec 描述一个待监听的目录。
//
// WatchID 由 Python 端生成，用于回调时关联回原配置；mp-watcher 不解释其含义。
type WatchSpec struct {
	WatchID   string `json:"watch_id"`
	Path      string `json:"path"`
	Recursive bool   `json:"recursive"`
}

// WatchStatus 是 /api/v1/watch_status 的响应载荷里单条记录。
type WatchStatus struct {
	WatchID    string `json:"watch_id"`
	Path       string `json:"path"`
	Recursive  bool   `json:"recursive"`
	DirCount   int    `json:"dir_count"`
	EventCount uint64 `json:"event_count"`
}

// EventSink 是 manager 推送事件的下游抽象，便于测试时替换。
type EventSink interface {
	Push(ctx context.Context, ev Event) error
}

// Manager 管理一组 fsnotify watcher，把原始事件包装成 Event 推送给 sink。
//
// 设计要点：
//   - 不做去抖、不做扩展名过滤——原始事件透传给 Python，由 Python 端复用现有
//     TTLCache 与扩展名过滤逻辑，避免双端规则维护。
//   - Configure 是全量重置：传入新 specs，与当前监听做 diff，关闭多余的、
//     新增缺失的。这样 Python 端不需要维护 add/remove 增量协议。
//   - 单 fsnotify.Watcher 实例承担所有目录（fsnotify 推荐用法），事件路径
//     回查表里反向定位 WatchID。
type Manager struct {
	logger *slog.Logger
	sink   EventSink

	mu         sync.Mutex
	watcher    *fsnotify.Watcher
	watches    map[string]*watchEntry // watch_id -> entry
	dirToWatch map[string]string      // 绝对目录路径 -> watch_id

	// 推送事件的并发控制：避免单个慢 callback 阻塞整个事件循环
	pushSem chan struct{}

	cancel context.CancelFunc
	wg     sync.WaitGroup
}

type watchEntry struct {
	spec       WatchSpec
	dirs       map[string]struct{} // 该 watch 实际加入的子目录集合
	eventCount uint64
}

// NewManager 创建 manager 但不启动；调用 Start 后才真正开始监听事件循环。
func NewManager(logger *slog.Logger, sink EventSink) *Manager {
	return &Manager{
		logger:     logger,
		sink:       sink,
		watches:    make(map[string]*watchEntry),
		dirToWatch: make(map[string]string),
		// 同时最多 8 个 in-flight 回调，避免回调慢导致 fsnotify channel 溢出
		pushSem: make(chan struct{}, 8),
	}
}

// Start 启动 fsnotify watcher 与事件分发协程。
//
// 失败时返回错误，调用方应让进程退出。
func (m *Manager) Start(ctx context.Context) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	if m.watcher != nil {
		return errors.New("manager 已启动")
	}
	w, err := fsnotify.NewWatcher()
	if err != nil {
		return fmt.Errorf("创建 fsnotify watcher 失败：%w", err)
	}
	m.watcher = w

	loopCtx, cancel := context.WithCancel(ctx)
	m.cancel = cancel

	m.wg.Add(1)
	go m.eventLoop(loopCtx)
	return nil
}

// Stop 关闭 watcher 并等待事件循环退出。
func (m *Manager) Stop() error {
	m.mu.Lock()
	w := m.watcher
	cancel := m.cancel
	m.watcher = nil
	m.cancel = nil
	m.watches = make(map[string]*watchEntry)
	m.dirToWatch = make(map[string]string)
	m.mu.Unlock()

	if cancel != nil {
		cancel()
	}
	if w != nil {
		_ = w.Close()
	}
	m.wg.Wait()
	return nil
}

// Configure 用 specs 全量替换当前监听集合。
//
// 行为：
//   - 与当前 watches 做 diff，移除多余、新增缺失
//   - 单个目录添加失败不会中断整体流程，会返回汇总错误
//   - 已存在的 watch_id 若 path 变化，等价于 remove+add
func (m *Manager) Configure(specs []WatchSpec) (added, removed int, err error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	if m.watcher == nil {
		return 0, 0, errors.New("manager 未启动")
	}

	// 用 watch_id 索引新规格
	newByID := make(map[string]WatchSpec, len(specs))
	for _, s := range specs {
		if s.WatchID == "" || s.Path == "" {
			return 0, 0, fmt.Errorf("非法 watch spec: watch_id 和 path 不能为空")
		}
		abs, e := filepath.Abs(s.Path)
		if e != nil {
			return 0, 0, fmt.Errorf("解析路径 %s 失败：%w", s.Path, e)
		}
		s.Path = abs
		newByID[s.WatchID] = s
	}

	var errs []error

	// 1. 移除：旧的里不在新的里、或同 ID 但 spec 变了
	for id, entry := range m.watches {
		newSpec, ok := newByID[id]
		if ok && newSpec.Path == entry.spec.Path && newSpec.Recursive == entry.spec.Recursive {
			continue // 完全一致，保留
		}
		if e := m.removeLocked(id); e != nil {
			errs = append(errs, fmt.Errorf("移除 %s 失败：%w", id, e))
		}
		removed++
	}

	// 2. 新增：新的里不在旧的里
	for id, spec := range newByID {
		if _, ok := m.watches[id]; ok {
			continue
		}
		if e := m.addLocked(spec); e != nil {
			errs = append(errs, fmt.Errorf("新增 %s (%s) 失败：%w", id, spec.Path, e))
			continue
		}
		added++
	}

	if len(errs) > 0 {
		return added, removed, errors.Join(errs...)
	}
	return added, removed, nil
}

// Status 返回当前监听快照（按 watch_id 字典序），调用方可序列化为 JSON 直接返回。
func (m *Manager) Status() []WatchStatus {
	m.mu.Lock()
	defer m.mu.Unlock()

	out := make([]WatchStatus, 0, len(m.watches))
	for id, entry := range m.watches {
		out = append(out, WatchStatus{
			WatchID:    id,
			Path:       entry.spec.Path,
			Recursive:  entry.spec.Recursive,
			DirCount:   len(entry.dirs),
			EventCount: atomic.LoadUint64(&entry.eventCount),
		})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].WatchID < out[j].WatchID })
	return out
}

// addLocked 调用方必须持有 m.mu。
func (m *Manager) addLocked(spec WatchSpec) error {
	info, err := os.Stat(spec.Path)
	if err != nil {
		return err
	}
	if !info.IsDir() {
		return fmt.Errorf("%s 不是目录", spec.Path)
	}

	entry := &watchEntry{
		spec: spec,
		dirs: make(map[string]struct{}),
	}

	// 收集要加入 watcher 的目录列表
	dirs := []string{spec.Path}
	if spec.Recursive {
		err := filepath.Walk(spec.Path, func(p string, info os.FileInfo, walkErr error) error {
			if walkErr != nil {
				// 单个子目录读不了就跳过，整体继续
				m.logger.Warn("walk 子目录失败，跳过",
					"path", p, "error", walkErr.Error())
				return nil
			}
			if info.IsDir() && p != spec.Path {
				dirs = append(dirs, p)
			}
			return nil
		})
		if err != nil {
			return fmt.Errorf("walk %s 失败：%w", spec.Path, err)
		}
	}

	for _, d := range dirs {
		if _, taken := m.dirToWatch[d]; taken {
			// 同一目录已被其他 watch 占用——fsnotify 添加重复路径会去重，
			// 但事件就只能归属一个 watch_id，这里按"先到先得"
			continue
		}
		if err := m.watcher.Add(d); err != nil {
			m.logger.Warn("fsnotify Add 失败，跳过",
				"path", d, "error", err.Error())
			continue
		}
		entry.dirs[d] = struct{}{}
		m.dirToWatch[d] = spec.WatchID
	}

	m.watches[spec.WatchID] = entry
	m.logger.Info("watch 已添加",
		"watch_id", spec.WatchID, "path", spec.Path,
		"recursive", spec.Recursive, "dir_count", len(entry.dirs))
	return nil
}

// removeLocked 调用方必须持有 m.mu。
func (m *Manager) removeLocked(id string) error {
	entry, ok := m.watches[id]
	if !ok {
		return nil
	}
	for d := range entry.dirs {
		if err := m.watcher.Remove(d); err != nil && !errors.Is(err, fsnotify.ErrNonExistentWatch) {
			m.logger.Debug("fsnotify Remove 失败",
				"path", d, "error", err.Error())
		}
		delete(m.dirToWatch, d)
	}
	delete(m.watches, id)
	m.logger.Info("watch 已移除", "watch_id", id, "path", entry.spec.Path)
	return nil
}

// eventLoop 是 fsnotify 事件分发主循环，仅在 Start 协程中执行。
func (m *Manager) eventLoop(ctx context.Context) {
	defer m.wg.Done()

	for {
		// 缓存 watcher 引用，避免每次都加锁
		m.mu.Lock()
		w := m.watcher
		m.mu.Unlock()
		if w == nil {
			return
		}

		select {
		case <-ctx.Done():
			return
		case ev, ok := <-w.Events:
			if !ok {
				return
			}
			m.handleRawEvent(ctx, ev)
		case err, ok := <-w.Errors:
			if !ok {
				return
			}
			if err != nil {
				m.logger.Warn("fsnotify error", "error", err.Error())
			}
		}
	}
}

// handleRawEvent 将一条 fsnotify 原始事件转换成 Event 并异步推送。
func (m *Manager) handleRawEvent(ctx context.Context, raw fsnotify.Event) {
	eventType, ok := mapEventType(raw.Op)
	if !ok {
		return // CHMOD / REMOVE 等暂不上报
	}

	// 反查 watch_id：先精确匹配，再逐级父目录回溯（覆盖递归子目录场景）
	watchID, watchPath := m.lookupWatch(raw.Name)
	if watchID == "" {
		return // 不属于任何注册的 watch（理论上不应发生）
	}

	// 收集元数据；文件可能在事件抵达时已被删除/移走，容错处理
	var (
		size     int64
		mtime    int64
		isDir    bool
	)
	if info, err := os.Stat(raw.Name); err == nil {
		size = info.Size()
		mtime = info.ModTime().Unix()
		isDir = info.IsDir()
	}

	// 递归 watch 时，对新建子目录做动态注册，避免漏听
	if isDir && eventType == EventCreated {
		m.maybeAddSubdir(watchID, raw.Name)
	}

	ev := Event{
		EventID:     nextEventID(),
		WatchID:     watchID,
		EventType:   eventType,
		Storage:     "local",
		SrcPath:     raw.Name,
		FileSize:    size,
		MtimeUnix:   mtime,
		IsDirectory: isDir,
	}
	if eventType == EventMoved {
		// fsnotify 的 RENAME 事件 raw.Name 是源路径；目标路径需要由 Python
		// 通过下一个 CREATE 事件关联，这里 dest_path 留空。
		ev.DestPath = ""
	}

	// 计数（无锁原子操作）
	if entry := m.entryByID(watchID); entry != nil {
		atomic.AddUint64(&entry.eventCount, 1)
	}
	_ = watchPath // 当前未用，预留给未来按 watch 路径过滤

	m.pushAsync(ctx, ev)
}

// pushAsync 用信号量限制 in-flight 推送数；信号量满时同步退化为 drop +
// warn，避免事件无限堆积撑爆内存。
func (m *Manager) pushAsync(ctx context.Context, ev Event) {
	select {
	case m.pushSem <- struct{}{}:
	default:
		m.logger.Warn("push 通道已满，丢弃事件",
			"event_id", ev.EventID, "src_path", ev.SrcPath)
		return
	}

	go func() {
		defer func() { <-m.pushSem }()
		// 单条回调最多 5 秒，避免 Python 端慢响应阻塞太久
		callCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
		defer cancel()
		if err := m.sink.Push(callCtx, ev); err != nil {
			m.logger.Warn("回调推送失败",
				"event_id", ev.EventID, "src_path", ev.SrcPath,
				"error", err.Error())
		}
	}()
}

// lookupWatch 反查路径属于哪个 watch_id，找不到返回空串。
//
// 优先精确匹配父目录；若是递归 watch 的深层子目录，则逐级回溯到根。
func (m *Manager) lookupWatch(path string) (string, string) {
	dir := filepath.Dir(path)

	m.mu.Lock()
	defer m.mu.Unlock()

	// 1. 精确父目录命中
	if id, ok := m.dirToWatch[dir]; ok {
		return id, m.watches[id].spec.Path
	}
	// 2. 逐级向上找（保护：最多回溯 50 层，防御异常路径）
	for i := 0; i < 50; i++ {
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		if id, ok := m.dirToWatch[parent]; ok {
			entry := m.watches[id]
			if entry != nil && entry.spec.Recursive {
				return id, entry.spec.Path
			}
		}
		dir = parent
	}
	return "", ""
}

// maybeAddSubdir 在递归 watch 下，对新建子目录动态加 watch。
func (m *Manager) maybeAddSubdir(watchID, path string) {
	m.mu.Lock()
	defer m.mu.Unlock()

	entry, ok := m.watches[watchID]
	if !ok || !entry.spec.Recursive || m.watcher == nil {
		return
	}
	if _, exists := m.dirToWatch[path]; exists {
		return
	}
	if err := m.watcher.Add(path); err != nil {
		m.logger.Debug("动态新增子目录 watch 失败",
			"path", path, "error", err.Error())
		return
	}
	entry.dirs[path] = struct{}{}
	m.dirToWatch[path] = watchID
	m.logger.Debug("动态新增子目录 watch", "watch_id", watchID, "path", path)
}

// entryByID 必须在持有 m.mu 时调用？此处只读 atomic 字段，但 map 本身仍需锁保护
func (m *Manager) entryByID(id string) *watchEntry {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.watches[id]
}

// mapEventType 将 fsnotify Op 翻译成对外的 EventType。
//
// 策略：
//   - CREATE / WRITE 都映射为 created/modified（最常见的"新文件落地"信号）
//   - RENAME 映射为 moved（dest 留给 Python 通过后续 CREATE 关联）
//   - REMOVE / CHMOD 不上报（Python 端目前不消费）
func mapEventType(op fsnotify.Op) (EventType, bool) {
	switch {
	case op.Has(fsnotify.Create):
		return EventCreated, true
	case op.Has(fsnotify.Write):
		return EventModified, true
	case op.Has(fsnotify.Rename):
		return EventMoved, true
	default:
		return "", false
	}
}

package watcher

import (
	"strings"
	"sync"
	"testing"

	"github.com/fsnotify/fsnotify"
)

func TestNextEventID_Unique(t *testing.T) {
	const n = 1000
	seen := make(map[string]struct{}, n)
	for i := 0; i < n; i++ {
		id := nextEventID()
		if _, dup := seen[id]; dup {
			t.Fatalf("发现重复 event id: %s", id)
		}
		seen[id] = struct{}{}
	}
}

func TestNextEventID_Format(t *testing.T) {
	id := nextEventID()
	// 形如 "<startTime base36>-<counter base36>"，至少有一个 "-"
	if !strings.Contains(id, "-") {
		t.Fatalf("event id 缺少分隔符: %s", id)
	}
	parts := strings.SplitN(id, "-", 2)
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		t.Fatalf("event id 格式异常: %s", id)
	}
}

func TestNextEventID_ConcurrentSafe(t *testing.T) {
	const goroutines = 20
	const perG = 500

	var wg sync.WaitGroup
	results := make([][]string, goroutines)
	for i := 0; i < goroutines; i++ {
		i := i
		wg.Add(1)
		go func() {
			defer wg.Done()
			ids := make([]string, perG)
			for j := 0; j < perG; j++ {
				ids[j] = nextEventID()
			}
			results[i] = ids
		}()
	}
	wg.Wait()

	seen := make(map[string]struct{}, goroutines*perG)
	for _, ids := range results {
		for _, id := range ids {
			if _, dup := seen[id]; dup {
				t.Fatalf("并发场景下重复 id: %s", id)
			}
			seen[id] = struct{}{}
		}
	}
}

func TestMapEventType(t *testing.T) {
	tests := []struct {
		name     string
		op       fsnotify.Op
		wantType EventType
		wantOK   bool
	}{
		{"create", fsnotify.Create, EventCreated, true},
		{"write", fsnotify.Write, EventModified, true},
		{"rename", fsnotify.Rename, EventMoved, true},
		{"remove not reported", fsnotify.Remove, "", false},
		{"chmod not reported", fsnotify.Chmod, "", false},
		// CREATE 优先级最高（fsnotify 有时会把 CREATE+WRITE 合并）
		{"create wins over write", fsnotify.Create | fsnotify.Write, EventCreated, true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, ok := mapEventType(tt.op)
			if ok != tt.wantOK {
				t.Errorf("ok = %v, want %v", ok, tt.wantOK)
			}
			if got != tt.wantType {
				t.Errorf("type = %q, want %q", got, tt.wantType)
			}
		})
	}
}

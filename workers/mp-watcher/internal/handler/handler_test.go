package handler

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"github.com/jxxghp/MoviePilot/workers/mp-watcher/internal/watcher"
)

// 用 manager + fakeSink 起一个真实的 Handler，避免 mock 整套接口
func newHandlerForTest(t *testing.T) *Handler {
	t.Helper()
	logger := slog.New(slog.NewTextHandler(io.Discard, &slog.HandlerOptions{
		Level: slog.LevelError,
	}))
	mgr := watcher.NewManager(logger, &noopSink{})
	if err := mgr.Start(context.Background()); err != nil {
		t.Fatalf("Start manager: %v", err)
	}
	t.Cleanup(func() { _ = mgr.Stop() })
	return New(logger, mgr)
}

type noopSink struct{}

func (noopSink) Push(_ context.Context, _ watcher.Event) error { return nil }

// 用 net/http 的 httptest 直接调底层 handler 函数
type apiResp struct {
	Code    int             `json:"code"`
	Message string          `json:"message"`
	Data    json.RawMessage `json:"data"`
}

func doJSON(t *testing.T, h http.HandlerFunc, method, body string) (int, apiResp) {
	t.Helper()
	req := httptest.NewRequest(method, "/", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	rr := httptest.NewRecorder()
	h(rr, req)
	var ar apiResp
	if err := json.NewDecoder(rr.Body).Decode(&ar); err != nil {
		t.Fatalf("解析响应失败: %v, raw=%s", err, rr.Body.String())
	}
	return rr.Code, ar
}

// ----- /configure -----

func TestConfigure_RejectsNonPost(t *testing.T) {
	h := newHandlerForTest(t)
	_, ar := doJSON(t, h.configure, http.MethodGet, "")
	if ar.Code == 0 {
		t.Fatal("非 POST 应返回业务错误码")
	}
}

func TestConfigure_RejectsBadJSON(t *testing.T) {
	h := newHandlerForTest(t)
	_, ar := doJSON(t, h.configure, http.MethodPost, "{not json")
	if ar.Code == 0 {
		t.Fatal("非法 JSON 应返回业务错误码")
	}
}

func TestConfigure_EmptyWatches_OK(t *testing.T) {
	h := newHandlerForTest(t)
	_, ar := doJSON(t, h.configure, http.MethodPost, `{"watches":[]}`)
	if ar.Code != 0 {
		t.Fatalf("空 watches 应该 ok，实际 code=%d msg=%s", ar.Code, ar.Message)
	}
	var data ConfigureResponse
	if err := json.Unmarshal(ar.Data, &data); err != nil {
		t.Fatal(err)
	}
	if data.Added != 0 || data.Removed != 0 || data.Total != 0 {
		t.Fatalf("data = %+v", data)
	}
}

func TestConfigure_AddOneDir(t *testing.T) {
	h := newHandlerForTest(t)
	dir := t.TempDir()
	body, _ := json.Marshal(map[string]any{
		"watches": []map[string]any{
			{"watch_id": "w1", "path": dir, "recursive": false},
		},
	})
	_, ar := doJSON(t, h.configure, http.MethodPost, string(body))
	if ar.Code != 0 {
		t.Fatalf("code=%d msg=%s", ar.Code, ar.Message)
	}
	var data ConfigureResponse
	_ = json.Unmarshal(ar.Data, &data)
	if data.Added != 1 || data.Total != 1 {
		t.Fatalf("data = %+v", data)
	}
}

func TestConfigure_PartialFailure_ReturnsWarning(t *testing.T) {
	h := newHandlerForTest(t)
	good := t.TempDir()
	bad := filepath.Join(t.TempDir(), "nonexistent-xyz")
	body, _ := json.Marshal(map[string]any{
		"watches": []map[string]any{
			{"watch_id": "w1", "path": good},
			{"watch_id": "w2", "path": bad},
		},
	})
	_, ar := doJSON(t, h.configure, http.MethodPost, string(body))
	if ar.Code != 0 {
		t.Fatalf("部分失败应返回 200/0 + warning，实际 code=%d msg=%s",
			ar.Code, ar.Message)
	}
	var data ConfigureResponse
	_ = json.Unmarshal(ar.Data, &data)
	if data.Added != 1 {
		t.Fatalf("应有 1 个 added: %+v", data)
	}
	if data.Warning == "" {
		t.Fatal("应有 warning 字段")
	}
}

func TestConfigure_AllInvalidPaths_ReturnsError(t *testing.T) {
	h := newHandlerForTest(t)
	body := `{"watches":[{"watch_id":"w1","path":"/no/such/dir/xyz"}]}`
	_, ar := doJSON(t, h.configure, http.MethodPost, body)
	if ar.Code == 0 {
		t.Fatal("全部失败应返回非 0 业务码")
	}
}

// ----- /watch_status -----

func TestWatchStatus_RejectsNonGet(t *testing.T) {
	h := newHandlerForTest(t)
	_, ar := doJSON(t, h.watchStatus, http.MethodPost, "")
	if ar.Code == 0 {
		t.Fatal("非 GET 应返回业务错误码")
	}
}

func TestWatchStatus_EmptyByDefault(t *testing.T) {
	h := newHandlerForTest(t)
	_, ar := doJSON(t, h.watchStatus, http.MethodGet, "")
	if ar.Code != 0 {
		t.Fatalf("code=%d msg=%s", ar.Code, ar.Message)
	}
	var data StatusResponse
	_ = json.Unmarshal(ar.Data, &data)
	if data.Count != 0 || len(data.Watches) != 0 {
		t.Fatalf("data = %+v", data)
	}
}

func TestWatchStatus_AfterConfigure(t *testing.T) {
	h := newHandlerForTest(t)
	dir := t.TempDir()
	cfgBody, _ := json.Marshal(map[string]any{
		"watches": []map[string]any{
			{"watch_id": "w1", "path": dir, "recursive": true},
		},
	})
	if _, ar := doJSON(t, h.configure, http.MethodPost, string(cfgBody)); ar.Code != 0 {
		t.Fatalf("configure 失败: %s", ar.Message)
	}
	_, ar := doJSON(t, h.watchStatus, http.MethodGet, "")
	var data StatusResponse
	_ = json.Unmarshal(ar.Data, &data)
	if data.Count != 1 {
		t.Fatalf("count = %d, want 1", data.Count)
	}
	if data.Watches[0].WatchID != "w1" {
		t.Fatalf("watches = %+v", data.Watches)
	}
}

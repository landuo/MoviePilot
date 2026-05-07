package handler

import (
	"bytes"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/landuo/MoviePilot/workers/mp-transfer/internal/ops"
)

// newTestHandler 构造一个静默 logger 的 handler 单例供测试复用。
func newTestHandler() *Handler {
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	return New(logger)
}

func doTransfer(t *testing.T, h *Handler, body any) (*http.Response, map[string]any) {
	t.Helper()
	buf, _ := json.Marshal(body)
	req := httptest.NewRequest(http.MethodPost, "/api/v1/transfer", bytes.NewReader(buf))
	rec := httptest.NewRecorder()
	h.transfer(rec, req)
	resp := rec.Result()
	defer resp.Body.Close()

	var out map[string]any
	respBody, _ := io.ReadAll(resp.Body)
	if len(respBody) > 0 {
		_ = json.Unmarshal(respBody, &out)
	}
	return resp, out
}

func TestHandler_Transfer_Copy_OK(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "src.txt")
	if err := os.WriteFile(src, []byte("ok"), 0o644); err != nil {
		t.Fatal(err)
	}
	dst := filepath.Join(dir, "dst.txt")

	h := newTestHandler()
	_, body := doTransfer(t, h, TransferRequest{
		Mode: string(ops.ModeCopy), Src: src, Dst: dst,
	})
	if got := body["code"]; got != float64(0) {
		t.Fatalf("code 应为 0，实际 %v, body=%v", got, body)
	}
	data, _ := body["data"].(map[string]any)
	if data["mode"] != "copy" {
		t.Errorf("mode 字段错误：%v", data)
	}
	// 计数器应自增
	if h.totalCalls.Load() != 1 {
		t.Errorf("total 应为 1，实际 %d", h.totalCalls.Load())
	}
	if h.byMode[ops.ModeCopy].Load() != 1 {
		t.Errorf("copy 计数应为 1，实际 %d", h.byMode[ops.ModeCopy].Load())
	}
}

func TestHandler_Transfer_BadMethod(t *testing.T) {
	h := newTestHandler()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/transfer", nil)
	rec := httptest.NewRecorder()
	h.transfer(rec, req)
	resp := rec.Result()
	defer resp.Body.Close()

	var body map[string]any
	raw, _ := io.ReadAll(resp.Body)
	_ = json.Unmarshal(raw, &body)
	if body["code"] == float64(0) {
		t.Errorf("非 POST 应返回业务错误，body=%v", body)
	}
}

func TestHandler_Transfer_BadJSON(t *testing.T) {
	h := newTestHandler()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/transfer", strings.NewReader("not-json"))
	rec := httptest.NewRecorder()
	h.transfer(rec, req)
	resp := rec.Result()
	defer resp.Body.Close()

	var body map[string]any
	raw, _ := io.ReadAll(resp.Body)
	_ = json.Unmarshal(raw, &body)
	if body["code"] == float64(0) {
		t.Errorf("非法 JSON 应返回业务错误")
	}
}

func TestHandler_Transfer_RelativePath(t *testing.T) {
	h := newTestHandler()
	_, body := doTransfer(t, h, TransferRequest{
		Mode: "copy", Src: "rel/a", Dst: "/tmp/b",
	})
	if body["code"] == float64(0) {
		t.Errorf("相对路径应被拒绝")
	}
	if h.failedCalls.Load() != 0 {
		// ValidatePaths 拒绝阶段不应计入 failed（那是 IO 错误的统计）
		t.Errorf("参数错误不应计入 failed")
	}
}

func TestHandler_Transfer_InvalidMode(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "src")
	if err := os.WriteFile(src, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	h := newTestHandler()
	_, body := doTransfer(t, h, TransferRequest{
		Mode: "wat", Src: src, Dst: filepath.Join(dir, "dst"),
	})
	if body["code"] == float64(0) {
		t.Errorf("非法 mode 应返回业务错误")
	}
	if h.failedCalls.Load() != 1 {
		t.Errorf("非法 mode 应计入 failed，实际 %d", h.failedCalls.Load())
	}
}

func TestHandler_Stats(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "src.txt")
	if err := os.WriteFile(src, []byte("hi"), 0o644); err != nil {
		t.Fatal(err)
	}
	h := newTestHandler()
	_, _ = doTransfer(t, h, TransferRequest{
		Mode: "copy", Src: src, Dst: filepath.Join(dir, "dst.txt"),
	})

	req := httptest.NewRequest(http.MethodGet, "/api/v1/transfer_stats", nil)
	rec := httptest.NewRecorder()
	h.stats(rec, req)
	resp := rec.Result()
	defer resp.Body.Close()

	var body map[string]any
	raw, _ := io.ReadAll(resp.Body)
	_ = json.Unmarshal(raw, &body)
	if body["code"] != float64(0) {
		t.Fatalf("stats 应返回 code=0, body=%v", body)
	}
	data := body["data"].(map[string]any)
	if data["total"].(float64) != 1 {
		t.Errorf("total 应为 1，实际 %v", data["total"])
	}
	byMode := data["by_mode"].(map[string]any)
	if byMode["copy"].(float64) != 1 {
		t.Errorf("copy 计数应为 1，实际 %v", byMode["copy"])
	}
}

func TestHandler_Stats_BadMethod(t *testing.T) {
	h := newTestHandler()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/transfer_stats", nil)
	rec := httptest.NewRecorder()
	h.stats(rec, req)
	resp := rec.Result()
	defer resp.Body.Close()

	var body map[string]any
	raw, _ := io.ReadAll(resp.Body)
	_ = json.Unmarshal(raw, &body)
	if body["code"] == float64(0) {
		t.Errorf("非 GET 应返回业务错误")
	}
}

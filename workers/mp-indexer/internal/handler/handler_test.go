package handler

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestHandleFetch_EmptyRequests(t *testing.T) {
	body, _ := json.Marshal(FetchRequest{Requests: []FetchItem{}})
	req := httptest.NewRequest(http.MethodPost, "/api/v1/fetch", bytes.NewReader(body))
	w := httptest.NewRecorder()
	handleFetch(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", w.Code)
	}
	var resp map[string]interface{}
	json.Unmarshal(w.Body.Bytes(), &resp)
	if resp["code"].(float64) != 1000 {
		t.Fatalf("expected code 1000 for empty requests, got %v", resp["code"])
	}
}

func TestHandleFetch_WrongMethod(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/api/v1/fetch", nil)
	w := httptest.NewRecorder()
	handleFetch(w, req)

	var resp map[string]interface{}
	json.Unmarshal(w.Body.Bytes(), &resp)
	if resp["code"].(float64) != 1000 {
		t.Fatalf("expected code 1000 for GET method, got %v", resp["code"])
	}
}

func TestHandleFetch_SingleRequest(t *testing.T) {
	// 启动一个简单的测试 HTTP 服务
	testServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Write([]byte("<html><body>hello</body></html>"))
	}))
	defer testServer.Close()

	fetchReq := FetchRequest{
		Requests: []FetchItem{
			{
				ID:             "test-1",
				URL:            testServer.URL,
				Method:         "GET",
				Headers:        map[string]string{"User-Agent": "TestBot/1.0"},
				TimeoutMs:      5000,
				AllowRedirects: true,
			},
		},
	}

	body, _ := json.Marshal(fetchReq)
	req := httptest.NewRequest(http.MethodPost, "/api/v1/fetch", bytes.NewReader(body))
	w := httptest.NewRecorder()
	handleFetch(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", w.Code)
	}

	var resp map[string]interface{}
	json.Unmarshal(w.Body.Bytes(), &resp)
	if resp["code"].(float64) != 0 {
		t.Fatalf("expected code 0, got %v", resp["code"])
	}

	data := resp["data"].(map[string]interface{})
	results := data["results"].([]interface{})
	if len(results) != 1 {
		t.Fatalf("expected 1 result, got %d", len(results))
	}

	result := results[0].(map[string]interface{})
	if result["id"] != "test-1" {
		t.Errorf("expected id test-1, got %v", result["id"])
	}
	if result["status_code"].(float64) != 200 {
		t.Errorf("expected status 200, got %v", result["status_code"])
	}
	if result["body"] != "<html><body>hello</body></html>" {
		t.Errorf("unexpected body: %v", result["body"])
	}
	if result["error"] != "" {
		t.Errorf("expected no error, got: %v", result["error"])
	}
}

func TestHandleFetch_ConcurrentRequests(t *testing.T) {
	callCount := 0
	testServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		callCount++
		w.Write([]byte("ok"))
	}))
	defer testServer.Close()

	items := make([]FetchItem, 5)
	for i := range items {
		items[i] = FetchItem{
			ID:        "concurrent-" + string(rune('a'+i)),
			URL:       testServer.URL,
			Method:    "GET",
			TimeoutMs: 5000,
		}
	}

	fetchReq := FetchRequest{Requests: items}
	body, _ := json.Marshal(fetchReq)
	req := httptest.NewRequest(http.MethodPost, "/api/v1/fetch", bytes.NewReader(body))
	w := httptest.NewRecorder()
	handleFetch(w, req)

	var resp map[string]interface{}
	json.Unmarshal(w.Body.Bytes(), &resp)
	if resp["code"].(float64) != 0 {
		t.Fatalf("expected code 0, got %v", resp["code"])
	}

	data := resp["data"].(map[string]interface{})
	results := data["results"].([]interface{})
	if len(results) != 5 {
		t.Fatalf("expected 5 results, got %d", len(results))
	}

	// 所有请求都应成功
	for _, r := range results {
		item := r.(map[string]interface{})
		if item["status_code"].(float64) != 200 {
			t.Errorf("expected status 200, got %v for %v", item["status_code"], item["id"])
		}
	}
}

func TestHandleFetch_InvalidURL(t *testing.T) {
	fetchReq := FetchRequest{
		Requests: []FetchItem{
			{
				ID:        "bad-url",
				URL:       "http://127.0.0.1:1", // 不可达端口
				Method:    "GET",
				TimeoutMs: 1000,
			},
		},
	}

	body, _ := json.Marshal(fetchReq)
	req := httptest.NewRequest(http.MethodPost, "/api/v1/fetch", bytes.NewReader(body))
	w := httptest.NewRecorder()
	handleFetch(w, req)

	var resp map[string]interface{}
	json.Unmarshal(w.Body.Bytes(), &resp)
	if resp["code"].(float64) != 0 {
		t.Fatalf("expected code 0 (batch always succeeds), got %v", resp["code"])
	}

	data := resp["data"].(map[string]interface{})
	results := data["results"].([]interface{})
	result := results[0].(map[string]interface{})
	if result["error"] == "" {
		t.Error("expected error for unreachable URL")
	}
	if result["status_code"].(float64) != 0 {
		t.Errorf("expected status 0 for failed request, got %v", result["status_code"])
	}
}

func TestHandleFetchStats(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/api/v1/fetch_stats", nil)
	w := httptest.NewRecorder()
	handleFetchStats(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", w.Code)
	}

	var resp map[string]interface{}
	json.Unmarshal(w.Body.Bytes(), &resp)
	if resp["code"].(float64) != 0 {
		t.Fatalf("expected code 0, got %v", resp["code"])
	}
}

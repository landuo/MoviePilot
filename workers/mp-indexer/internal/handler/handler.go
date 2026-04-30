package handler

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"sync"
	"sync/atomic"
	"time"

	"github.com/jxxghp/MoviePilot-Workers/shared/log"
	"github.com/jxxghp/MoviePilot-Workers/shared/transport"
)

// ---------- 请求 / 响应结构 ----------

// FetchItem 描述一个待发出的 HTTP 请求。
type FetchItem struct {
	ID             string            `json:"id"`
	URL            string            `json:"url"`
	Method         string            `json:"method"`
	Headers        map[string]string `json:"headers"`
	Proxy          string            `json:"proxy"`
	TimeoutMs      int               `json:"timeout_ms"`
	AllowRedirects bool              `json:"allow_redirects"`
}

// FetchRequest 是 POST /api/v1/fetch 的请求体。
type FetchRequest struct {
	Requests []FetchItem `json:"requests"`
}

// FetchResultItem 描述一个 HTTP 响应。
type FetchResultItem struct {
	ID         string            `json:"id"`
	StatusCode int               `json:"status_code"`
	Headers    map[string]string `json:"headers"`
	Body       string            `json:"body"`
	Error      string            `json:"error"`
	DurationMs int64             `json:"duration_ms"`
}

// FetchResponse 是 POST /api/v1/fetch 的响应体 data 部分。
type FetchResponse struct {
	Results         []FetchResultItem `json:"results"`
	TotalDurationMs int64             `json:"total_duration_ms"`
}

// ---------- 统计 ----------

type stats struct {
	TotalRequests  int64            `json:"total_requests"`
	FailedRequests int64            `json:"failed_requests"`
	BytesTotal     int64            `json:"bytes_total"`
	ByStatus       map[int]int64    `json:"by_status"`
	mu             sync.Mutex
}

func (s *stats) record(statusCode int, bodyLen int64, failed bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	atomic.AddInt64(&s.TotalRequests, 1)
	if failed {
		atomic.AddInt64(&s.FailedRequests, 1)
	}
	atomic.AddInt64(&s.BytesTotal, bodyLen)
	s.ByStatus[statusCode]++
}

func (s *stats) snapshot() map[string]interface{} {
	s.mu.Lock()
	defer s.mu.Unlock()
	byStatus := make(map[int]int64, len(s.ByStatus))
	for k, v := range s.ByStatus {
		byStatus[k] = v
	}
	return map[string]interface{}{
		"total_requests":  atomic.LoadInt64(&s.TotalRequests),
		"failed_requests": atomic.LoadInt64(&s.FailedRequests),
		"bytes_total":     atomic.LoadInt64(&s.BytesTotal),
		"by_status":       byStatus,
	}
}

// ---------- 全局状态 ----------

var globalStats = &stats{ByStatus: make(map[int]int64)}

// ---------- 路由注册 ----------

// Register 注册 mp-indexer 的业务路由。
func Register(server *transport.Server) {
	server.Handle("/api/v1/fetch", handleFetch)
	server.Handle("/api/v1/fetch_stats", handleFetchStats)
}

// ---------- POST /api/v1/fetch ----------

func handleFetch(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		transport.WriteError(w, 1000, "仅支持 POST 方法")
		return
	}

	var req FetchRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		transport.WriteError(w, 1000, "请求体解析失败: "+err.Error())
		return
	}
	if len(req.Requests) == 0 {
		transport.WriteError(w, 1000, "requests 列表为空")
		return
	}

	totalStart := time.Now()

	results := make([]FetchResultItem, len(req.Requests))
	var wg sync.WaitGroup

	for i, item := range req.Requests {
		wg.Add(1)
		go func(idx int, fi FetchItem) {
			defer wg.Done()
			results[idx] = doFetch(fi)
		}(i, item)
	}
	wg.Wait()

	resp := FetchResponse{
		Results:         results,
		TotalDurationMs: time.Since(totalStart).Milliseconds(),
	}

	log.L().Info("fetch 完成",
		"count", len(req.Requests),
		"total_ms", resp.TotalDurationMs,
	)

	transport.WriteOK(w, resp)
}

// doFetch 执行单个 HTTP 请求并返回结果。
func doFetch(fi FetchItem) FetchResultItem {
	start := time.Now()

	result := FetchResultItem{ID: fi.ID}

	// 默认值
	method := fi.Method
	if method == "" {
		method = http.MethodGet
	}
	timeoutMs := fi.TimeoutMs
	if timeoutMs <= 0 {
		timeoutMs = 15000
	}

	// 构建 HTTP client
	httpTransport := &http.Transport{
		TLSClientConfig: &tls.Config{InsecureSkipVerify: true},
	}

	// 代理
	if fi.Proxy != "" {
		proxyURL, err := url.Parse(fi.Proxy)
		if err != nil {
			result.Error = "代理地址解析失败: " + err.Error()
			result.DurationMs = time.Since(start).Milliseconds()
			globalStats.record(0, 0, true)
			return result
		}
		httpTransport.Proxy = http.ProxyURL(proxyURL)
	}

	client := &http.Client{
		Transport: httpTransport,
		Timeout:   time.Duration(timeoutMs) * time.Millisecond,
	}

	// 不跟随重定向
	if !fi.AllowRedirects {
		client.CheckRedirect = func(req *http.Request, via []*http.Request) error {
			return http.ErrUseLastResponse
		}
	}

	// 构建请求
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(timeoutMs)*time.Millisecond)
	defer cancel()

	httpReq, err := http.NewRequestWithContext(ctx, method, fi.URL, nil)
	if err != nil {
		result.Error = "构建请求失败: " + err.Error()
		result.DurationMs = time.Since(start).Milliseconds()
		globalStats.record(0, 0, true)
		return result
	}

	// 设置请求头
	for key, value := range fi.Headers {
		httpReq.Header.Set(key, value)
	}

	// 执行请求
	resp, err := client.Do(httpReq)
	if err != nil {
		result.Error = "请求失败: " + err.Error()
		result.DurationMs = time.Since(start).Milliseconds()
		globalStats.record(0, 0, true)
		log.L().Warn("fetch 失败",
			"id", fi.ID,
			"url", fi.URL,
			"error", err.Error(),
		)
		return result
	}
	defer resp.Body.Close()

	// 读取响应体
	bodyBytes, err := io.ReadAll(resp.Body)
	if err != nil {
		result.Error = "读取响应体失败: " + err.Error()
		result.StatusCode = resp.StatusCode
		result.DurationMs = time.Since(start).Milliseconds()
		globalStats.record(resp.StatusCode, 0, true)
		return result
	}

	// 提取响应头（只保留常用的）
	respHeaders := make(map[string]string)
	for _, key := range []string{"Content-Type", "Content-Length", "Location", "Set-Cookie"} {
		if v := resp.Header.Get(key); v != "" {
			respHeaders[key] = v
		}
	}

	result.StatusCode = resp.StatusCode
	result.Headers = respHeaders
	result.Body = string(bodyBytes)
	result.DurationMs = time.Since(start).Milliseconds()

	failed := resp.StatusCode >= 400
	globalStats.record(resp.StatusCode, int64(len(bodyBytes)), failed)

	return result
}

// ---------- GET /api/v1/fetch_stats ----------

func handleFetchStats(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		transport.WriteError(w, 1000, "仅支持 GET 方法")
		return
	}
	transport.WriteOK(w, globalStats.snapshot())
}

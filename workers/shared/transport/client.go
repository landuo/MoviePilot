package transport

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// CallbackClient 用于 worker 主动推送事件回 Python 主进程。
//
// 注意：依赖本机进程隔离，不做签名鉴权。
type CallbackClient struct {
	url        string
	httpClient *http.Client
}

// NewCallbackClient 创建一个回调客户端，timeout 为单次请求超时。
func NewCallbackClient(url string, timeout time.Duration) *CallbackClient {
	return &CallbackClient{
		url: url,
		httpClient: &http.Client{
			Timeout: timeout,
		},
	}
}

// Post 推送 payload，自动 JSON 序列化。
//
// 失败时返回错误，调用方应自行处理重试策略（避免回调风暴）。
func (c *CallbackClient) Post(ctx context.Context, payload interface{}) error {
	if c.url == "" {
		return fmt.Errorf("callback url 未配置")
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("序列化失败：%w", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	// 读完响应体以复用连接
	_, _ = io.Copy(io.Discard, resp.Body)

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("回调返回非 200：%d", resp.StatusCode)
	}
	return nil
}

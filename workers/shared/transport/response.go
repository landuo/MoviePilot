// Package transport 提供 worker 通用的 HTTP 服务端 / 客户端封装。
package transport

import (
	"encoding/json"
	"net/http"
)

// 业务错误码段位约定（与 Python 端 schemas/worker.py 保持一致）。
const (
	CodeOK          = 0
	CodeBadRequest  = 1000
	CodeInternal    = 2000
	CodeExternalIO  = 3000
	CodeUnknown     = 9000
)

// Response 是所有 worker HTTP 响应的统一结构。
type Response struct {
	Code    int         `json:"code"`
	Message string      `json:"message"`
	Data    interface{} `json:"data,omitempty"`
}

// WriteJSON 序列化 resp 并以 200 状态写回。
//
// 业务错误（非 0 code）也走 200，让 Python 端通过 code 字段判断；
// 仅在 HTTP 协议本身出错时才使用非 200 状态码（如 404 路由不存在）。
func WriteJSON(w http.ResponseWriter, resp Response) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(resp)
}

// WriteOK 是 WriteJSON 的便捷方法，data 可为 nil。
func WriteOK(w http.ResponseWriter, data interface{}) {
	WriteJSON(w, Response{Code: CodeOK, Message: "ok", Data: data})
}

// WriteError 写入业务错误响应。
func WriteError(w http.ResponseWriter, code int, message string) {
	WriteJSON(w, Response{Code: code, Message: message})
}

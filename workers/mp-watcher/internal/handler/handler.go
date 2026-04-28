// Package handler 把 watcher.Manager 暴露成 HTTP 接口。
package handler

import (
	"encoding/json"
	"log/slog"
	"net/http"

	"github.com/jxxghp/MoviePilot/workers/mp-watcher/internal/watcher"
	"github.com/jxxghp/MoviePilot/workers/shared/transport"
)

// Handler 把 watcher.Manager 暴露成 HTTP 接口。
type Handler struct {
	logger  *slog.Logger
	manager *watcher.Manager
}

// New 构造一个 Handler。
func New(logger *slog.Logger, mgr *watcher.Manager) *Handler {
	return &Handler{logger: logger, manager: mgr}
}

// Register 把所有业务路由注册到 server。
func (h *Handler) Register(server *transport.Server) {
	server.Handle("/api/v1/configure", h.configure)
	server.Handle("/api/v1/watch_status", h.watchStatus)
}

// ConfigureRequest 是 /api/v1/configure 的请求体。
type ConfigureRequest struct {
	Watches []watcher.WatchSpec `json:"watches"`
}

// ConfigureResponse 是 /api/v1/configure 的响应数据。
type ConfigureResponse struct {
	Added   int    `json:"added"`
	Removed int    `json:"removed"`
	Total   int    `json:"total"`
	Warning string `json:"warning,omitempty"`
}

func (h *Handler) configure(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		transport.WriteError(w, transport.CodeBadRequest, "只支持 POST")
		return
	}
	var req ConfigureRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		transport.WriteError(w, transport.CodeBadRequest, "请求体非法 JSON："+err.Error())
		return
	}
	defer r.Body.Close()

	added, removed, err := h.manager.Configure(req.Watches)
	resp := ConfigureResponse{
		Added:   added,
		Removed: removed,
		Total:   len(req.Watches),
	}
	if err != nil {
		// 部分失败仍然返回 200 + 业务码 0，把警告塞 warning，由 Python 端记日志
		// （彻底失败例如 manager 未启动才走 CodeInternal）
		if added == 0 && removed == 0 {
			h.logger.Error("configure 失败", "error", err.Error())
			transport.WriteError(w, transport.CodeInternal, err.Error())
			return
		}
		resp.Warning = err.Error()
		h.logger.Warn("configure 部分失败", "error", err.Error())
	}
	h.logger.Info("configure 完成",
		"added", added, "removed", removed, "total", len(req.Watches))
	transport.WriteOK(w, resp)
}

// StatusResponse 是 /api/v1/watch_status 的响应数据。
type StatusResponse struct {
	Watches []watcher.WatchStatus `json:"watches"`
	Count   int                   `json:"count"`
}

func (h *Handler) watchStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		transport.WriteError(w, transport.CodeBadRequest, "只支持 GET")
		return
	}
	statuses := h.manager.Status()
	transport.WriteOK(w, StatusResponse{
		Watches: statuses,
		Count:   len(statuses),
	})
}

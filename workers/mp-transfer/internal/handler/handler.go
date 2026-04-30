// Package handler 把 ops 暴露为 HTTP 接口。
package handler

import (
	"encoding/json"
	"log/slog"
	"net/http"
	"sync"
	"sync/atomic"

	"github.com/jxxghp/MoviePilot/workers/mp-transfer/internal/ops"
	"github.com/jxxghp/MoviePilot/workers/shared/transport"
)

// Handler 把 ops.Run 暴露成 HTTP 接口，并维护轻量计数器供巡检。
type Handler struct {
	logger *slog.Logger

	// 计数器：所有字段都通过 atomic 操作，按 mode 累加调用次数与字节数
	statsMu     sync.RWMutex
	totalCalls  atomic.Int64
	failedCalls atomic.Int64
	bytesTotal  atomic.Int64
	byMode      map[ops.Mode]*atomic.Int64
}

// New 构造 Handler 并预初始化每种 mode 的计数器，避免运行时分配。
func New(logger *slog.Logger) *Handler {
	return &Handler{
		logger: logger,
		byMode: map[ops.Mode]*atomic.Int64{
			ops.ModeCopy:     {},
			ops.ModeMove:     {},
			ops.ModeLink:     {},
			ops.ModeSoftlink: {},
		},
	}
}

// Register 把所有业务路由注册到 server。
func (h *Handler) Register(server *transport.Server) {
	server.Handle("/api/v1/transfer", h.transfer)
	server.Handle("/api/v1/transfer_stats", h.stats)
}

// TransferRequest 是 /api/v1/transfer 的请求体。字段与 Python 端 schemas.WorkerTransferRequest 对齐。
type TransferRequest struct {
	Mode string `json:"mode"`
	Src  string `json:"src"`
	Dst  string `json:"dst"`
}

func (h *Handler) transfer(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		transport.WriteError(w, transport.CodeBadRequest, "只支持 POST")
		return
	}
	var req TransferRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		transport.WriteError(w, transport.CodeBadRequest, "请求体非法 JSON："+err.Error())
		return
	}
	defer r.Body.Close()

	if err := ops.ValidatePaths(req.Src, req.Dst); err != nil {
		transport.WriteError(w, transport.CodeBadRequest, err.Error())
		return
	}

	mode := ops.Mode(req.Mode)
	result, err := ops.Run(mode, req.Src, req.Dst)
	if err != nil {
		h.failedCalls.Add(1)
		h.logger.Warn("transfer 失败",
			"mode", req.Mode, "src", req.Src, "dst", req.Dst, "error", err.Error())
		// 区分参数类错误（返回 1000）与 IO 类错误（返回 3000），便于 Python 侧分类降级
		code := transport.CodeExternalIO
		if mode == "" || isInvalidModeErr(err) {
			code = transport.CodeBadRequest
		}
		transport.WriteError(w, code, err.Error())
		return
	}

	h.totalCalls.Add(1)
	h.bytesTotal.Add(result.Bytes)
	if counter, ok := h.byMode[mode]; ok {
		counter.Add(1)
	}
	h.logger.Debug("transfer 成功",
		"mode", req.Mode, "src", req.Src, "dst", req.Dst,
		"bytes", result.Bytes, "duration_ms", result.DurationMS)

	transport.WriteOK(w, result)
}

// StatsResponse 是 /api/v1/transfer_stats 的响应数据。
type StatsResponse struct {
	Total      int64            `json:"total"`
	Failed     int64            `json:"failed"`
	BytesTotal int64            `json:"bytes_total"`
	ByMode     map[string]int64 `json:"by_mode"`
}

func (h *Handler) stats(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		transport.WriteError(w, transport.CodeBadRequest, "只支持 GET")
		return
	}
	h.statsMu.RLock()
	defer h.statsMu.RUnlock()

	byMode := make(map[string]int64, len(h.byMode))
	for mode, c := range h.byMode {
		byMode[string(mode)] = c.Load()
	}
	transport.WriteOK(w, StatsResponse{
		Total:      h.totalCalls.Load(),
		Failed:     h.failedCalls.Load(),
		BytesTotal: h.bytesTotal.Load(),
		ByMode:     byMode,
	})
}

// isInvalidModeErr 仅供 transfer handler 使用，判断是否为 mode 不识别错误。
//
// 错误码分流的目的：
//   - mode 错误属于调用方 bug，Python 侧不应该 fallback（fallback 也会抛同样的错）；
//   - IO 错误才是降级到本地 shutil 的合理时机。
func isInvalidModeErr(err error) bool {
	return err != nil && (err == ops.ErrInvalidMode ||
		// errors.Is 兼容 fmt.Errorf("%w") 包装
		errorsIs(err, ops.ErrInvalidMode))
}

// errorsIs 是 errors.Is 的局部别名，避免在文件顶部多导入一个包仅为这一行调用。
func errorsIs(err, target error) bool {
	type unwrapper interface{ Unwrap() error }
	for err != nil {
		if err == target {
			return true
		}
		u, ok := err.(unwrapper)
		if !ok {
			return false
		}
		err = u.Unwrap()
	}
	return false
}

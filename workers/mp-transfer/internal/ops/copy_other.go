//go:build !linux

package ops

import (
	"errors"
	"os"
	"syscall"
)

// copyFileContent 非 Linux 平台直接走 io.Copy。
//
// macOS / Windows / *BSD 也有各自的零拷贝接口（如 fcopyfile / TransmitFile），
// 但当前 worker 主要部署在 Linux 容器，其他平台仅作开发联调用，
// 不引入额外平台分支以降低维护成本。
func copyFileContent(dst *os.File, src *os.File, size int64) (int64, error) {
	_ = size
	return fallbackCopy(dst, src)
}

// isCrossDevice 在非 Linux 平台同样识别 EXDEV，触发 copy+remove 退化。
func isCrossDevice(err error) bool {
	var linkErr *os.LinkError
	if errors.As(err, &linkErr) {
		var errno syscall.Errno
		if errors.As(linkErr.Err, &errno) {
			return errno == syscall.EXDEV
		}
	}
	return false
}

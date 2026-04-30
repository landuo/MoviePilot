//go:build linux

package ops

import (
	"errors"
	"io"
	"os"
	"syscall"

	"golang.org/x/sys/unix"
)

// copyFileContent 在 Linux 上优先使用 copy_file_range（内核侧零拷贝），
// 失败时按错误类型决定是否退化为 io.Copy：
//   - EXDEV：跨设备/跨文件系统，内核拒绝；
//   - ENOSYS：内核太老（< 4.5）；
//   - EINVAL/EOPNOTSUPP：源/目标不在普通文件系统上（如 NFS、tmpfs 在某些场景）。
//
// 退化策略保证此函数在任何 Linux 环境下都能成功拷贝。
func copyFileContent(dst *os.File, src *os.File, size int64) (int64, error) {
	if size <= 0 {
		// 空文件直接结束，避免对 0 字节调用 syscall
		return 0, nil
	}

	var written int64
	for written < size {
		remaining := size - written
		// 每次最多拷 1 GiB，避免一次系统调用占用过久
		const maxChunk = int64(1) << 30
		chunk := remaining
		if chunk > maxChunk {
			chunk = maxChunk
		}

		n, err := unix.CopyFileRange(int(src.Fd()), nil, int(dst.Fd()), nil, int(chunk), 0)
		if err != nil {
			if isCopyFileRangeFallback(err) {
				// 退化到 io.Copy：先 seek 回 written 偏移，避免重复字节
				if _, seekErr := src.Seek(written, io.SeekStart); seekErr != nil {
					return written, seekErr
				}
				if _, seekErr := dst.Seek(written, io.SeekStart); seekErr != nil {
					return written, seekErr
				}
				more, copyErr := fallbackCopy(dst, src)
				return written + more, copyErr
			}
			return written, err
		}
		if n == 0 {
			// 内核返回 0 表示已到 EOF（理论上不会先于 written < size 出现，防御一下）
			break
		}
		written += int64(n)
	}
	return written, nil
}

// isCopyFileRangeFallback 判断 copy_file_range 是否需要降级到 io.Copy。
func isCopyFileRangeFallback(err error) bool {
	var errno syscall.Errno
	if !errors.As(err, &errno) {
		return false
	}
	switch errno {
	case syscall.ENOSYS, syscall.EXDEV, syscall.EINVAL, syscall.EOPNOTSUPP:
		return true
	default:
		return false
	}
}

// isCrossDevice 判断 rename 是否因跨设备失败，决定是否走 copy+remove 退化。
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

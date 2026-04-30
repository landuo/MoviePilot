// Package ops 实现 mp-transfer 的核心物理 IO 操作：copy / move / link / softlink。
//
// 设计要点：
//   - 行为与 Python 端 app/utils/system.py 中的对应静态方法严格对齐，
//     这样 Python 端 worker 调用失败 fallback 到 shutil 时业务结果一致。
//   - 不创建父目录（与原 SystemUtils 行为一致），由调用方保证 dst 父目录存在。
//   - copy 在 Linux 用 unix.CopyFileRange 走零拷贝，其他平台走 io.Copy。
//   - move 优先 os.Rename（同设备 inode 不变）；跨设备时退化为 copy + remove。
//   - link 沿用 "tmp 后缀 + rename" 模式，避免目录监控触发半成品事件。
package ops

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"time"
)

// Mode 表示一次 transfer 操作的类型。与 Python 端 transfer mode 名称对齐。
type Mode string

const (
	ModeCopy     Mode = "copy"
	ModeMove     Mode = "move"
	ModeLink     Mode = "link"
	ModeSoftlink Mode = "softlink"
)

// Result 是一次 transfer 操作的结果摘要，由 handler 包装为 HTTP 响应。
type Result struct {
	Mode       Mode  `json:"mode"`
	Bytes      int64 `json:"bytes"`
	DurationMS int64 `json:"duration_ms"`
}

// ErrInvalidMode 表示不识别的 mode 字符串。
var ErrInvalidMode = errors.New("无效的 transfer mode")

// Run 执行一次 transfer 操作。
//
// 入参约束：
//   - mode 必须是 4 个已知值之一，否则返回 ErrInvalidMode；
//   - src 必须存在；
//   - dst 父目录必须存在（由调用方保证）。
func Run(mode Mode, src, dst string) (*Result, error) {
	start := time.Now()

	switch mode {
	case ModeCopy:
		n, err := doCopy(src, dst)
		if err != nil {
			return nil, err
		}
		return makeResult(mode, n, start), nil
	case ModeMove:
		n, err := doMove(src, dst)
		if err != nil {
			return nil, err
		}
		return makeResult(mode, n, start), nil
	case ModeLink:
		n, err := doHardlink(src, dst)
		if err != nil {
			return nil, err
		}
		return makeResult(mode, n, start), nil
	case ModeSoftlink:
		if err := doSoftlink(src, dst); err != nil {
			return nil, err
		}
		// softlink 不计入字节数（与 shutil 行为一致：symlink_to 不复制内容）
		return makeResult(mode, 0, start), nil
	default:
		return nil, fmt.Errorf("%w: %q", ErrInvalidMode, mode)
	}
}

func makeResult(mode Mode, bytes int64, start time.Time) *Result {
	return &Result{
		Mode:       mode,
		Bytes:      bytes,
		DurationMS: time.Since(start).Milliseconds(),
	}
}

// doCopy 平台无关的 copy 入口：优先尝试零拷贝（Linux），失败 fallback io.Copy。
//
// 零拷贝实现见 copy_linux.go；其他平台见 copy_other.go。
func doCopy(src, dst string) (int64, error) {
	srcFile, err := os.Open(src)
	if err != nil {
		return 0, fmt.Errorf("打开源文件失败：%w", err)
	}
	defer srcFile.Close()

	srcInfo, err := srcFile.Stat()
	if err != nil {
		return 0, fmt.Errorf("读取源文件元信息失败：%w", err)
	}
	if !srcInfo.Mode().IsRegular() {
		return 0, fmt.Errorf("源不是常规文件：%s", src)
	}

	// 与 shutil.copy2 一致：保留原文件权限位
	dstFile, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, srcInfo.Mode().Perm())
	if err != nil {
		return 0, fmt.Errorf("创建目标文件失败：%w", err)
	}
	defer dstFile.Close()

	n, err := copyFileContent(dstFile, srcFile, srcInfo.Size())
	if err != nil {
		// 失败时尽量清理半成品，避免误导上层判定
		_ = os.Remove(dst)
		return n, fmt.Errorf("拷贝内容失败：%w", err)
	}

	// 同步元数据（mtime/atime），与 shutil.copy2 行为对齐
	if err := os.Chtimes(dst, srcInfo.ModTime(), srcInfo.ModTime()); err != nil {
		// 元数据失败不影响拷贝主体，仅记录返回错误前提下的字节数
		return n, fmt.Errorf("同步时间戳失败：%w", err)
	}
	return n, nil
}

// doMove 优先同设备 rename，跨设备则降级为 copy + remove。
func doMove(src, dst string) (int64, error) {
	if err := os.Rename(src, dst); err == nil {
		// rename 成功不会读取字节数，返回 0 即可（与 shutil.move 一致：调用方不依赖 bytes）
		return 0, nil
	} else if !isCrossDevice(err) {
		return 0, fmt.Errorf("重命名失败：%w", err)
	}

	// 跨设备：copy + remove
	n, err := doCopy(src, dst)
	if err != nil {
		return n, err
	}
	if err := os.Remove(src); err != nil {
		return n, fmt.Errorf("跨设备移动后清理源文件失败：%w", err)
	}
	return n, nil
}

// doHardlink 复用 Python 端 "tmp 后缀 + rename" 模式：
// 先创建 dst.mp 硬链接，再 rename 为 dst，避免目录监控感知到半成品。
func doHardlink(src, dst string) (int64, error) {
	tmp := dst + ".mp"

	// 如果 tmp 已存在（上次失败残留），先清掉
	if _, err := os.Lstat(tmp); err == nil {
		if rmErr := os.Remove(tmp); rmErr != nil {
			return 0, fmt.Errorf("清理残留 tmp 失败：%w", rmErr)
		}
	}

	if err := os.Link(src, tmp); err != nil {
		return 0, fmt.Errorf("创建硬链接失败：%w", err)
	}
	if err := os.Rename(tmp, dst); err != nil {
		// 创建成功但改名失败，清理 tmp 防泄漏
		_ = os.Remove(tmp)
		return 0, fmt.Errorf("硬链接重命名失败：%w", err)
	}
	// 硬链接不复制内容，bytes 返回 0；想要展示文件大小由调用方自行 stat
	return 0, nil
}

// doSoftlink 直接调用 os.Symlink。与 Python 端 dst.symlink_to(src) 行为一致。
func doSoftlink(src, dst string) error {
	if err := os.Symlink(src, dst); err != nil {
		return fmt.Errorf("创建软链接失败：%w", err)
	}
	return nil
}

// fallbackCopy 是 io.Copy 的薄封装，作为零拷贝失败或不支持平台的 fallback。
func fallbackCopy(dst io.Writer, src io.Reader) (int64, error) {
	return io.Copy(dst, src)
}

// ValidatePaths 在 handler 入口侧调用，集中做参数校验。
//
// 规则：
//   - src/dst 不能为空；
//   - 必须是绝对路径（避免在 worker 进程 cwd 上下文出歧义）；
//   - src/dst 不能相同（拦截无意义的自拷贝）。
func ValidatePaths(src, dst string) error {
	if src == "" || dst == "" {
		return errors.New("src 与 dst 都不能为空")
	}
	if !filepath.IsAbs(src) {
		return fmt.Errorf("src 必须是绝对路径：%s", src)
	}
	if !filepath.IsAbs(dst) {
		return fmt.Errorf("dst 必须是绝对路径：%s", dst)
	}
	if src == dst {
		return errors.New("src 与 dst 相同，拒绝执行")
	}
	return nil
}

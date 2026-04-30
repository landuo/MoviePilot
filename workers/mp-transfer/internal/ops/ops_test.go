package ops

import (
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"testing"
)

// writeTempFile 在 t.TempDir() 下创建一个内容已知的源文件，返回完整路径。
func writeTempFile(t *testing.T, dir, name, content string) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("写入临时文件失败：%v", err)
	}
	return path
}

func mustStat(t *testing.T, path string) os.FileInfo {
	t.Helper()
	fi, err := os.Lstat(path)
	if err != nil {
		t.Fatalf("Lstat %s 失败：%v", path, err)
	}
	return fi
}

// inodeOf 返回文件的 inode（仅 Unix 系），用于校验硬链接是否共享 inode。
func inodeOf(t *testing.T, path string) uint64 {
	t.Helper()
	fi := mustStat(t, path)
	st, ok := fi.Sys().(*syscall.Stat_t)
	if !ok {
		t.Skipf("当前平台不支持 inode 检查（%s）", runtime.GOOS)
	}
	return st.Ino
}

func TestRun_Copy(t *testing.T) {
	dir := t.TempDir()
	src := writeTempFile(t, dir, "src.txt", "hello mp-transfer")
	dst := filepath.Join(dir, "dst.txt")

	res, err := Run(ModeCopy, src, dst)
	if err != nil {
		t.Fatalf("copy 失败：%v", err)
	}
	if res.Mode != ModeCopy {
		t.Errorf("mode 应为 copy，实际：%s", res.Mode)
	}
	if res.Bytes != int64(len("hello mp-transfer")) {
		t.Errorf("bytes 错误，期望 %d，实际 %d", len("hello mp-transfer"), res.Bytes)
	}
	got, err := os.ReadFile(dst)
	if err != nil {
		t.Fatalf("读取目标失败：%v", err)
	}
	if string(got) != "hello mp-transfer" {
		t.Errorf("内容不一致：%q", got)
	}
	// 源文件应保留
	if _, err := os.Stat(src); err != nil {
		t.Errorf("copy 后源文件不应被删除：%v", err)
	}
}

func TestRun_Copy_EmptyFile(t *testing.T) {
	// 0 字节文件是 copy_file_range 的边界，单独覆盖
	dir := t.TempDir()
	src := writeTempFile(t, dir, "empty.bin", "")
	dst := filepath.Join(dir, "dst.bin")

	res, err := Run(ModeCopy, src, dst)
	if err != nil {
		t.Fatalf("空文件 copy 失败：%v", err)
	}
	if res.Bytes != 0 {
		t.Errorf("空文件 bytes 应为 0，实际 %d", res.Bytes)
	}
	fi := mustStat(t, dst)
	if fi.Size() != 0 {
		t.Errorf("dst 应为空文件，实际 size=%d", fi.Size())
	}
}

func TestRun_Move(t *testing.T) {
	dir := t.TempDir()
	src := writeTempFile(t, dir, "src.txt", "moveme")
	dst := filepath.Join(dir, "dst.txt")

	if _, err := Run(ModeMove, src, dst); err != nil {
		t.Fatalf("move 失败：%v", err)
	}
	if _, err := os.Stat(src); !errors.Is(err, os.ErrNotExist) {
		t.Errorf("move 后源文件应不存在，stat err=%v", err)
	}
	got, _ := os.ReadFile(dst)
	if string(got) != "moveme" {
		t.Errorf("move 后目标内容错误：%q", got)
	}
}

func TestRun_Hardlink_SharesInode(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Windows hardlink 行为不一致，跳过")
	}
	dir := t.TempDir()
	src := writeTempFile(t, dir, "src.txt", "linkme")
	dst := filepath.Join(dir, "dst.txt")

	if _, err := Run(ModeLink, src, dst); err != nil {
		t.Fatalf("link 失败：%v", err)
	}
	if inodeOf(t, src) != inodeOf(t, dst) {
		t.Errorf("hardlink 应与源共享 inode")
	}
	// 中间产物 .mp 不应残留
	if _, err := os.Stat(dst + ".mp"); !errors.Is(err, os.ErrNotExist) {
		t.Errorf("link 后不应残留 .mp 中间文件")
	}
}

func TestRun_Hardlink_CleanupResidualTmp(t *testing.T) {
	// 模拟上次 link 失败留下的 .mp 残留，应能被清理后继续
	if runtime.GOOS == "windows" {
		t.Skip("Windows hardlink 行为不一致，跳过")
	}
	dir := t.TempDir()
	src := writeTempFile(t, dir, "src.txt", "data")
	dst := filepath.Join(dir, "dst.txt")
	residual := dst + ".mp"
	if err := os.WriteFile(residual, []byte("stale"), 0o644); err != nil {
		t.Fatalf("准备残留文件失败：%v", err)
	}

	if _, err := Run(ModeLink, src, dst); err != nil {
		t.Fatalf("link 失败：%v", err)
	}
	if _, err := os.Stat(residual); !errors.Is(err, os.ErrNotExist) {
		t.Errorf("残留 .mp 应被清理")
	}
}

func TestRun_Softlink(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Windows symlink 需要管理员权限，跳过")
	}
	dir := t.TempDir()
	src := writeTempFile(t, dir, "src.txt", "symlink target")
	dst := filepath.Join(dir, "dst.txt")

	res, err := Run(ModeSoftlink, src, dst)
	if err != nil {
		t.Fatalf("softlink 失败：%v", err)
	}
	if res.Bytes != 0 {
		t.Errorf("softlink bytes 应为 0，实际 %d", res.Bytes)
	}
	fi := mustStat(t, dst)
	if fi.Mode()&os.ModeSymlink == 0 {
		t.Errorf("dst 应为 symlink，mode=%v", fi.Mode())
	}
	// 通过 symlink 读到的内容应与源一致
	got, err := os.ReadFile(dst)
	if err != nil {
		t.Fatalf("通过 symlink 读取失败：%v", err)
	}
	if string(got) != "symlink target" {
		t.Errorf("内容不一致：%q", got)
	}
}

func TestRun_InvalidMode(t *testing.T) {
	dir := t.TempDir()
	src := writeTempFile(t, dir, "src.txt", "x")
	dst := filepath.Join(dir, "dst.txt")

	_, err := Run("not-a-mode", src, dst)
	if !errors.Is(err, ErrInvalidMode) {
		t.Errorf("无效 mode 应返回 ErrInvalidMode，实际 %v", err)
	}
}

func TestRun_CopyMissingSrc(t *testing.T) {
	dir := t.TempDir()
	_, err := Run(ModeCopy, filepath.Join(dir, "nope"), filepath.Join(dir, "dst"))
	if err == nil {
		t.Fatal("源不存在应返回错误")
	}
	if !strings.Contains(err.Error(), "打开源文件失败") {
		t.Errorf("错误信息应指明打开源文件失败，实际：%v", err)
	}
}

func TestValidatePaths(t *testing.T) {
	cases := []struct {
		name      string
		src, dst  string
		expectErr bool
	}{
		{"both empty", "", "", true},
		{"src empty", "", "/tmp/a", true},
		{"dst empty", "/tmp/a", "", true},
		{"relative src", "rel/a", "/tmp/b", true},
		{"relative dst", "/tmp/a", "rel/b", true},
		{"identical", "/tmp/a", "/tmp/a", true},
		{"valid", "/tmp/a", "/tmp/b", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := ValidatePaths(tc.src, tc.dst)
			if tc.expectErr && err == nil {
				t.Errorf("应返回错误")
			}
			if !tc.expectErr && err != nil {
				t.Errorf("不应返回错误，实际：%v", err)
			}
		})
	}
}

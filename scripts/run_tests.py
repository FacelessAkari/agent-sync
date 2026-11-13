"""Release gate: real symlinks, Git, unittest, and zero skips."""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


def prerequisites():
    if shutil.which('git') is None:
        raise RuntimeError('需要系统 Git')
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp).resolve()
        target = root / 'target.txt'
        target.write_bytes(b'symlink-probe')
        directory = root / 'directory'
        directory.mkdir()
        file_link = root / 'file-link'
        dir_link = root / 'dir-link'
        try:
            file_link.symlink_to(target)
            dir_link.symlink_to(directory, target_is_directory=True)
            if not file_link.is_symlink() or file_link.read_bytes() != b'symlink-probe':
                raise RuntimeError('真实文件 symlink 检查失败')
            if not dir_link.is_symlink() or not dir_link.is_dir():
                raise RuntimeError('真实目录 symlink 检查失败')
        finally:
            for link in (file_link, dir_link):
                if link.is_symlink():
                    link.unlink()


def main():
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    sys.path.insert(0, str(root))
    try:
        prerequisites()
    except (OSError, RuntimeError) as exc:
        print('测试前置条件失败：' + str(exc) + '\nWindows 必须具备真实 symlink 权限；必要时在已授权管理员环境执行。', file=sys.stderr)
        return 1
    suite = unittest.defaultTestLoader.discover(str(root / 'tests'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped:
        print('发布检查失败：skipped=' + str(len(result.skipped)), file=sys.stderr)
    return 0 if result.wasSuccessful() and not result.skipped and result.testsRun > 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())

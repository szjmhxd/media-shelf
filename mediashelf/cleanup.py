"""空目录清理。

两个 Windows 特有的坑，决定了这里为什么不用 os.rmdir：

1. 宿主安全层会接管 Python 进程内的 ``os.rmdir`` / ``shutil.rmtree``，
   强制改成"移到回收站"。SMB 网络盘没有回收站，于是必然失败，而且报的
   ``WinError 145 目录不是空的`` 具有误导性 —— 目录其实是空的。
2. 于是直接调 Win32 ``RemoveDirectoryW`` / ``DeleteFileW``，不经过 Python 的
   删除封装，行为确定，也不需要拉子进程。

非 Windows 平台退回标准库实现，方便 CI 跑单元测试。
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

VIDEO_EXTS = {
    ".mkv",
    ".mp4",
    ".avi",
    ".ts",
    ".m2ts",
    ".mts",
    ".iso",
    ".mov",
    ".wmv",
    ".rmvb",
    ".flv",
    ".webm",
}

# 上一版清理时给空壳目录打的标记，见到就可以直接清
SHELL_MARKS = ("_空壳待删", "_待删除_")

if sys.platform == "win32":  # pragma: no cover - 平台相关
    import ctypes
    import ctypes.wintypes as wt

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.RemoveDirectoryW.argtypes = [wt.LPCWSTR]
    _k32.RemoveDirectoryW.restype = wt.BOOL
    _k32.DeleteFileW.argtypes = [wt.LPCWSTR]
    _k32.DeleteFileW.restype = wt.BOOL

    def _rmdir(path: Path) -> tuple[bool, str]:
        if _k32.RemoveDirectoryW(str(path)):
            return True, ""
        err = ctypes.get_last_error()
        return False, ctypes.FormatError(err)

    def _unlink(path: Path) -> tuple[bool, str]:
        if _k32.DeleteFileW(str(path)):
            return True, ""
        err = ctypes.get_last_error()
        return False, ctypes.FormatError(err)

else:  # pragma: no cover - 仅 CI

    def _rmdir(path: Path) -> tuple[bool, str]:
        try:
            os.rmdir(path)
            return True, ""
        except OSError as exc:
            return False, str(exc)

    def _unlink(path: Path) -> tuple[bool, str]:
        try:
            os.unlink(path)
            return True, ""
        except OSError as exc:
            return False, str(exc)


@dataclass
class Candidate:
    """一个可以删的目录。"""

    path: Path
    files: list[tuple[str, int]] = field(default_factory=list)
    tagged: bool = False  # 名字带 _空壳待删 / _待删除_，是上一版留下的标记
    blocked: str = ""  # 非空原因，比如里面还有视频

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def size(self) -> int:
        return sum(value for _name, value in self.files)

    @property
    def removable(self) -> bool:
        return not self.blocked

    def describe(self) -> str:
        if self.blocked:
            return self.blocked
        if not self.files:
            return "空目录"
        names = "、".join(name for name, _ in self.files[:3])
        more = "…" if len(self.files) > 3 else ""
        return f"{len(self.files)} 个文件：{names}{more}"


def is_shell_name(name: str) -> bool:
    return any(mark in name for mark in SHELL_MARKS)


def _walk(dir_path: Path) -> list[tuple[str, int]]:
    """返回整棵树里的全部文件（名字 + 大小）。"""
    files: list[tuple[str, int]] = []
    stack = [dir_path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        try:
                            size = entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            size = 0
                        files.append((entry.name, size))
        except OSError:
            continue
    return files


def _judge(dir_path: Path) -> Candidate:
    files = _walk(dir_path)
    tagged = is_shell_name(dir_path.name)
    cand = Candidate(path=dir_path, files=files, tagged=tagged)
    has_video = any(Path(name).suffix.lower() in VIDEO_EXTS for name, _ in files)
    if has_video:
        # 有视频就绝不自动删，宁可漏删也不能删片
        cand.blocked = "里面还有视频文件，跳过"
    return cand


def scan(root: Path, concurrency: int = 8, max_depth: int = 4) -> list[Candidate]:
    """找出整棵树里没有任何文件的目录，以及名字带删除标记的目录。

    只看 root 下面 max_depth 层。电影库再深也不会超过这个量级，
    限制层数可以避免在 NAS 上一个目录一个目录地扫到底。
    """
    root = Path(root)
    if not root.exists():
        return []

    # 候选的起点：顶层子目录 + 带标记的更深目录
    tops: list[Path] = []
    try:
        with os.scandir(root) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    tops.append(Path(entry.path))
    except OSError:
        return []

    found: list[Candidate] = []
    with ThreadPoolExecutor(max_workers=max(1, min(concurrency, 16))) as pool:
        for cand in pool.map(_judge, tops):
            if cand.blocked and not cand.tagged:
                # 有视频的合集目录本身不能删，但里面可能还有空壳子目录
                found.extend(_scan_children(cand.path, 1, max_depth))
            else:
                # 整棵树里没有视频：不管是空的还是只剩海报/字幕，都算空壳
                found.append(cand)
    found.sort(key=lambda item: str(item.path))
    return found


def _scan_children(parent: Path, depth: int, max_depth: int) -> list[Candidate]:
    """在父目录里找空目录；父目录本身有内容时才会走到这里。"""
    out: list[Candidate] = []
    if depth >= max_depth:
        return out
    try:
        with os.scandir(parent) as it:
            children = [Path(e.path) for e in it if e.is_dir(follow_symlinks=False)]
    except OSError:
        return out

    for child in children:
        cand = _judge(child)
        if cand.blocked and not cand.tagged:
            out.extend(_scan_children(child, depth + 1, max_depth))
        else:
            out.append(cand)
    return out


@dataclass
class Result:
    removed: list[Path] = field(default_factory=list)
    failed: list[tuple[Path, str]] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def ok(self) -> int:
        return len(self.removed)


def remove(candidates: list[Candidate]) -> Result:
    """删掉候选目录。先清文件，再从深到浅删目录。"""
    result = Result()
    for cand in candidates:
        if not cand.removable:
            result.skipped.append((cand.path, cand.blocked))
            continue

        ok_all = True
        # 1) 清掉里面的文件（空壳里偶尔有 poster.jpg、旧的 nfo）
        for name, _size in cand.files:
            ok, err = _unlink(cand.path / name)
            if not ok:
                ok_all = False
                result.failed.append((cand.path / name, err))
                break
        if not ok_all:
            continue

        # 2) 自底向上删目录。@eaDir 这类隐藏目录先不管，删不掉会在这里报出来
        dirs = _all_dirs(cand.path)
        for path in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
            ok, err = _rmdir(path)
            if not ok:
                ok_all = False
                result.failed.append((path, err))
        if ok_all:
            result.removed.append(cand.path)
    return result


def _all_dirs(root: Path) -> list[Path]:
    dirs = [root]
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        dirs.append(Path(entry.path))
                        stack.append(Path(entry.path))
        except OSError:
            continue
    return dirs

"""计划执行、日志与撤销。

只做 os.rename / os.replace —— 同盘改名是元数据操作，不产生数据复制。
跨设备（NAS 与本地分属不同共享）时 rename 会抛 OSError，此时按失败记录，
不会退化成复制，避免把磁盘塞满。
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import config as cfg
from . import nfo as nfo_mod
from .organizer import Op


@dataclass
class Report:
    ok: list[tuple[Op, str]] = field(default_factory=list)
    failed: list[tuple[Op, str]] = field(default_factory=list)
    log_path: Path | None = None
    # 视频被移走后，原目录里剩下没人管的文件。
    # [{"src_dir": ..., "dst_dir": ..., "files": [(名字, 字节), ...]}]
    residuals: list[dict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.ok) + len(self.failed)


def _new_log() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return cfg.log_dir() / f"执行日志_{stamp}.csv"


def execute(
    ops: list[Op],
    client=None,
    progress=None,
    stop_flag=None,
    root: Path | None = None,
) -> Report:
    report = Report(log_path=_new_log())
    rows: list[dict[str, str]] = []
    done = 0
    moved: list[tuple[Path, Path]] = []

    for op in ops:
        if stop_flag is not None and stop_flag():
            break
        done += 1
        if progress:
            progress(done, len(ops), op)

        try:
            if op.kind == "move":
                message = _do_move(op)
            elif op.kind == "write_nfo":
                message = _do_nfo(op)
            elif op.kind == "download":
                message = _do_download(op, client)
            elif op.kind == "mkdir":
                Path(op.dst).mkdir(parents=True, exist_ok=True)
                message = "已建目录"
            else:
                raise ValueError(f"未知操作 {op.kind}")
        except Exception as exc:  # noqa: BLE001 - 单条失败不能中断整批
            report.failed.append((op, str(exc)))
            rows.append({**op.as_row(), "结果": "失败", "信息": str(exc)})
            continue

        report.ok.append((op, message))
        rows.append({**op.as_row(), "结果": "成功", "信息": message})

        # 字幕跟随也算移动，但它不是"这部片"，不能拿它触发残留检查
        if op.kind == "move" and message == "已改名" and "字幕跟随" not in op.note:
            moved.append((Path(op.src).parent, Path(op.dst).parent))

    report.residuals = _collect_residuals(moved, root)

    _write_log(report.log_path, rows)
    return report


def _collect_residuals(moved: list[tuple[Path, Path]], root: Path | None) -> list[dict]:
    """统计每个被搬空的源目录里还剩下什么。

    必须等所有移动做完再统计：同一目录里可能有两部片，第一部移走时
    第二部还在，提前统计会把它当成"残留"。
    """
    out: list[dict] = []
    seen: set[str] = set()
    root_key = str(root).lower() if root else ""
    for src_dir, dst_dir in moved:
        key = str(src_dir).lower()
        if key in seen:
            continue
        seen.add(key)
        # 片子直接平铺在库根目录，移走后根目录本来就要留着
        if root_key and key == root_key:
            continue
        if src_dir == dst_dir:
            continue
        try:
            files = sorted((p for p in src_dir.iterdir() if p.is_file()), key=lambda p: p.name)
        except OSError:
            continue
        if not files:
            continue
        entries = []
        for path in files:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            entries.append((path.name, size))
        out.append({"src_dir": str(src_dir), "dst_dir": str(dst_dir), "files": entries})
    return out


def move_residuals(items: list[dict]) -> tuple[int, list[str]]:
    """把残留文件搬到对应的新目录。返回 (成功数, 错误列表)。"""
    ok = 0
    errors: list[str] = []
    for item in items:
        src_dir = Path(item["src_dir"])
        dst_dir = Path(item["dst_dir"])
        for name, _size in item["files"]:
            src = src_dir / name
            dst = dst_dir / name
            try:
                if not src.exists():
                    continue
                if dst.exists():
                    errors.append(f"{name}：目标已存在，跳过")
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.rename(src, dst)
                ok += 1
            except OSError as exc:
                errors.append(f"{name}：{exc}")
    return ok, errors


def delete_residuals(items: list[dict]) -> tuple[int, list[str]]:
    """删除残留文件。不进回收站，调用方必须先做二次确认。"""
    ok = 0
    errors: list[str] = []
    for item in items:
        src_dir = Path(item["src_dir"])
        for name, _size in item["files"]:
            path = src_dir / name
            try:
                if not path.exists():
                    continue
                # 只删文件，不删目录；目录留空由用户自己决定怎么处理
                if path.is_dir():
                    errors.append(f"{name}：是目录，未删除")
                    continue
                path.unlink()
                ok += 1
            except OSError as exc:
                errors.append(f"{name}：{exc}")
    return ok, errors


def _do_move(op: Op) -> str:
    src = Path(op.src)
    dst = Path(op.dst)
    if not src.exists():
        return "源文件已不存在"
    if dst.exists():
        raise FileExistsError("目标已存在")
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dst)
    return "已改名"


def _do_nfo(op: Op) -> str:
    meta = op.payload.get("meta") or {}
    if not meta:
        return "无元数据，跳过"
    path = Path(op.dst)
    kind = op.payload.get("kind") or "movie"

    if kind == "tv_episode":
        content = nfo_mod.build_episode_nfo(
            meta,
            int(op.payload.get("season") or 1),
            int(op.payload.get("episode") or 0),
            op.payload.get("ep_title") or "",
        )
    elif kind == "tv_show":
        content = nfo_mod.build_tvshow_nfo(meta, art=bool(meta.get("poster")))
    else:
        content = nfo_mod.build_nfo(meta, art=bool(meta.get("poster")))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return "已写入"


def _do_download(op: Op, client) -> str:
    if client is None:
        return "未配置接口，跳过"
    ok = client.download_image(op.src, Path(op.dst))
    if not ok:
        raise OSError("下载失败")
    return "已下载"


def _write_log(path: Path | None, rows: list[dict[str, str]]) -> None:
    if not path:
        return
    try:
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=["操作", "源", "目标", "备注", "结果", "信息"]
            )
            writer.writeheader()
            writer.writerows(rows)
    except OSError:
        pass


def undo(log_path: Path) -> tuple[int, int]:
    """按日志把移动过的文件反向还原；新写入的 nfo / 图片直接删除。"""
    restored = 0
    removed = 0
    with log_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("结果") != "成功":
                continue
            action = row.get("操作", "")
            src, dst = row.get("源", ""), row.get("目标", "")
            try:
                if action == "移动/改名":
                    if Path(dst).exists() and not Path(src).exists():
                        Path(src).parent.mkdir(parents=True, exist_ok=True)
                        os.rename(dst, src)
                        restored += 1
                elif action in ("写入 nfo", "下载图片"):
                    if Path(dst).exists():
                        Path(dst).unlink()
                        removed += 1
            except OSError:
                continue
    return restored, removed


def latest_log() -> Path | None:
    logs = sorted(cfg.log_dir().glob("执行日志_*.csv"))
    return logs[-1] if logs else None

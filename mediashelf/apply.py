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

    @property
    def total(self) -> int:
        return len(self.ok) + len(self.failed)


def _new_log() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return cfg.log_dir() / f"执行日志_{stamp}.csv"


def execute(ops: list[Op], client=None, progress=None, stop_flag=None) -> Report:
    report = Report(log_path=_new_log())
    rows: list[dict[str, str]] = []
    done = 0

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

    _write_log(report.log_path, rows)
    return report


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
    nfo_mod.write_nfo(path.parent, meta, name=path.name, art=bool(meta.get("poster")))
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

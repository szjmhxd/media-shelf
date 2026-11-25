"""本地联调：不开窗也能验证界面与核心的接线。不参与 CI。"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402

from mediashelf import config as cfg  # noqa: E402
from mediashelf.ui.dialogs import PreviewDialog, _merge_by_owner  # noqa: E402
from mediashelf.ui.main_window import MainWindow  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    os.environ["APPDATA"] = str(tmp / "appdata")
    os.environ["XDG_CONFIG_HOME"] = str(tmp / "appdata")

    ffmpeg = Path(cfg.find_ffprobe("")).with_name("ffmpeg.exe")
    folder = tmp / "阿凡达 (2009)"
    folder.mkdir()
    video = folder / "阿凡达.Avatar.2009.mkv"
    subprocess.run(
        [
            str(ffmpeg),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=1280x720:rate=24",
            "-t",
            "1",
            "-c:v",
            "libx264",
            str(video),
        ],
        check=True,
        capture_output=True,
    )

    app = QApplication([])
    window = MainWindow(cfg.Config())
    window.root = tmp
    window.scan()

    deadline = time.time() + 60
    while window.worker is not None and window.worker.isRunning() and time.time() < deadline:
        app.processEvents()
        time.sleep(0.05)
    app.processEvents()

    rows = window.table.rowCount()
    print("表格行数:", rows)
    assert rows == 1, "扫描后应有 1 行"

    window._apply_meta(0, {"id": 1, "cn": "阿凡达", "en": "Avatar", "year": "2009"}, "已匹配")
    preview = window.table.item(0, 8).text()
    print("预览新名:", preview)
    assert "阿凡达.Avatar.2009" in preview, preview

    # 两阶段扫描：行先出现（探测中），参数后回填
    status = window.table.item(0, 9).text()
    print("状态列:", status, "| 片源列:", window.table.item(0, 6).text())
    assert status != "探测中…", "扫描结束后不应还停在探测中"
    assert window.table.item(0, 6).text(), "探测完成后片源列应该有值"

    ops = window._build_plan()
    print("计划条目:", [(op.kind, Path(op.dst).name) for op in ops])
    assert any(op.kind == "move" for op in ops)
    assert any(op.kind == "write_nfo" for op in ops)
    assert all(op.owner for op in ops), "每个操作都要带归属文件，预览才能合并"

    # 执行计划预览：按文件合并 + 搜索过滤
    merged = _merge_by_owner(ops)
    print("合并后文件数:", len(merged), merged[0]["name"])
    assert len(merged) == 1, "一个文件应合并成一行"
    assert merged[0]["rename"] and merged[0]["nfo"]

    dialog = PreviewDialog(ops)
    assert dialog.table.rowCount() == 1
    print("预览表头:", [dialog.table.horizontalHeaderItem(i).text() for i in range(8)])
    dialog.search.setText("阿凡达")
    assert not dialog.table.isRowHidden(0)
    dialog.search.setText("不存在的片名")
    assert dialog.table.isRowHidden(0), "搜索应能过滤掉不匹配的行"
    dialog.deleteLater()

    window.close()
    print("界面接线正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

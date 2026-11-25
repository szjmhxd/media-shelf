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
        [str(ffmpeg), "-v", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=24",
         "-t", "1", "-c:v", "libx264", str(video)],
        check=True, capture_output=True,
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

    ops = window._build_plan()
    print("计划条目:", [(op.kind, Path(op.dst).name) for op in ops])
    assert any(op.kind == "move" for op in ops)
    assert any(op.kind == "write_nfo" for op in ops)

    window.close()
    print("界面接线正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""MediaShelf —— 影视库整理工具：改名、归档、nfo。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _load_theme() -> str:
    qss = Path(__file__).with_name("theme.qss")
    try:
        return qss.read_text(encoding="utf-8")
    except OSError:
        return ""


def main(argv: list[str] | None = None) -> int:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from .config import APP_NAME, APP_VERSION, Config
    from .ui.main_window import MainWindow, _demo_fill

    parser = argparse.ArgumentParser(prog="mediashelf")
    parser.add_argument("--demo", action="store_true", help="载入演示数据，不联网")
    parser.add_argument("--smoke", metavar="PNG", help="渲染窗口后截图退出，用于自检")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    args = parser.parse_args(argv)

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)

    style = _load_theme()
    if style:
        app.setStyleSheet(style)

    window = MainWindow(Config())
    window.show()

    if args.demo:
        _demo_fill(window)

    if args.smoke:
        target = Path(args.smoke)

        def _grab() -> None:
            window.grab().save(str(target))
            app.quit()

        QTimer.singleShot(1500, _grab)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

"""MediaShelf —— 影视库整理工具：改名、归档、nfo。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _load_theme() -> str:
    """样式表在 ui/ 子目录；打包后路径同样成立（PyInstaller 保留包结构）。"""
    qss = Path(__file__).resolve().parent / "ui" / "theme.qss"
    try:
        return qss.read_text(encoding="utf-8")
    except OSError:
        return ""


def _install_crash_handler() -> None:
    """窗口模式下没有控制台，未捕获异常必须落盘并弹窗，否则用户只看到闪退。"""

    def hook(exc_type, exc_value, exc_tb) -> None:
        import traceback

        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            from .config import config_dir

            log = config_dir() / "crash.log"
            log.write_text(detail, encoding="utf-8")
            where = str(log)
        except Exception:  # noqa: BLE001 - 兜底逻辑本身不能再抛
            where = "(无法写入日志)"

        sys.stderr.write(detail)
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            if QApplication.instance() is not None:
                QMessageBox.critical(
                    None,
                    "MediaShelf 出错了",
                    f"{exc_type.__name__}: {exc_value}\n\n详细信息已写入:\n{where}",
                )
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = hook


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
    _install_crash_handler()

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

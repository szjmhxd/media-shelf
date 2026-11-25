"""设置、手动匹配、计划预览三个对话框。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .. import config as cfg
from ..organizer import Op, plan_summary
from ..tmdb import TMDBClient, test_key


class SettingsDialog(QDialog):
    def __init__(self, config: cfg.Config, parent=None) -> None:
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("设置")
        self.setMinimumWidth(620)

        self.key_edit = QLineEdit(config.get("tmdb_key", ""))
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setPlaceholderText("v3 API Key（32 位）或 v4 Read Access Token（eyJ 开头）")
        show = QPushButton("显示")
        show.setCheckable(True)
        show.setFixedWidth(52)
        show.toggled.connect(
            lambda on: self.key_edit.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password)
        )
        key_row = QHBoxLayout()
        key_row.addWidget(self.key_edit, 1)
        key_row.addWidget(show)

        self.test_btn = QPushButton("测试连接")
        self.test_btn.clicked.connect(self._test)
        self.test_label = QLabel("")
        self.test_label.setWordWrap(True)
        self.test_label.setObjectName("hintLabel")

        self.ffprobe_edit = QLineEdit(config.get("ffprobe", ""))
        self.ffprobe_edit.setPlaceholderText("留空则自动查找 ffprobe")
        browse = QPushButton("浏览")
        browse.clicked.connect(self._browse)
        detect = QPushButton("自动检测")
        detect.clicked.connect(self._detect)
        probe_row = QHBoxLayout()
        probe_row.addWidget(self.ffprobe_edit, 1)
        probe_row.addWidget(browse)
        probe_row.addWidget(detect)

        self.lang_combo = QComboBox()
        for label, code in [
            ("简体中文", "zh-CN"),
            ("繁体中文", "zh-TW"),
            ("English", "en-US"),
            ("日本語", "ja-JP"),
        ]:
            self.lang_combo.addItem(label, code)
        current = config.get("language", "zh-CN")
        index = self.lang_combo.findData(current)
        self.lang_combo.setCurrentIndex(index if index >= 0 else 0)

        self.images_check = QCheckBox("写入 nfo 时同步下载 poster.jpg / fanart.jpg")
        self.images_check.setChecked(bool(config.get("download_images", True)))
        self.nfo_check = QCheckBox("整理时自动写入 nfo")
        self.nfo_check.setChecked(bool(config.get("write_nfo", True)))
        self.backup_check = QCheckBox("删除 nfo 前先备份到 _nfo_backup")
        self.backup_check.setChecked(bool(config.get("keep_nfo_backup", True)))

        form = QFormLayout()
        form.addRow("TMDB 凭据", key_row)
        form.addRow("", self.test_label)
        form.addRow("ffprobe", probe_row)
        form.addRow("元数据语言", self.lang_combo)
        form.addRow("", self.images_check)
        form.addRow("", self.nfo_check)
        form.addRow("", self.backup_check)

        tip = QLabel(
            "免费获取凭据：themoviedb.org → 设置 → API → 申请开发者 Key（约 1 分钟）。\n"
            "没有 Key 时，改名与整理仍可用，只是无法写入 nfo 元数据。"
        )
        tip.setObjectName("hintLabel")
        tip.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(tip)
        layout.addStretch(1)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 ffprobe", "", "可执行文件 (*.exe);;所有文件 (*)"
        )
        if path:
            self.ffprobe_edit.setText(path)

    def _detect(self) -> None:
        found = cfg.find_ffprobe()
        if found:
            self.ffprobe_edit.setText(found)
            self.test_label.setText(f"已找到：{found}")
        else:
            self.test_label.setText("没有找到 ffprobe，请手动指定")

    def _test(self) -> None:
        self.test_label.setText("正在连接…")
        self.test_btn.setEnabled(False)
        ok, message = test_key(self.key_edit.text().strip())
        self.test_btn.setEnabled(True)
        self.test_label.setText(("✓ " if ok else "✗ ") + message)

    def apply_to_config(self) -> None:
        self.config.update(
            {
                "tmdb_key": self.key_edit.text().strip(),
                "ffprobe": self.ffprobe_edit.text().strip(),
                "language": self.lang_combo.currentData(),
                "download_images": self.images_check.isChecked(),
                "write_nfo": self.nfo_check.isChecked(),
                "keep_nfo_backup": self.backup_check.isChecked(),
            }
        )
        self.config.save()


class MatchDialog(QDialog):
    """手动搜索：识别不准时自己挑一条。"""

    def __init__(self, client: TMDBClient | None, query: str, year: str, parent=None) -> None:
        super().__init__(parent)
        self.client = client
        self.meta: dict | None = None
        self.setWindowTitle("手动匹配")
        self.resize(720, 460)

        self.query_edit = QLineEdit(query)
        self.year_edit = QLineEdit(year)
        self.year_edit.setFixedWidth(80)
        self.year_edit.setPlaceholderText("年份")
        search_btn = QPushButton("搜索")
        search_btn.setDefault(True)
        search_btn.clicked.connect(self._search)

        row = QHBoxLayout()
        row.addWidget(QLabel("片名"))
        row.addWidget(self.query_edit, 1)
        row.addWidget(self.year_edit)
        row.addWidget(search_btn)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _: self._accept())
        self.list.setAlternatingRowColors(True)

        self.info = QLabel("输入片名后回车或点搜索")
        self.info.setObjectName("hintLabel")
        self.info.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("使用选中项")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.info)
        layout.addWidget(buttons)

        self.query_edit.returnPressed.connect(self._search)
        if query:
            self._search()

    def _search(self) -> None:
        if self.client is None:
            QMessageBox.warning(
                self, "缺少数据源", "请先在设置里填写 TMDB 凭据，或直接使用维基百科。"
            )
            return
        self.list.clear()
        self.info.setText("搜索中…")
        self.setCursor(Qt.WaitCursor)
        try:
            results = self.client.search(
                self.query_edit.text().strip(), self.year_edit.text().strip()
            )
        except Exception as exc:  # noqa: BLE001 - 数据源可能是维基百科
            self.info.setText(f"搜索失败：{exc}")
            return
        finally:
            self.unsetCursor()

        if not results:
            self.info.setText("没有结果，换个片名再试")
            return
        for item in results:
            text = f"{item['cn']}　({item['year']})　★{item['rating']}　|　{item['original']}"
            entry = QListWidgetItem(text)
            entry.setData(Qt.UserRole, item["id"])
            self.list.addItem(entry)
        self.list.setCurrentRow(0)
        self.info.setText(f"共 {len(results)} 条，双击可直接使用")

    def _accept(self) -> None:
        item = self.list.currentItem()
        if item is None or self.client is None:
            return
        tmdb_id = item.data(Qt.UserRole)
        self.setCursor(Qt.WaitCursor)
        try:
            self.meta = self.client.details(tmdb_id)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "获取详情失败", str(exc))
            return
        finally:
            self.unsetCursor()
        self.accept()


class PreviewDialog(QDialog):
    def __init__(self, ops: list[Op], parent=None) -> None:
        super().__init__(parent)
        self.ops = ops
        self.setWindowTitle("执行计划预览")
        self.resize(980, 560)

        summary = plan_summary(ops)
        label_text = (
            "　".join(f"{_KIND.get(k, k)} {v} 条" for k, v in summary.items())
            or "没有需要执行的操作"
        )
        label = QLabel(f"共 {len(ops)} 条操作：{label_text}")
        label.setObjectName("titleLabel")

        table = QTableWidget(len(ops), 4)
        table.setHorizontalHeaderLabels(["操作", "源", "目标", "备注"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setAlternatingRowColors(True)
        for row, op in enumerate(ops):
            values = [op.as_row()["操作"], op.src, op.dst, op.note]
            for col, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setToolTip(str(value))
                table.setItem(row, col, cell)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)

        hint = QLabel("执行时只做同盘改名，不复制数据。完成后可在日志目录按日志撤销。")
        hint.setObjectName("hintLabel")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("执行")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(label)
        layout.addWidget(table, 1)
        layout.addWidget(hint)
        layout.addWidget(buttons)


_KIND = {
    "move": "移动/改名",
    "write_nfo": "写入 nfo",
    "download": "下载图片",
    "mkdir": "新建目录",
}

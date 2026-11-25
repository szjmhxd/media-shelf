"""主窗口。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import apply as apply_mod
from .. import config as cfg
from .. import naming, organizer
from .. import nfo as nfo_mod
from ..tmdb import TMDBClient
from .dialogs import MatchDialog, PreviewDialog, SettingsDialog
from .workers import ApplyWorker, IdentifyWorker, ScanWorker

COLUMNS = ["", "文件", "大小", "中文名", "英文名", "年份", "片源", "发布组", "新文件名", "状态"]
COL_CHECK, COL_FILE, COL_SIZE, COL_CN, COL_EN, COL_YEAR, COL_SRC, COL_GROUP, COL_NEW, COL_STATUS = (
    range(10)
)

EDIT_FIELDS = {
    COL_CN: ("meta", "cn"),
    COL_EN: ("meta", "en"),
    COL_YEAR: ("meta", "year"),
    COL_SRC: (None, "source"),
    COL_GROUP: (None, "group"),
}

STATUS_COLORS = {
    "已匹配": "#1a7f37",
    "手动指定": "#0969da",
    "需人工确认": "#bc4c00",
    "无搜索结果": "#cf222e",
    "无匹配": "#cf222e",
    "无匹配：文件名里没有片名": "#cf222e",
    "网络错误": "#cf222e",
    "Key 无效": "#cf222e",
    "识别出错": "#cf222e",
    "待识别": "#6e7781",
}


class MainWindow(QMainWindow):
    def __init__(self, config: cfg.Config) -> None:
        super().__init__()
        self.config = config
        self.records: list[dict] = []
        self.root: Path | None = Path(config.get("last_dir")) if config.get("last_dir") else None
        self.worker = None
        self._loading = False
        self._client: TMDBClient | None = None

        self.setWindowTitle("MediaShelf · 影视库整理")
        self.resize(1360, 820)
        self._build_ui()
        self._refresh_key_state()

    # ------------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(14, 12, 14, 10)
        layout.setSpacing(8)

        # 第一行：目录与主要动作
        self.dir_label = QLabel("未选择目录")
        self.dir_label.setObjectName("pathLabel")
        self.dir_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        pick_btn = QPushButton("选择目录")
        pick_btn.clicked.connect(self.pick_dir)
        self.scan_btn = QPushButton("扫描")
        self.scan_btn.clicked.connect(self.scan)
        self.identify_btn = QPushButton("联网识别")
        self.identify_btn.clicked.connect(self.identify)
        self.settings_btn = QPushButton("设置")
        self.settings_btn.clicked.connect(self.open_settings)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("媒体目录"))
        row1.addWidget(self.dir_label, 1)
        for btn in (pick_btn, self.scan_btn, self.identify_btn, self.settings_btn):
            row1.addWidget(btn)
        layout.addLayout(row1)

        # 第二行：命名模板
        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(190)
        for name, template, _ in cfg.PRESETS:
            self.preset_combo.addItem(name, template)
        self._sync_preset()
        self.preset_combo.currentIndexChanged.connect(self._apply_preset)

        self.template_edit = QLineEdit(self.config.get("template"))
        self.template_edit.setFont(QFont("Consolas", 10))
        self.template_edit.textChanged.connect(self.refresh_all_previews)

        self.field_combo = QComboBox()
        self.field_combo.addItem("插入字段…", "")
        for token, desc, _example in naming.FIELD_DOCS:
            self.field_combo.addItem(f"{token}  {desc}", token)
        self.field_combo.currentIndexChanged.connect(self._insert_field)

        reset_btn = QPushButton("恢复默认")
        reset_btn.setFixedWidth(88)
        reset_btn.clicked.connect(lambda: self.template_edit.setText(cfg.DEFAULT_TEMPLATE))

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("命名规则"))
        row2.addWidget(self.preset_combo)
        row2.addWidget(self.template_edit, 1)
        row2.addWidget(self.field_combo)
        row2.addWidget(reset_btn)
        layout.addLayout(row2)

        # 表格
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_CHECK, QHeaderView.Fixed)
        self.table.setColumnWidth(COL_CHECK, 34)
        header.setSectionResizeMode(COL_FILE, QHeaderView.Interactive)
        self.table.setColumnWidth(COL_FILE, 168)
        header.setSectionResizeMode(COL_SIZE, QHeaderView.ResizeToContents)
        for col, width in ((COL_CN, 104), (COL_EN, 118)):
            header.setSectionResizeMode(col, QHeaderView.Fixed)
            self.table.setColumnWidth(col, width)
        for col in (COL_YEAR, COL_SRC, COL_GROUP):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        # 新文件名是主操作列，给足宽度；其余列压缩，避免 125% 缩放下被截断
        header.setSectionResizeMode(COL_NEW, QHeaderView.Stretch)
        self.table.setMinimumWidth(720)
        header.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeToContents)
        self.table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.table, 1)

        # 选项
        self.organize_check = QCheckBox("整理到子目录")
        self.organize_check.setChecked(True)
        self.collection_check = QCheckBox("系列电影归入 Collection")
        self.collection_check.setChecked(bool(self.config.get("group_collections", True)))
        self.nfo_check = QCheckBox("写入 nfo")
        self.nfo_check.setChecked(bool(self.config.get("write_nfo", True)))
        self.image_check = QCheckBox("下载海报")
        self.image_check.setChecked(bool(self.config.get("download_images", True)))
        self.organize_check.toggled.connect(self.refresh_all_previews)

        self.field_summary = QLabel("")
        self.field_summary.setObjectName("hintLabel")

        row3 = QHBoxLayout()
        for box in (self.organize_check, self.collection_check, self.nfo_check, self.image_check):
            row3.addWidget(box)
        row3.addStretch(1)
        row3.addWidget(self.field_summary)
        layout.addLayout(row3)

        # 动作
        self.preview_btn = QPushButton("预览并执行")
        self.preview_btn.setObjectName("primaryButton")
        self.preview_btn.clicked.connect(self.preview_and_apply)
        self.match_btn = QPushButton("手动匹配")
        self.match_btn.clicked.connect(self.manual_match)
        self.nfo_btn = QPushButton("仅生成 NFO")
        self.nfo_btn.clicked.connect(self.only_nfo)
        self.del_nfo_btn = QPushButton("删除 NFO")
        self.del_nfo_btn.clicked.connect(self.delete_nfo)
        self.undo_btn = QPushButton("撤销上次")
        self.undo_btn.clicked.connect(self.undo_last)
        check_all = QPushButton("全选/全不选")
        check_all.clicked.connect(self.toggle_all)

        row4 = QHBoxLayout()
        for btn in (self.preview_btn, self.match_btn, self.nfo_btn, check_all):
            row4.addWidget(btn)
        row4.addStretch(1)
        for btn in (self.del_nfo_btn, self.undo_btn):
            row4.addWidget(btn)
        layout.addLayout(row4)

        # 进度与日志
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setFixedHeight(6)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setFixedHeight(118)
        self.log_view.setFont(QFont("Consolas", 9))
        layout.addWidget(self.log_view)

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self._status("就绪")

        menu = self.menuBar().addMenu("工具")
        act_log = QAction("打开日志目录", self)
        act_log.triggered.connect(self.open_log_dir)
        menu.addAction(act_log)
        act_cfg = QAction("打开配置目录", self)
        act_cfg.triggered.connect(self.open_config_dir)
        menu.addAction(act_cfg)

        if self.root:
            self.dir_label.setText(str(self.root))

    # ------------------------------------------------------------------ 状态
    def _status(self, text: str) -> None:
        self.statusBar().showMessage(text)

    def log(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{stamp}] {text}")

    def _refresh_key_state(self) -> None:
        key = self.config.get("tmdb_key", "")
        self.identify_btn.setEnabled(True)
        self.identify_btn.setToolTip(
            "使用 TMDB 抓取完整元数据"
            if key
            else "未配置 TMDB，使用 Wikidata 兜底（只有中英文名/年份）"
        )
        ffprobe = cfg.find_ffprobe(self.config.get("ffprobe", ""))
        self.field_summary.setText(
            f"ffprobe: {'已找到' if ffprobe else '未找到'}　|　数据源: {'TMDB' if key else 'Wikidata（免 Key，命中率低）'}"
        )

    # ------------------------------------------------------------------ 目录
    def pick_dir(self) -> None:
        start = str(self.root) if self.root else ""
        path = QFileDialog.getExistingDirectory(self, "选择媒体目录", start)
        if path:
            self.root = Path(path)
            self.dir_label.setText(path)
            self.config.set("last_dir", path)
            self.config.save()
            self.log(f"媒体目录：{path}")

    # ------------------------------------------------------------------ 扫描
    def scan(self) -> None:
        if self._guard_busy("扫描"):
            return
        if not self.root or not self.root.exists():
            QMessageBox.warning(self, "没有目录", "先选择一个媒体目录。")
            return
        ffprobe = cfg.find_ffprobe(self.config.get("ffprobe", ""))
        if not ffprobe:
            QMessageBox.warning(
                self,
                "缺少 ffprobe",
                "没有找到 ffprobe，请到设置里指定路径。\nWindows 可用 winget install Gyan.FFmpeg",
            )
            return

        self.records.clear()
        self.table.setRowCount(0)
        self.log(f"开始扫描 {self.root}")
        self.progress.setRange(0, 0)

        self.worker = ScanWorker(
            self.root,
            ffprobe,
            recursive=True,
            workers=int(self.config.get("probe_workers", 4) or 4),
        )
        self.worker.row_ready.connect(self._add_record)
        self.worker.progress.connect(self._on_progress)
        self.worker.done.connect(self._scan_done)
        self.worker.start()

    def _add_record(self, record: dict) -> None:
        self.records.append(record)
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._loading = True
        check = QTableWidgetItem()
        check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        check.setCheckState(Qt.Checked)
        self.table.setItem(row, COL_CHECK, check)

        name_item = QTableWidgetItem(Path(record["path"]).name)
        name_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        name_item.setToolTip(record["path"])
        self.table.setItem(row, COL_FILE, name_item)

        size_item = QTableWidgetItem(_human_size(record["size"]))
        size_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        size_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.table.setItem(row, COL_SIZE, size_item)

        for col in (COL_CN, COL_EN, COL_YEAR, COL_SRC, COL_GROUP):
            item = QTableWidgetItem("")
            self.table.setItem(row, col, item)
        self.table.item(row, COL_SRC).setText(record.get("source", ""))
        self.table.item(row, COL_GROUP).setText(record.get("group", ""))
        self.table.item(row, COL_YEAR).setText("")

        new_item = QTableWidgetItem("")
        new_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        self.table.setItem(row, COL_NEW, new_item)

        status = QTableWidgetItem(record.get("status", "待识别"))
        status.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        self.table.setItem(row, COL_STATUS, status)
        self._loading = False

        self.refresh_preview(row)

    def _scan_done(self, count: int) -> None:
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.log(f"扫描完成，共 {count} 个视频文件")
        self._status(f"扫描完成：{count} 个文件")
        self.refresh_all_previews()

    def _on_progress(self, done: int, total: int, name: str) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)
        self.progress.setFormat(f"{done}/{total}")
        self._status(f"{done}/{total}  {name}")

    # ------------------------------------------------------------------ 识别
    def _ensure_client(self):
        key = self.config.get("tmdb_key", "")
        if key:
            if self._client is None or self._client.api_key != key:
                self._client = TMDBClient(key, self.config.get("language", "zh-CN"))
            return self._client

        # 没有 Key 时用 Wikidata 兜底，至少能拿到中英文名与年份
        from ..wiki import WikiProvider

        if self._client is None or not isinstance(self._client, WikiProvider):
            self._client = WikiProvider(self.config.get("language", "zh-CN"))
            self.log("未配置 TMDB，改用 Wikidata（只有中英文名和年份，命中率明显低于 TMDB）")
        return self._client

    def identify(self) -> None:
        if self._guard_busy("识别"):
            return
        client = self._ensure_client()
        if client is None:
            return
        if not self.records:
            QMessageBox.information(self, "没有数据", "先扫描目录。")
            return

        self.log("开始联网识别…")
        self.identify_btn.setEnabled(False)
        self.progress.setRange(0, len(self.records))
        self.worker = IdentifyWorker(client, self.records)
        self.worker.progress.connect(self._on_progress)
        self.worker.row_updated.connect(self._apply_meta)
        self.worker.failed.connect(lambda msg: self.log(f"识别失败：{msg}"))
        self.worker.done.connect(self._identify_done)
        self.worker.start()

    def _apply_meta(self, index: int, meta: dict, status: str) -> None:
        if index >= len(self.records):
            return
        record = self.records[index]
        if meta.get("id"):
            record["meta"] = meta
        record["status"] = status
        if meta.get("query"):
            record["query"] = meta["query"]

        # 失败重跑时不要用空值覆盖已填好的内容
        has_name = bool(meta.get("cn") or meta.get("en"))
        self._loading = True
        if has_name:
            cn_item = self.table.item(index, COL_CN)
            cn_item.setText(meta.get("cn", ""))
            cn_item.setToolTip(meta.get("overview") or meta.get("cn", ""))
            en_item = self.table.item(index, COL_EN)
            en_item.setText(meta.get("en") or meta.get("original", ""))
            en_item.setToolTip(meta.get("en") or meta.get("original", ""))
            self.table.item(index, COL_YEAR).setText(str(meta.get("year") or ""))
        status_item = self.table.item(index, COL_STATUS)
        status_item.setText(status)
        color = STATUS_COLORS.get(status)
        if color:
            status_item.setForeground(QColor(color))
        self._loading = False
        self.refresh_preview(index)

    def _identify_done(self, matched: int, pending: int) -> None:
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.identify_btn.setEnabled(True)
        self.log(f"识别完成：已匹配 {matched} 部，未匹配 {pending} 部")
        self._status(f"识别完成：{matched} 部匹配，{pending} 部未匹配")
        self.refresh_all_previews()

        # 一部都没匹配上，几乎一定是数据源的问题，而不是片名的问题
        if matched == 0 and pending:
            has_key = bool(self.config.get("tmdb_key", ""))
            if not has_key:
                self.log(
                    "提示：未配置 TMDB 时只能用 Wikidata，中文片名命中率很低。"
                    "到 themoviedb.org 申请免费 Key 填进设置，识别质量会正常。"
                )
                QMessageBox.warning(
                    self,
                    "一部都没匹配上",
                    "当前用的是 Wikidata 兜底，它的中文片名覆盖率很低，大部分片子查不到。\n\n"
                    "解决办法：到 themoviedb.org 注册后申请免费 API Key（设置 → API），"
                    "填进本程序的「设置」里，再点一次联网识别。\n\n"
                    "不改名也能继续用：现在点「预览并执行」会按文件名解析出的片名和年份改名，"
                    "只是没有英文名和 nfo。",
                )
            else:
                self.log("提示：检查 TMDB Key 是否有效，或网络是否需要代理。")

    # ------------------------------------------------------------------ 表格
    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading:
            return
        row, col = item.row(), item.column()
        if row >= len(self.records):
            return
        record = self.records[row]

        if col == COL_CHECK:
            record["checked"] = item.checkState() == Qt.Checked
            return

        if col in EDIT_FIELDS:
            container, key = EDIT_FIELDS[col]
            value = item.text().strip()
            if container:
                record.setdefault(container, {})[key] = value
            else:
                record[key] = value
            if col in (COL_CN, COL_EN, COL_YEAR):
                record["status"] = "手动指定"
                status_item = self.table.item(row, COL_STATUS)
                self._loading = True
                status_item.setText("手动指定")
                status_item.setForeground(QColor(STATUS_COLORS["手动指定"]))
                self._loading = False
            self.refresh_preview(row)

    def refresh_preview(self, row: int) -> None:
        if row >= len(self.records):
            return
        record = self.records[row]
        try:
            name = naming.render_filename(record, self.template_edit.text())
        except Exception:  # noqa: BLE001 - 模板写错时不能影响界面
            name = ""
        shown = name
        if self.organize_check.isChecked():
            folder = naming.render_folder(
                record, self.config.get("folder_template", cfg.DEFAULT_FOLDER_TEMPLATE)
            )
            shown = f"{folder}/{name}"
        self._loading = True
        new_item = self.table.item(row, COL_NEW)
        new_item.setText(shown)
        new_item.setToolTip(shown)
        self._loading = False

    def refresh_all_previews(self) -> None:
        for row in range(min(self.table.rowCount(), len(self.records))):
            self.refresh_preview(row)

    def _insert_field(self, index: int) -> None:
        token = self.field_combo.itemData(index)
        if not token:
            return
        self.template_edit.insert(token)
        self.template_edit.setFocus()
        self.field_combo.setCurrentIndex(0)

    def _sync_preset(self) -> None:
        current = self.config.get("template")
        index = self.preset_combo.findData(current)
        if index < 0:
            self.preset_combo.insertItem(0, "自定义", "")
            index = 0
        self.preset_combo.blockSignals(True)
        self.preset_combo.setCurrentIndex(index)
        self.preset_combo.blockSignals(False)

    def _apply_preset(self, index: int) -> None:
        template = self.preset_combo.itemData(index)
        if template:
            self.template_edit.setText(template)

    def toggle_all(self) -> None:
        rows = range(self.table.rowCount())
        any_off = any(self.table.item(row, COL_CHECK).checkState() != Qt.Checked for row in rows)
        self._loading = True
        for row in rows:
            self.table.item(row, COL_CHECK).setCheckState(Qt.Checked if any_off else Qt.Unchecked)
            if row < len(self.records):
                self.records[row]["checked"] = any_off
        self._loading = False

    # ------------------------------------------------------------------ 匹配
    def manual_match(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        if not rows:
            QMessageBox.information(self, "未选择", "先在表格里选中要匹配的行。")
            return
        client = self._ensure_client()
        if client is None:
            return

        record = self.records[rows[0]]
        query, year = naming.guess_query(record["path"])
        dialog = MatchDialog(client, record.get("query", query), year, self)
        if dialog.exec() != MatchDialog.Accepted or not dialog.meta:
            return

        for row in rows:
            target = self.records[row]
            meta = dict(dialog.meta)
            target["meta"] = meta
            target["status"] = "手动指定"
            self._loading = True
            self.table.item(row, COL_CN).setText(meta.get("cn", ""))
            self.table.item(row, COL_EN).setText(meta.get("en") or meta.get("original", ""))
            self.table.item(row, COL_YEAR).setText(str(meta.get("year", "")))
            item = self.table.item(row, COL_STATUS)
            item.setText("手动指定")
            item.setForeground(QColor(STATUS_COLORS["手动指定"]))
            self._loading = False
            self.refresh_preview(row)
        self.log(f"手动匹配：{dialog.meta.get('cn')} ({dialog.meta.get('year')})")

    # ------------------------------------------------------------------ 执行
    def _build_plan(self, only_nfo: bool = False) -> list[organizer.Op]:
        if not self.root:
            return []
        return organizer.build_plan(
            self.records,
            self.root,
            template=self.template_edit.text(),
            folder_template=self.config.get("folder_template", cfg.DEFAULT_FOLDER_TEMPLATE),
            organize=self.organize_check.isChecked() and not only_nfo,
            group_collections=self.collection_check.isChecked(),
            collection_suffix=self.config.get("collection_suffix", "Collection"),
            write_nfo=self.nfo_check.isChecked(),
            download_images=self.image_check.isChecked(),
        )

    def _busy(self) -> bool:
        return bool(self.worker is not None and self.worker.isRunning())

    def _guard_busy(self, action: str) -> bool:
        """正在跑后台任务时不许再叠一个，否则计划基于半截数据生成。"""
        if self._busy():
            QMessageBox.information(self, "还在处理", f"{action}尚未结束，等进度条走完再操作。")
            return True
        return False

    def preview_and_apply(self) -> None:
        if self._guard_busy("识别"):
            return
        if not self.records:
            QMessageBox.information(self, "没有数据", "先扫描目录。")
            return
        ops = self._build_plan()
        if not ops:
            QMessageBox.information(
                self,
                "没有可执行的操作",
                "没有生成任何操作，通常是这三种原因：\n\n"
                "1. 没有勾选任何行（表格最左侧的复选框）\n"
                "2. 目标文件名和现有文件名完全一样，没有可改的\n"
                "3. 目标重名，被跳过了\n\n"
                "看表格「状态」列能确认每行的具体情况。",
            )
            return
        dialog = PreviewDialog(ops, self)
        if dialog.exec() != PreviewDialog.Accepted:
            return
        self._run_ops(ops)

    def only_nfo(self) -> None:
        if self._guard_busy("识别"):
            return
        if not self.records:
            QMessageBox.information(self, "没有数据", "先扫描并识别。")
            return
        self.config.set("write_nfo", True)
        ops = [op for op in self._build_plan(only_nfo=True) if op.kind in ("write_nfo", "download")]
        if not ops:
            QMessageBox.information(
                self,
                "没有可写入的内容",
                "nfo 必须有中英文名等元数据才能生成。\n\n"
                "先完成「联网识别」，或直接在表格里手工填中文名、英文名、年份，再试一次。",
            )
            return
        if (
            QMessageBox.question(self, "写入 NFO", f"将为 {len(ops)} 项写入 nfo / 图片，继续？")
            != QMessageBox.Yes
        ):
            return
        self._run_ops(ops)

    def _run_ops(self, ops) -> None:
        client = self._client if any(op.kind == "download" for op in ops) else None
        self.progress.setRange(0, len(ops))
        self.worker = ApplyWorker(ops, client)
        self.worker.progress.connect(self._on_progress)
        self.worker.done.connect(self._apply_done)
        self.worker.start()

    def _apply_done(self, report) -> None:
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.log(f"执行完成：成功 {len(report.ok)} 条，失败 {len(report.failed)} 条")
        if report.log_path:
            self.log(f"日志：{report.log_path}")
        for op, error in report.failed[:10]:
            self.log(f"  失败 {op.dst or op.src}：{error}")
        QMessageBox.information(
            self,
            "执行结果",
            f"成功 {len(report.ok)} 条，失败 {len(report.failed)} 条。\n日志已保存到：\n{report.log_path}",
        )
        if report.ok:
            self.scan()

    def delete_nfo(self) -> None:
        if not self.root:
            QMessageBox.warning(self, "没有目录", "先选择媒体目录。")
            return
        files = nfo_mod.find_nfos(self.root)
        if not files:
            QMessageBox.information(self, "没有 nfo", "该目录下没有 nfo 文件。")
            return
        backup = self.config.get("keep_nfo_backup", True)
        text = f"找到 {len(files)} 个 nfo 文件，确定删除？"
        text += "\n会先备份到 _nfo_backup 目录。" if backup else "\n不备份，直接删除。"
        if QMessageBox.question(self, "删除 NFO", text) != QMessageBox.Yes:
            return
        target = self.root / "_nfo_backup" if backup else None
        deleted, backed = nfo_mod.delete_nfos(files, target)
        self.log(f"删除 nfo：{deleted} 个，备份 {backed} 个")
        QMessageBox.information(self, "完成", f"已删除 {deleted} 个 nfo，备份 {backed} 个。")

    def undo_last(self) -> None:
        log = apply_mod.latest_log()
        if log is None:
            QMessageBox.information(self, "没有日志", "还没有执行记录。")
            return
        if (
            QMessageBox.question(
                self,
                "撤销",
                f"按 {log.name} 反向还原？\n已改名的文件会移回原位置，新写入的 nfo 会被删除。",
            )
            != QMessageBox.Yes
        ):
            return
        restored, removed = apply_mod.undo(log)
        self.log(f"撤销：还原 {restored} 个文件，删除 {removed} 个生成文件")
        QMessageBox.information(self, "撤销完成", f"还原 {restored} 个，删除 {removed} 个。")

    # ------------------------------------------------------------------ 杂项
    def open_settings(self) -> None:
        dialog = SettingsDialog(self.config, self)
        if dialog.exec() == SettingsDialog.Accepted:
            dialog.apply_to_config()
            self._client = None
            self._refresh_key_state()
            self.log("设置已保存")

    def open_log_dir(self) -> None:
        _open_folder(cfg.log_dir())

    def open_config_dir(self) -> None:
        _open_folder(cfg.config_dir())


def _open_folder(path: Path) -> None:
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def _human_size(size: int) -> str:
    if size <= 0:
        return "-"
    gb = size / 1024**3
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{size / 1024**2:.0f} MB"


def _demo_fill(window: MainWindow) -> None:
    """截图/联调用的假数据，不联网。"""
    from ..probe import AudioTrack, ProbeResult

    samples = [
        (
            "阿凡达 (2009).mkv",
            "阿凡达",
            "Avatar",
            "2009",
            "2160p",
            "REMUX",
            "DV.HDR",
            "TrueHD.7.1.Atmos",
            "DreamHD",
        ),
        (
            "F1：狂飙飞车 (2025).mkv",
            "F1：狂飙飞车",
            "F1: The Movie",
            "2025",
            "2160p",
            "REMUX",
            "DV.HDR",
            "TrueHD.7.1.Atmos",
            "Maybe",
        ),
        (
            "夺宝奇兵1：法柜奇兵 (1981).mkv",
            "夺宝奇兵1：法柜奇兵",
            "Indiana Jones and the Raiders of the Lost Ark",
            "1981",
            "2160p",
            "REMUX",
            "DV.HDR",
            "DTS-HD.MA.7.1",
            "老K",
        ),
        (
            "沙丘2 (2024).mp4",
            "沙丘2",
            "Dune: Part Two",
            "2024",
            "1080p",
            "HDRip",
            "",
            "AAC.2.0",
            "",
        ),
        (
            "出门在外 (2024).mkv",
            "出门在外",
            "Give Me a Ride",
            "2024",
            "2160p",
            "WEB-DL",
            "",
            "DTS.5.1",
            "",
        ),
        (
            "春娇救志明 (2017).mkv",
            "春娇救志明",
            "Love Off the Cuff",
            "2017",
            "1080p",
            "BluRay",
            "",
            "AC3.5.1",
            "MOMOHD",
        ),
    ]
    window.root = Path("N:/电影")
    window.dir_label.setText("N:\\电影")
    for index, (name, cn, en, year, res, src, hdr, _audio, group) in enumerate(samples):
        probe = ProbeResult(path=name)
        probe.width = 3840 if res == "2160p" else 1920
        probe.height = 2160 if res == "2160p" else 1080
        probe.vcodec = "hevc"
        probe.pix_fmt = "yuv420p10le"
        probe.hdr = hdr
        probe.size = (66 - index * 9) * 1024**3
        probe.bitrate = 60_000_000 - index * 3_000_000
        probe.audio = [AudioTrack("truehd", 8, title="TrueHD Atmos 7.1")]
        record = {
            "path": f"N:/电影/{name}",
            "name": name,
            "size": probe.size,
            "probe": probe,
            "meta": {
                "id": 19995 + index,
                "cn": cn,
                "en": en,
                "year": year,
                "rating": 8.2,
                "collection": "阿凡达" if index == 0 else "",
            },
            "source": src,
            "group": group,
            "checked": True,
            "status": "已匹配" if index != 3 else "需人工确认",
            "target": "",
        }
        window._add_record(record)
        window._apply_meta(index, record["meta"], record["status"])
    window.log("演示数据已载入（未连接 TMDB）")

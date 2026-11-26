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
    QSizePolicy,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import apply as apply_mod
from .. import cleanup as cleanup_mod
from .. import config as cfg
from .. import naming, organizer
from .. import nfo as nfo_mod
from ..tmdb import TMDBClient
from .dialogs import MatchDialog, PreviewDialog, ResidualDialog, SettingsDialog, ShellDialog
from .workers import (
    ApplyWorker,
    CleanApplyWorker,
    CleanScanWorker,
    IdentifyWorker,
    KeyCheckWorker,
    ScanWorker,
)

COLUMNS = [
    "",
    "文件",
    "大小",
    "中文名",
    "英文名",
    "年份",
    "片源",
    "发布组",
    "新文件名",
    "状态",
    "类型",
]
(
    COL_CHECK,
    COL_FILE,
    COL_SIZE,
    COL_CN,
    COL_EN,
    COL_YEAR,
    COL_SRC,
    COL_GROUP,
    COL_NEW,
    COL_STATUS,
    COL_TYPE,
) = range(11)

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
    "探测中…": "#6e7781",
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
        self._key_state = ""
        self._key_error = ""

        self.setWindowTitle("MediaShelf · 影视库整理")
        self.resize(1360, 820)
        self._build_ui()
        self._verify_key()

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

        # 凭据状态就放在「设置」左边：一眼能看到能不能联网识别，点一下直接进设置
        self.key_badge = QPushButton("● TMDB 检测中")
        self.key_badge.setCursor(Qt.PointingHandCursor)
        self.key_badge.setFlat(True)
        self.key_badge.clicked.connect(self.open_settings)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("媒体目录"))
        row1.addWidget(self.dir_label, 1)
        for btn in (pick_btn, self.scan_btn, self.identify_btn, self.key_badge, self.settings_btn):
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
        self.table.setColumnWidth(COL_FILE, 200)
        header.setSectionResizeMode(COL_SIZE, QHeaderView.ResizeToContents)
        for col in (COL_YEAR, COL_SRC, COL_GROUP):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        # 中英文名和新文件名平分剩余宽度：固定死宽度会在 125% 缩放或窄窗口下
        # 把"新文件名"挤成一条缝，正好是最需要看清的一列。
        for col in (COL_CN, COL_EN, COL_NEW):
            header.setSectionResizeMode(col, QHeaderView.Stretch)
        self.table.setMinimumWidth(720)
        header.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_TYPE, QHeaderView.ResizeToContents)
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
        # 数据源状态里可能带错误原因，长文本会把整行撑宽，进而把窗口拉变形。
        # Ignored 让标签不参与最小宽度计算，超出部分裁掉，全文放 tooltip。
        self.field_summary.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

        # 状态筛选：库一上千行，手工滚着找「无匹配」不现实，按状态一键过滤。
        # 只隐藏显示行，不影响勾选状态和执行范围。
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(["全部状态", "无匹配", "需人工确认", "已匹配"])
        self.filter_combo.setToolTip(
            "按识别状态筛选表格显示的行。\n不影响勾选，被隐藏的行仍按勾选状态参与执行。"
        )
        self.filter_combo.currentIndexChanged.connect(self._apply_filter)

        row3 = QHBoxLayout()
        for box in (self.organize_check, self.collection_check, self.nfo_check, self.image_check):
            row3.addWidget(box)
        row3.addStretch(1)
        row3.addWidget(QLabel("筛选"))
        row3.addWidget(self.filter_combo)
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
        self.clean_btn = QPushButton("清理空目录")
        self.clean_btn.setToolTip("删掉整理后留下的空文件夹和「空壳待删」目录，不碰正片")
        self.clean_btn.clicked.connect(self.clean_shells)

        row4 = QHBoxLayout()
        for btn in (self.preview_btn, self.match_btn, self.nfo_btn, check_all, self.clean_btn):
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
        # 长 URL 在 Qt 里不会自动断行（只在空白处折），会把整个窗口的
        # 最小宽度顶到屏幕外。关掉自动换行、关掉横向滚动条，写入时自己折。
        self.log_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.log_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.log_view.setMinimumWidth(120)
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
        lines = _wrap_text(str(text))
        self.log_view.appendPlainText(f"[{stamp}] {lines[0]}")
        for extra in lines[1:]:
            self.log_view.appendPlainText(f"{' ' * 11}{extra}")

    def _refresh_key_state(self) -> None:
        key = self.config.get("tmdb_key", "").strip()
        self.identify_btn.setEnabled(True)
        self.identify_btn.setToolTip(
            "使用 TMDB 抓取完整元数据"
            if key
            else "未配置 TMDB，使用 Wikidata 兜底（只有中英文名/年份）"
        )

        # 工具栏徽标：这是给用户看的主状态
        if not key:
            badge, color = "● TMDB 未配置", "#6e7781"
            tip = "没有凭据，联网识别会退回 Wikidata（只有中英文名和年份）。点这里配置。"
        elif self._key_state == "ok":
            badge, color = "● TMDB 已连接", "#1a7f37"
            tip = "凭据验证通过，可正常抓取元数据"
        elif self._key_state == "fail":
            badge, color = "● TMDB 不可用", "#cf222e"
            tip = self._key_error or "凭据验证失败，点这里重新测试"
        elif self._key_state == "checking":
            badge, color = "● TMDB 验证中", "#9a6700"
            tip = "正在连接 TMDB…"
        else:
            badge, color = "● TMDB 未验证", "#6e7781"
            tip = "点这里打开设置并测试凭据"
        self.key_badge.setText(badge)
        self.key_badge.setStyleSheet(
            f"QPushButton {{ color: {color}; border: none; background: transparent;"
            " padding: 2px 6px; }"
            f"QPushButton:hover {{ color: {color}; text-decoration: underline; }}"
        )
        self.key_badge.setToolTip(_wrap_text(tip)[0])

        ffprobe = cfg.find_ffprobe(self.config.get("ffprobe", ""))
        summary = f"ffprobe: {'已找到' if ffprobe else '未找到'}"
        if not ffprobe:
            summary += "（影响分辨率/编码探测，可用 winget install Gyan.FFmpeg 安装）"
        self.field_summary.setText(summary)
        self.field_summary.setToolTip(summary)
        self.field_summary.setStyleSheet("" if ffprobe else "color: #cf222e;")

    def _verify_key(self) -> None:
        """启动和改完设置时验一次凭据，把结果显示在界面右下角。"""
        key = self.config.get("tmdb_key", "").strip()
        self._key_state = ""
        self._key_error = ""
        if not key:
            self._refresh_key_state()
            return
        self._key_state = "checking"
        self._refresh_key_state()
        self.key_worker = KeyCheckWorker(key)
        self.key_worker.checked.connect(self._on_key_checked)
        self.key_worker.start()

    def _on_key_checked(self, ok: bool, message: str) -> None:
        self._key_state = "ok" if ok else "fail"
        self._key_error = "" if ok else message
        self._refresh_key_state()
        self.log(("TMDB 凭据可用：" if ok else "TMDB 凭据不可用：") + message)

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
        # 新扫描的行全是「待识别」，旧筛选会把整表藏光，归位到「全部状态」
        self.filter_combo.setCurrentIndex(0)
        self.log(f"开始扫描 {self.root}")
        self.progress.setRange(0, 0)

        self.worker = ScanWorker(
            self.root,
            ffprobe,
            recursive=True,
            workers=int(self.config.get("probe_workers", 4) or 4),
        )
        self.worker.row_ready.connect(self._add_record)
        self.worker.row_filled.connect(self._fill_record)
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

        status_text = record.get("status", "待识别")
        status = QTableWidgetItem(_status_label(record, status_text))
        status.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        color = STATUS_COLORS.get(status_text)
        if color:
            status.setForeground(QColor(color))
        self.table.setItem(row, COL_STATUS, status)
        kind_label = {"tv": "剧集", "special": "特别篇"}.get(record.get("kind"), "电影")
        kind_item = QTableWidgetItem(kind_label)
        kind_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        self.table.setItem(row, COL_TYPE, kind_item)
        self._loading = False

        self.refresh_preview(row)
        self._apply_filter()

    def _fill_record(self, index: int, record: dict) -> None:
        """探测完成后回填这一行的技术参数。"""
        if index >= len(self.records) or index >= self.table.rowCount():
            return
        self.records[index] = record
        self._loading = True
        self.table.item(index, COL_SIZE).setText(_human_size(record.get("size", 0)))
        self.table.item(index, COL_SRC).setText(record.get("source", ""))
        self.table.item(index, COL_GROUP).setText(record.get("group", ""))
        self.table.item(index, COL_TYPE).setText(
            {"tv": "剧集", "special": "特别篇"}.get(record.get("kind"), "电影")
        )
        status_item = self.table.item(index, COL_STATUS)
        status_item.setText(record.get("status", "待识别"))
        color = STATUS_COLORS.get(record.get("status", ""))
        if color:
            status_item.setForeground(QColor(color))
        self._loading = False
        self.refresh_preview(index)
        self._apply_filter()

    def _scan_done(self, count: int) -> None:
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.log(f"扫描完成，共 {count} 个视频文件")
        self._status(f"扫描完成：{count} 个文件")
        self.refresh_all_previews()
        if getattr(self, "_pending_clean", False):
            self._pending_clean = False
            self.clean_shells(auto=True)

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
        status_item.setText(_status_label(record, status))
        color = STATUS_COLORS.get(status)
        if color:
            status_item.setForeground(QColor(color))
        self._loading = False
        self.refresh_preview(index)
        self._apply_filter()

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
    def _apply_filter(self) -> None:
        """按状态列筛选显示行（仅隐藏，不动数据和勾选）。"""
        want = self.filter_combo.currentText()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, COL_STATUS)
            text = item.text() if item else ""
            if want == "全部状态":
                show = True
            elif want == "无匹配":
                show = "无匹配" in text
            elif want == "需人工确认":
                show = "需人工确认" in text
            else:  # 已匹配
                # 「无匹配」「需人工确认」里也含「匹配」二字，要排除掉
                show = "已匹配" in text and "需人工确认" not in text
            self.table.setRowHidden(row, not show)

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
            ep_template=cfg.DEFAULT_EP_TEMPLATE,
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
        self.worker = ApplyWorker(ops, client, root=self.root)
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
        self._handle_residuals(getattr(report, "residuals", []))
        if report.ok:
            # 搬完之后必定留下一批空目录，扫完自动清一遍，不用用户再去 NAS 手动删
            self._pending_clean = True
            self.scan()

    # -------------------------------------------------------------- 清理空目录
    def clean_shells(self, auto: bool = False) -> None:
        """找空目录。auto=True 时只静默删完全空的，带文件的留给用户确认。"""
        if not self.root or not self.root.exists():
            if not auto:
                QMessageBox.information(self, "先选目录", "先选择一个媒体目录。")
            return
        self._clean_auto = auto
        self.log("执行后复查空目录…" if auto else "正在查找空目录…")
        self._status("正在查找空目录…")
        self.clean_scan = CleanScanWorker(self.root, int(self.config.get("probe_workers", 4) or 4))
        self.clean_scan.scanned.connect(self._on_shells_scanned)
        self.clean_scan.start()

    def _on_shells_scanned(self, candidates: list) -> None:
        auto = getattr(self, "_clean_auto", False)
        self._clean_auto = False
        if not candidates:
            self.log("没有空目录")
            self._status("没有空目录")
            return

        if auto:
            empty_only = [c for c in candidates if not c.files]
            with_files = [c for c in candidates if c.files]
            if empty_only:
                result = cleanup_mod.remove(empty_only)
                self.log(
                    f"自动清理空目录：删除 {result.ok} 个"
                    + (f"，失败 {len(result.failed)} 个" if result.failed else "")
                )
                for path, error in result.failed[:5]:
                    self.log(f"  失败 {path.name}：{error}")
            if with_files:
                self.log(f"另有 {len(with_files)} 个目录里还有残留文件，点「清理空目录」确认")
            self._status("空目录已清理")
            return

        dialog = ShellDialog(candidates, self.root, parent=self)
        if dialog.exec() != ShellDialog.Accepted:
            self.log(f"空目录：{len(candidates)} 个，取消删除")
            return
        chosen = dialog.selected()
        if not chosen:
            return
        self.log(f"正在删除 {len(chosen)} 个空目录…")
        self.clean_apply = CleanApplyWorker(chosen)
        self.clean_apply.done.connect(self._on_shells_removed)
        self.clean_apply.start()

    def _on_shells_removed(self, result) -> None:
        self.log(
            f"空目录：删除 {result.ok} 个"
            + (f"，失败 {len(result.failed)} 个" if result.failed else "")
        )
        for path, error in result.failed[:5]:
            self.log(f"  失败 {path.name}：{error}")
        self._status(f"已删除 {result.ok} 个空目录")

    def _handle_residuals(self, residuals: list[dict]) -> None:
        """片子搬走了，原目录里剩下的海报/旧 nfo/样片交给用户决定。"""
        if not residuals:
            return
        dialog = ResidualDialog(residuals, self)
        if dialog.exec() != ResidualDialog.Accepted:
            self.log(f"原目录残留：{len(residuals)} 个目录，保留不动")
            return
        chosen = dialog.selected()
        if not chosen:
            return
        if dialog.action == "move":
            ok, errors = apply_mod.move_residuals(chosen)
            self.log(f"残留文件：移动 {ok} 个，跳过 {len(errors)} 个")
            for error in errors[:5]:
                self.log(f"  {error}")
        elif dialog.action == "delete":
            ok, errors = apply_mod.delete_residuals(chosen)
            self.log(f"残留文件：删除 {ok} 个，失败 {len(errors)} 个")
            for error in errors[:5]:
                self.log(f"  {error}")

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
            self._verify_key()
            self.log("设置已保存")

    def open_log_dir(self) -> None:
        _open_folder(cfg.log_dir())

    def open_config_dir(self) -> None:
        _open_folder(cfg.config_dir())


def _open_folder(path: Path) -> None:
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def _status_label(record: dict, status: str) -> str:
    """剧集在状态前面带上 SxxE0x，一眼能对上是哪一集。"""
    if record.get("kind") != "tv":
        return status
    season = int(record.get("season") or 0)
    episode = int(record.get("episode") or 0)
    if not (season or episode):
        return status
    return f"S{season:02d}E{episode:02d} · {status}"


def _wrap_text(text: str, width: int = 96) -> list[str]:
    """按字符硬折行。Qt 只在空格处折，带 URL 的长句会撑破窗口。"""
    out: list[str] = []
    for raw in (text or "").splitlines() or [""]:
        while len(raw) > width:
            out.append(raw[:width])
            raw = raw[width:]
        out.append(raw)
    return out or [""]


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

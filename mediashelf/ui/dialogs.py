"""设置、手动匹配、计划预览三个对话框。"""

from __future__ import annotations

from pathlib import Path

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
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .. import config as cfg
from ..organizer import Op
from ..tmdb import TMDBClient


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
        self.test_btn.setFixedWidth(96)
        self.test_btn.clicked.connect(self._test)
        self.test_label = QLabel("填入凭据后可测试")
        self.test_label.setWordWrap(True)
        self.test_label.setObjectName("hintLabel")
        self.test_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        # 改了凭据就把上一次的测试结果作废，避免"显示可用但其实是旧 Key"
        self.key_edit.textChanged.connect(self._reset_test_state)

        test_row = QHBoxLayout()
        test_row.addWidget(self.test_btn)
        test_row.addWidget(self.test_label, 1)

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
        form.addRow("", test_row)
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
        buttons.button(QDialogButtonBox.Ok).setText("确定")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
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

    def _reset_test_state(self) -> None:
        if getattr(self, "_test_worker", None) is not None and self._test_worker.isRunning():
            return
        self.test_label.setText("凭据已修改，点「测试连接」验证")
        self.test_label.setStyleSheet("")

    def _set_test_state(self, state: str, text: str) -> None:
        colors = {"ok": "#1a7f37", "fail": "#cf222e", "checking": "#9a6700"}
        prefix = {"ok": "✓ ", "fail": "✗ ", "checking": ""}.get(state, "")
        self.test_label.setText(prefix + text)
        self.test_label.setStyleSheet(f"color: {colors[state]};" if state in colors else "")

    def _test(self) -> None:
        from .workers import KeyCheckWorker

        key = self.key_edit.text().strip()
        if not key:
            self._set_test_state("fail", "先粘贴 v3 Key 或 v4 Token")
            return
        self.test_btn.setEnabled(False)
        self._set_test_state("checking", "正在连接 TMDB…")
        # 网络请求放后台，不卡对话框
        self._test_worker = KeyCheckWorker(key)
        self._test_worker.checked.connect(self._on_test_done)
        self._test_worker.start()

    def _on_test_done(self, ok: bool, message: str) -> None:
        self.test_btn.setEnabled(True)
        self._set_test_state("ok" if ok else "fail", message)

    def closeEvent(self, event) -> None:  # noqa: D102
        worker = getattr(self, "_test_worker", None)
        if worker is not None and worker.isRunning():
            worker.wait(3000)
        super().closeEvent(event)

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
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
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
                self, "缺少数据源", "请先在设置里填写 TMDB 凭据，或直接使用免 Key 的 Wikidata。"
            )
            return
        self.list.clear()
        self.info.setText("搜索中…")
        self.setCursor(Qt.WaitCursor)
        try:
            results = self.client.search(
                self.query_edit.text().strip(), self.year_edit.text().strip()
            )
        except Exception as exc:  # noqa: BLE001 - 数据源可能是 Wikidata
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


def _merge_by_owner(ops: list[Op]) -> list[dict]:
    """把一个文件的多个操作（改名、字幕、nfo、海报）合并成一条记录。

    计划本身按操作为单位，执行时需要拆开；但人看的是"这部片会怎么处理"，
    227 条操作摊成 227 行没法核对，合并后 44 行一眼扫完。
    """
    rows: dict[str, dict] = {}
    order: list[str] = []
    for op in ops:
        key = op.owner or op.src or op.dst
        row = rows.get(key)
        if row is None:
            row = rows[key] = {
                "owner": key,
                "name": Path(key).name if key else "",
                "target": "",
                "rename": False,
                "sidecars": 0,
                "nfo": False,
                "poster": False,
                "fanart": False,
                "note": "",
                "kinds": 0,
            }
            order.append(key)
        row["kinds"] += 1
        if op.kind == "move":
            if not op.dst:
                row["note"] = op.note or "跳过"
            elif "字幕跟随" in op.note:
                row["sidecars"] += 1
            else:
                row["rename"] = True
                row["note"] = op.note
                row["target"] = op.dst
        elif op.kind == "write_nfo":
            row["nfo"] = True
            row["target"] = row["target"] or op.dst
        elif op.kind == "download":
            if op.note == "海报":
                row["poster"] = True
            elif op.note == "背景图":
                row["fanart"] = True
    return [rows[key] for key in order]


class PreviewDialog(QDialog):
    HEADERS = ["文件", "新文件名", "目标目录", "改名", "nfo", "海报", "背景", "备注"]

    def __init__(self, ops: list[Op], parent=None) -> None:
        super().__init__(parent)
        self.ops = ops
        self.plan = _merge_by_owner(ops)
        self.setWindowTitle("执行计划预览")
        self.resize(1160, 640)

        renamed = sum(1 for row in self.plan if row["rename"])
        nfo_count = sum(1 for row in self.plan if row["nfo"])
        poster_count = sum(1 for row in self.plan if row["poster"])
        skipped = sum(1 for row in self.plan if not row["rename"])

        parts = [
            f"{len(self.plan)} 个文件",
            f"改名 {renamed}",
            f"写 nfo {nfo_count}",
            f"海报 {poster_count}",
        ]
        if skipped:
            parts.append(f"不动 {skipped}")
        label = QLabel("　·　".join(parts))
        label.setObjectName("titleLabel")

        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索片名、路径或备注…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)

        self.table = QTableWidget(len(self.plan), len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        # 路径很长，允许换行会把行高撑成两三行，一屏就看不了几部
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.ElideMiddle)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self._haystack: list[str] = []

        for index, row in enumerate(self.plan):
            target = row["target"]
            new_name = Path(target).name if target else "—"
            target_dir = str(Path(target).parent) if target else "—"

            cells = [
                row["name"] or row["owner"],
                new_name,
                target_dir,
                "✓" if row["rename"] else "—",
                "✓" if row["nfo"] else "—",
                "✓" if row["poster"] else "—",
                "✓" if row["fanart"] else "—",
                row["note"],
            ]
            for col, value in enumerate(cells):
                cell = QTableWidgetItem(str(value))
                cell.setToolTip(str(value))
                if 3 <= col <= 6:
                    cell.setTextAlignment(Qt.AlignCenter)
                if col in (4, 5, 6) and value == "—":
                    cell.setForeground(Qt.gray)
                self.table.setItem(index, col, cell)

            self._haystack.append(
                " ".join([row["name"], row["owner"], new_name, target_dir, row["note"]]).lower()
            )

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        for col in (3, 4, 5, 6):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.Interactive)
        self.table.setColumnWidth(0, 240)
        self.table.setColumnWidth(7, 200)

        hint = QLabel("执行时只做同盘改名，不复制数据。完成后可在日志目录按日志撤销。")
        hint.setObjectName("hintLabel")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("执行")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(label)
        layout.addWidget(self.search)
        layout.addWidget(self.table, 1)
        layout.addWidget(hint)
        layout.addWidget(buttons)

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for index, hay in enumerate(self._haystack):
            self.table.setRowHidden(index, bool(needle) and needle not in hay)


class ResidualDialog(QDialog):
    """视频被移走后，原目录里剩下的文件怎么处理。

    真实的库里，一部片移走后目录里往往还留着 poster.jpg、旧的 movie.nfo、
    sample.mkv 或者没识别出来的另一个版本。这些要么跟着走、要么删掉，
    留在原地只会让刮削器认错。
    """

    def __init__(self, residuals: list[dict], parent=None) -> None:
        super().__init__(parent)
        self.residuals = residuals
        self.action = "keep"
        self.setWindowTitle("原目录还有文件")
        self.resize(780, 470)

        total_files = sum(len(item["files"]) for item in residuals)
        total_size = sum(size for item in residuals for _name, size in item["files"])

        title = QLabel(
            f"{len(residuals)} 个原目录的电影已移走，目录里还剩 {total_files} 个文件"
            f"（{_fmt_size(total_size)}）"
        )
        title.setObjectName("titleLabel")
        title.setWordWrap(True)

        hint = QLabel("勾选要一起处理的目录。下面是海报、旧 nfo、样片或没识别的其他视频：")
        hint.setWordWrap(True)
        hint.setObjectName("hintLabel")

        self.list = QListWidget()
        for item in residuals:
            names = [name for name, _size in item["files"]]
            size = sum(value for _name, value in item["files"])
            summary = "、".join(names[:4]) + ("…" if len(names) > 4 else "")
            row = QListWidgetItem(
                f"{item['src_dir']}\n    {summary}（{len(names)} 个，{_fmt_size(size)}）"
            )
            row.setFlags(row.flags() | Qt.ItemIsUserCheckable)
            row.setCheckState(Qt.Checked)
            row.setToolTip(
                "\n".join(f"{name}  {_fmt_size(value)}" for name, value in item["files"])
            )
            self.list.addItem(row)

        move_btn = QPushButton("一起移到新目录")
        delete_btn = QPushButton("删除这些文件")
        keep_btn = QPushButton("保留不动")
        move_btn.clicked.connect(self._choose_move)
        delete_btn.clicked.connect(self._choose_delete)
        keep_btn.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addWidget(keep_btn)
        buttons.addStretch(1)
        buttons.addWidget(move_btn)
        buttons.addWidget(delete_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addWidget(self.list, 1)
        layout.addLayout(buttons)

    def selected(self) -> list[dict]:
        return [
            item
            for index, item in enumerate(self.residuals)
            if self.list.item(index).checkState() == Qt.Checked
        ]

    def _choose_move(self) -> None:
        if not self.selected():
            QMessageBox.information(self, "没有选中", "先勾选要处理的目录。")
            return
        self.action = "move"
        self.accept()

    def _choose_delete(self) -> None:
        chosen = self.selected()
        if not chosen:
            QMessageBox.information(self, "没有选中", "先勾选要处理的目录。")
            return
        count = sum(len(item["files"]) for item in chosen)
        size = sum(value for item in chosen for _name, value in item["files"])
        answer = QMessageBox.warning(
            self,
            "确认删除",
            f"将删除 {count} 个文件，共 {_fmt_size(size)}。\n\n"
            "NAS / 网络盘上删除不进回收站，误删无法恢复。\n"
            "确认这些只是海报、旧 nfo 和样片，再继续。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self.action = "delete"
            self.accept()


def _fmt_size(size: int) -> str:
    if not size:
        return "0 B"
    if size >= 1024**3:
        return f"{size / 1024**3:.1f} GB"
    if size >= 1024**2:
        return f"{size / 1024**2:.0f} MB"
    return f"{size / 1024:.0f} KB"


class ShellDialog(QDialog):
    """清理空目录：列出找到的空壳，勾选后删掉。

    判定规则是"整棵树里没有任何视频文件"。带视频的目录压根不会出现在列表里，
    所以这里的删除不会碰到正片。
    """

    def __init__(self, candidates: list, root: Path, auto_note: str = "", parent=None) -> None:
        super().__init__(parent)
        self.candidates = candidates
        self.setWindowTitle("清理空目录")
        self.resize(820, 520)

        empty_count = sum(1 for c in candidates if not c.files)
        with_files = len(candidates) - empty_count
        total_size = sum(c.size for c in candidates)

        title = QLabel(
            f"找到 {len(candidates)} 个空目录"
            + (
                f"，其中 {with_files} 个里面还有残留小文件（共 {_fmt_size(total_size)}）"
                if with_files
                else ""
            )
        )
        title.setObjectName("titleLabel")
        title.setWordWrap(True)

        hint_text = "带视频文件的目录不会被列出。删除在 NAS 上不进回收站，删前请确认。"
        if auto_note:
            hint_text = auto_note + "\n" + hint_text
        hint = QLabel(hint_text)
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)

        self.list = QListWidget()
        for cand in candidates:
            try:
                rel = cand.path.relative_to(root)
            except ValueError:
                rel = cand.path
            item = QListWidgetItem(f"{rel}\n    {cand.describe()}")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            item.setToolTip(str(cand.path))
            self.list.addItem(item)

        all_btn = QPushButton("全选")
        none_btn = QPushButton("全不选")
        all_btn.clicked.connect(lambda: self._set_all(Qt.Checked))
        none_btn.clicked.connect(lambda: self._set_all(Qt.Unchecked))

        cancel_btn = QPushButton("取消")
        delete_btn = QPushButton(f"删除这 {len(candidates)} 个目录")
        delete_btn.setObjectName("primaryButton")
        cancel_btn.clicked.connect(self.reject)
        delete_btn.clicked.connect(self._confirm)

        buttons = QHBoxLayout()
        buttons.addWidget(all_btn)
        buttons.addWidget(none_btn)
        buttons.addStretch(1)
        buttons.addWidget(cancel_btn)
        buttons.addWidget(delete_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addWidget(self.list, 1)
        layout.addLayout(buttons)

    def _set_all(self, state) -> None:
        for index in range(self.list.count()):
            self.list.item(index).setCheckState(state)

    def selected(self) -> list:
        return [
            cand
            for index, cand in enumerate(self.candidates)
            if self.list.item(index).checkState() == Qt.Checked
        ]

    def _confirm(self) -> None:
        chosen = self.selected()
        if not chosen:
            QMessageBox.information(self, "没有选中", "先勾选要删除的目录。")
            return
        size = sum(c.size for c in chosen)
        detail = f"共 {size / 1024**2:.0f} MB 残留文件" if size else "全部是空目录，不含任何文件"
        answer = QMessageBox.warning(
            self,
            "确认删除",
            f"将删除 {len(chosen)} 个目录（{detail}）。\n\n"
            "NAS / 网络盘删除不进回收站，误删无法恢复。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self.accept()

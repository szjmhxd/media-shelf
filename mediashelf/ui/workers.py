"""后台线程：扫描、识别、执行。界面只收信号，不做阻塞调用。"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .. import naming, organizer
from ..probe import probe
from ..tmdb import TMDBClient, TMDBError


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (text or "").lower())


def _similar(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


class ScanWorker(QThread):
    progress = Signal(int, int, str)
    row_ready = Signal(object)
    done = Signal(int)

    def __init__(self, root: Path, ffprobe: str, recursive: bool = True) -> None:
        super().__init__()
        self.root = root
        self.ffprobe = ffprobe
        self.recursive = recursive

    def run(self) -> None:  # noqa: D102
        files = organizer.scan(self.root, recursive=self.recursive)
        total = len(files)
        count = 0
        for index, video in enumerate(files, start=1):
            self.progress.emit(index, total, video.name)
            result = probe(video, self.ffprobe)
            record = {
                "path": str(video),
                "name": video.name,
                "size": result.size,
                "probe": result,
                "meta": {},
                "source": result.source_guess(),
                "group": naming.detect_group(video.name) or naming.detect_group(video.parent.name),
                "edition": naming.detect_edition(video.name),
                "checked": True,
                "status": "待识别" if not result.error else f"探测失败：{result.error}",
                "target": "",
            }
            self.row_ready.emit(record)
            count += 1
        self.done.emit(count)


class IdentifyWorker(QThread):
    row_updated = Signal(int, object, str)
    progress = Signal(int, int, str)
    done = Signal(int, int)
    failed = Signal(str)

    def __init__(self, client: TMDBClient, records: list[dict]) -> None:
        super().__init__()
        self.client = client
        self.records = records
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:  # noqa: D102
        matched = 0
        pending = 0
        total = len(self.records)
        for index, record in enumerate(self.records, start=1):
            if self._stop:
                break
            self.progress.emit(index, total, Path(record["path"]).name)
            if not record.get("checked", True):
                continue

            query, year = naming.guess_query(record["path"])
            try:
                results = self.client.search(query, year)
            except TMDBError as exc:
                self.failed.emit(str(exc))
                continue

            meta, status = self._pick(results, query, year)
            if meta is None:
                pending += 1
                self.row_updated.emit(index - 1, {"query": query}, status)
                continue

            try:
                full = self.client.details(meta["id"])
            except TMDBError:
                full = meta
            self.row_updated.emit(index - 1, full, status)
            matched += 1

        self.client.save_cache()
        self.done.emit(matched, pending)

    def _pick(self, results: list[dict], query: str, year: str) -> tuple[dict | None, str]:
        if not results:
            return None, "无搜索结果"

        best = max(
            results,
            key=lambda item: (
                max(
                    _similar(query, item.get("cn", "")),
                    _similar(query, item.get("original", "")),
                )
                + (0.25 if year and item.get("year") == year else 0.0)
            ),
        )
        score = max(_similar(query, best.get("cn", "")), _similar(query, best.get("original", "")))
        year_ok = bool(year) and best.get("year") == year

        if score >= 0.62 or (year_ok and score >= 0.3):
            return best, "已匹配"
        if score >= 0.35:
            return best, "需人工确认"
        return None, "无搜索结果"


class ApplyWorker(QThread):
    progress = Signal(int, int, str)
    done = Signal(object)

    def __init__(self, ops, client=None) -> None:
        super().__init__()
        self.ops = ops
        self.client = client
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:  # noqa: D102
        from ..apply import execute

        report = execute(
            self.ops,
            client=self.client,
            progress=lambda done, total, op: self.progress.emit(done, total, op.note or op.kind),
            stop_flag=lambda: self._stop,
        )
        self.done.emit(report)

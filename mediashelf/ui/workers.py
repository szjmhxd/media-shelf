"""后台线程：扫描、识别、执行。界面只收信号，不做阻塞调用。"""

from __future__ import annotations

import difflib
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .. import naming, organizer
from ..probe import probe


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (text or "").lower())


def _similar(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    score = difflib.SequenceMatcher(None, a, b).ratio()
    # 副标题前缀：库里叫《冲出地球》，TMDB 叫《星游记之冲出地球》，字符相似度
    # 只有 0.67，会被阈值判成"无匹配"。一方完整包含另一方时给固定高分。
    # 长度差至少 2 才算，否则《流浪地球》会被判成《流浪地球2》。
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 4 and len(long) - len(short) >= 2 and short in long:
        score = max(score, 0.93)
    # 反过来，只差一个尾部序号的是续集，不是同一部：
    # 《流浪地球》和《流浪地球2》原始相似度 0.89，靠年份不一定救得回来。
    if long.startswith(short) and long[len(short) :].isdigit():
        score = min(score, 0.75)
    return score


class ScanWorker(QThread):
    progress = Signal(int, int, str)
    row_ready = Signal(object)
    row_filled = Signal(int, object)
    done = Signal(int)

    def __init__(self, root: Path, ffprobe: str, recursive: bool = True, workers: int = 4) -> None:
        super().__init__()
        self.root = root
        self.ffprobe = ffprobe
        self.recursive = recursive
        # NAS / 机械盘上并发 4 路通常比串行快 2~3 倍；再高会互相抢带宽
        self.workers = max(1, min(int(workers or 4), 16))
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def _one(self, video: Path, index: int):
        if self._stop:  # 已请求停止：排队中的任务直接跳过，别让线程池拖着界面
            return None
        result = probe(video, self.ffprobe)
        return index, {
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

    def run(self) -> None:  # noqa: D102
        files = organizer.scan(self.root, recursive=self.recursive)
        total = len(files)
        if not total:
            self.done.emit(0)
            return

        # 第一阶段：先把文件铺到表格里，界面立刻有东西看。
        # 探测一个 NAS 上的 50 GB 文件要一两秒，串起来就是两分钟空表格。
        self.progress.emit(0, total, "读取文件列表…")
        for video in files:
            try:
                size = video.stat().st_size
            except OSError:
                size = 0
            self.row_ready.emit(
                {
                    "path": str(video),
                    "name": video.name,
                    "size": size,
                    "probe": None,
                    "meta": {},
                    "source": "",
                    "group": naming.detect_group(video.name)
                    or naming.detect_group(video.parent.name),
                    "edition": naming.detect_edition(video.name),
                    "checked": True,
                    "status": "探测中…",
                    "target": "",
                }
            )

        # 第二阶段：并发探测，逐个回填参数
        count = 0
        futures: list = []
        pool = ThreadPoolExecutor(max_workers=self.workers)
        try:
            futures = [pool.submit(self._one, video, i) for i, video in enumerate(files)]
            for future in as_completed(futures):
                if self._stop:
                    break
                try:
                    payload = future.result()
                except Exception:  # noqa: BLE001 - 单个文件异常不能中断整批
                    continue
                if not payload:
                    continue
                index, record = payload
                count += 1
                self.row_filled.emit(index, record)
                self.progress.emit(count, total, record["name"])
        finally:
            for future in futures:
                future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)
            self.done.emit(count)


class KeyCheckWorker(QThread):
    """后台验证 TMDB 凭据，别让启动时卡在网络上。"""

    checked = Signal(bool, str)

    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key

    def run(self) -> None:  # noqa: D102
        from ..tmdb import test_key

        try:
            ok, message = test_key(self.key)
        except Exception as exc:  # noqa: BLE001 - 验证失败也要把原因显示出来
            ok, message = False, str(exc)
        self.checked.emit(ok, message)


class IdentifyWorker(QThread):
    row_updated = Signal(int, object, str)
    progress = Signal(int, int, str)
    done = Signal(int, int)
    failed = Signal(str)

    def __init__(self, client, records: list[dict]) -> None:
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
        try:
            for index, record in enumerate(self.records, start=1):
                if self._stop:
                    break
                self.progress.emit(index, total, Path(record["path"]).name)
                if not record.get("checked", True):
                    continue
                # 单条出错不能终止整批：每行独立 try，异常转成该行的状态
                time.sleep(0.6)  # 限速：连打会触发 429
                try:
                    matched, pending = self._identify_one(index, record, matched, pending)
                except Exception as exc:  # noqa: BLE001 - 兜住一切，别让线程静默死掉
                    pending += 1
                    reason = _reason(exc)
                    self.row_updated.emit(index - 1, {}, reason)
                    self.failed.emit(f"{Path(record['path']).name}：{reason}（{exc}）")
        finally:
            try:
                self.client.save_cache()
            except Exception:  # noqa: BLE001
                pass
            self.done.emit(matched, pending)

    def _identify_one(self, index: int, record: dict, matched: int, pending: int):
        query, year = naming.guess_query(record["path"])
        if not query:
            self.row_updated.emit(index - 1, {}, "无匹配：文件名里没有片名")
            return matched, pending + 1

        results = self.client.search(query, year)
        if not results:
            self.row_updated.emit(index - 1, {"query": query}, "无匹配")
            return matched, pending + 1

        # 先用搜索结果的标题粗排，只对最像的 2 个取详情。
        # 每个详情都是一次网络请求，53 部片子不能对每个候选都查。
        coarse = sorted(
            results,
            key=lambda item: (
                _similar(query, item.get("cn", ""))
                + (0.25 if year and year == item.get("year") else 0)
            ),
            reverse=True,
        )
        ranked = coarse[:1] if _similar(query, coarse[0].get("cn", "")) >= 0.92 else coarse[:2]

        best: dict | None = None
        best_score = 0.0
        best_weighted = -1.0
        best_year_ok = False
        for candidate in ranked:
            try:
                full = self.client.details(candidate["id"])
            except Exception:  # noqa: BLE001 - 单个候选失败不影响其余
                full = dict(candidate)
            cn = full.get("cn") or candidate.get("cn", "")
            en = full.get("en") or candidate.get("en", "") or full.get("original", "")
            score = max(_similar(query, cn), _similar(query, en))
            got = str(full.get("year") or candidate.get("year") or "")
            year_ok = bool(year) and got == year
            weighted = score + (0.3 if year_ok else 0.0)
            if weighted > best_weighted:
                best, best_score, best_weighted, best_year_ok = full, score, weighted, year_ok

        if best is None:
            self.row_updated.emit(index - 1, {"query": query}, "无匹配")
            return matched, pending + 1

        # 阈值偏严格：错配的片名比空着更麻烦，空着至少一眼能看出来
        if best_score >= 0.9 or (best_year_ok and best_score >= 0.85):
            status = "已匹配"
        elif best_score >= 0.7:
            status = "需人工确认"
        else:
            self.row_updated.emit(index - 1, {"query": query}, "无匹配")
            return matched, pending + 1

        # 年份对不上就降级，同名不同片是自动匹配最常见的错误
        if year and best.get("year") and str(best["year"]) != year:
            status = "需人工确认"
        if not best.get("overview") and hasattr(self.client, "enrich"):
            try:
                best = self.client.enrich(best)
            except Exception:  # noqa: BLE001 - 简介拿不到不影响改名
                pass
        self.row_updated.emit(index - 1, best, status)
        return matched + 1, pending


def _reason(exc: Exception) -> str:
    text = f"{type(exc).__name__} {exc}"
    low = text.lower()
    if "429" in text:
        return "被限流，稍后再试"
    if "timeout" in low or "timed out" in low or "connection" in low or "网络" in text:
        return "网络错误"
    if "401" in text or "unauthorized" in low or "api key" in low:
        return "Key 无效"
    return "识别出错"


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

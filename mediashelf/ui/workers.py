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


def classify_media(files: list[Path]) -> dict[str, dict]:
    """按目录聚类判断电影还是剧集，并解出季/集。

    判定规则偏保守：同一个目录里出现 >=2 个不同集号才算剧集。
    单个文件就算名字里带「01」也可能是《101忠狗》这类片名，
    误判成剧集会把电影搬进 Season 01，收拾起来比重命名麻烦得多。
    """
    groups: dict[Path, list[Path]] = {}
    for file in files:
        groups.setdefault(file.parent, []).append(file)

    out: dict[str, dict] = {}
    for parent, items in groups.items():
        parsed: list[tuple[Path, tuple[int, int]]] = []
        for file in items:
            hit = naming.parse_episode(file.name)
            if hit:
                parsed.append((file, hit))
        distinct = {episode for _file, (_season, episode) in parsed}
        is_tv = len(parsed) >= 2 and len(distinct) >= 2

        for file in items:
            hit = naming.parse_episode(file.name)
            season, episode = hit if hit else (0, 0)
            if is_tv and season == 0:
                season = naming.parse_season(parent.name, 1)
            out[str(file)] = {
                "kind": "tv" if is_tv else "movie",
                "season": season if is_tv else 0,
                "episode": episode if is_tv else 0,
            }
    return out


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
        self.kinds: dict[str, dict] = {}

    def stop(self) -> None:
        self._stop = True

    def _one(self, video: Path, index: int):
        if self._stop:  # 已请求停止：排队中的任务直接跳过，别让线程池拖着界面
            return None
        result = probe(video, self.ffprobe)
        info = self.kinds.get(str(video)) or {}
        return index, {
            "path": str(video),
            "name": video.name,
            "size": result.size,
            "probe": result,
            "meta": {},
            "source": result.source_guess(),
            "group": naming.detect_group(video.name) or naming.detect_group(video.parent.name),
            "edition": naming.detect_edition(video.name),
            "kind": info.get("kind", "movie"),
            "season": info.get("season", 0),
            "episode": info.get("episode", 0),
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
        # 先定性：哪些是剧集、各自的季/集号
        self.kinds = classify_media(files)

        # 第一阶段：先把文件铺到表格里，界面立刻有东西看。
        # 探测一个 NAS 上的 50 GB 文件要一两秒，串起来就是两分钟空表格。
        self.progress.emit(0, total, "读取文件列表…")
        for video in files:
            try:
                size = video.stat().st_size
            except OSError:
                size = 0
            info = self.kinds.get(str(video)) or {}
            self.row_ready.emit(
                {
                    "path": str(video),
                    "name": video.name,
                    "size": size,
                    "probe": None,
                    "meta": {},
                    "source": "",
                    "kind": info.get("kind", "movie"),
                    "season": info.get("season", 0),
                    "episode": info.get("episode", 0),
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
        from ..tmdb import short_error, test_key

        try:
            ok, message = test_key(self.key)
        except Exception as exc:  # noqa: BLE001 - 验证失败也要把原因显示出来
            ok, message = False, short_error(exc)
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
                    # 不要把异常全文写进日志：requests 的报错里带着完整 URL 和连接细节，
                    # 一行几百字符，几十行就会把日志区撑到屏幕外。
                    self.failed.emit(f"{Path(record['path']).name}：{reason}")
        finally:
            try:
                self.client.save_cache()
            except Exception:  # noqa: BLE001
                pass
            self.done.emit(matched, pending)

    def _window(self, query: str, results: list[dict]) -> list[dict]:
        """候选窗口用 TMDB 的原始顺序，不要按相似度重排后再截断。

        查询词是英文（Dan Da Dan）而 TMDB 返回中文名时，相关片的
        标题相似度是 0，会被自己的排序挤出窗口，最后匹配到一部无关片。
        相似度只用来在候选里挑最好的那个。
        """
        top = max(
            max(_similar(query, item.get("cn", "")), _similar(query, item.get("original", "")))
            for item in results[:1]
        )
        return results[:1] if top >= 0.92 else results[:3]

    def _pick(
        self, candidates: list[dict], queries: list[str], year: str, is_tv: bool
    ) -> tuple[dict | None, float, bool]:
        best: dict | None = None
        best_score = 0.0
        best_weighted = -1.0
        best_year_ok = False
        for candidate in candidates:
            try:
                if is_tv:
                    full = self.client.tv_details(candidate["id"])
                else:
                    full = self.client.details(candidate["id"])
            except Exception:  # noqa: BLE001 - 单个候选失败不影响其余
                full = dict(candidate)
            cn = full.get("cn") or candidate.get("cn", "")
            en = full.get("en") or candidate.get("en", "") or full.get("original", "")
            original = full.get("original") or candidate.get("original", "")
            # 打分对所有查询词取最大值：中文译名对不上官方译名时，
            # 英文原名那一段（长发公主.Tangled）就是命中的那把钥匙
            score = max(
                max(_similar(word, cn), _similar(word, en), _similar(word, original))
                for word in queries
            )
            got = str(full.get("year") or candidate.get("year") or "")
            year_ok = bool(year) and got == year
            # 年份不匹配要扣分，不能只给匹配的加分：《长发公主》(2010) 的
            # 标题相似度输《芭比之长发公主》(2002)，靠年份对不上才翻得回来
            year_penalty = -0.2 if (year and got and not year_ok) else 0.0
            weighted = score + (0.3 if year_ok else 0.0) + year_penalty
            if weighted > best_weighted:
                best, best_score, best_weighted, best_year_ok = full, score, weighted, year_ok
        return best, best_score, best_year_ok

    @staticmethod
    def _decide(
        best: dict | None, score: float, year_ok: bool, year: str, is_tv: bool = False
    ) -> str:
        # 阈值偏严格：错配的片名比空着更麻烦，空着至少一眼能看出来
        if best is None or score < 0.7:
            return "无匹配"
        if score >= 0.9 or (year_ok and score >= 0.85):
            status = "已匹配"
        else:
            status = "需人工确认"
        # 年份对不上就降级，同名不同片是自动匹配最常见的错误。
        # 剧集例外：季的播出年 ≠ 条目首播年是常态（家有儿女条目 2005、S03 是
        # 2007 播出；爱情公寓条目 2009、S05 是 2020 播出），拿目录年份卡条目
        # 年份会把正确的匹配全部降级掉。剧集的同名风险靠相似度阈值兜着。
        if not is_tv and year and best.get("year") and str(best["year"]) != year:
            status = "需人工确认"
        return status

    def _match_variety_episode(
        self, show_id, season_hint, variety: dict
    ) -> tuple[int, int, str] | None:
        """综艺正片在 TMDB 集表里定位这一集 → (season, episode, ep_title)。

        三级定位：播出日期精确 → 「第N期上/下」标题 token → 期号数学映射。
        上/下在 TMDB 里是独立集（第1期上=E01、第1期下=E02），不是一集拆两半，
        所以本地的「第N期上/下」能直接对上集号。日期最准，做第一优先。
        """
        qi, part = variety.get("qi") or 0, variety.get("part") or ""
        air = variety.get("air_date") or ""

        def season_eps(s: int) -> list[dict]:
            try:
                return self.client.season_details(show_id, s)
            except Exception:  # noqa: BLE001 - 单季拿不到不算整批失败
                return []

        # 目录里的季号优先（S02/ 锁定第 2 季），再全季兜底
        seasons: list[int] = []
        if season_hint:
            seasons.append(int(season_hint))
        try:
            full = self.client.tv_details(show_id)
            for s in full.get("seasons") or []:
                n = int(s.get("number") or s.get("season_number") or s.get("season") or 0)
                if n > 0 and n not in seasons:
                    seasons.append(n)
        except Exception:  # noqa: BLE001
            pass

        token = f"第{qi}期{part}" if qi else ""
        expected = naming.variety_expected_episode(qi, part) if qi else 0
        loose: list[tuple[int, dict]] = []

        for s in seasons:
            eps = season_eps(s)
            if not eps:
                continue
            # ① 播出日期精确命中
            if air:
                hits = [e for e in eps if (e.get("air_date") or "") == air]
                if len(hits) == 1:
                    e = hits[0]
                    return s, int(e["number"]), e.get("title") or ""
                if len(hits) > 1 and part:
                    for e in hits:
                        if part in (e.get("title") or ""):
                            return s, int(e["number"]), e.get("title") or ""
                loose += [(s, e) for e in hits]
            # ② 「第N期上/下」token 在集标题里唯一
            if token:
                hits = [e for e in eps if token in (e.get("title") or "")]
                if len(hits) == 1:
                    e = hits[0]
                    return s, int(e["number"]), e.get("title") or ""
            # ③ 期号数学映射（上=N*2-1 下=N*2）验证该集存在
            if expected:
                for e in eps:
                    if int(e.get("number") or 0) == expected:
                        loose.append((s, e))
                        break

        if len(loose) == 1:
            s, e = loose[0]
            return s, int(e["number"]), e.get("title") or ""
        if len(loose) > 1 and part:
            for s, e in loose:
                if part in (e.get("title") or ""):
                    return s, int(e["number"]), e.get("title") or ""
        return None

    def _identify_one(self, index: int, record: dict, matched: int, pending: int):
        is_tv = record.get("kind") in ("tv", "special", "variety")
        # 综艺形态：「第N期上/下」「第N期加更」+ 播出日期。普通剧集不会叫「第N期」，
        # parse_variety 对 SxxExx / 「01.第一回」返回 qi=0，不会误触发
        variety = naming.parse_variety(record.get("name") or record["path"]) if is_tv else {}
        # 剧集的文件名是「01.flv」「第01话…」，片名要从父目录取
        if is_tv:
            query, year = naming.guess_series_query(record["path"])
        else:
            query, year = naming.guess_query(record["path"])
        if not query:
            self.row_updated.emit(index - 1, {}, "无匹配：文件名里没有片名")
            return matched, pending + 1

        def search_one(word: str) -> list[dict]:
            # TV 搜索不带年份硬过滤：季的播出年和条目首播年不同是常态，
            # 带 first_air_date_year 会把正主直接过滤掉（搜《爱情公寓》+2014
            # 只返回《球爱酒吧》，正主 2009 首播被滤掉）。年份在打分里仍然参与。
            return self.client.search_tv(word) if is_tv else self.client.search(word, year)

        # 备用查询词：①「真人版」这类 TMDB 标题里没有的后缀先剥掉；
        # ②文件名里的英文/日文原名段（长发公主.Tangled → Tangled）。
        # 主查询词一击命中就不打这些额外请求，省得撞 429。
        variants: list[str] = []
        stripped = naming.strip_edition_suffix(query)
        if stripped and _norm(stripped) != _norm(query):
            variants.append(stripped)
        known = {_norm(query)} | {_norm(v) for v in variants}
        for word in naming.query_variants(record["path"], query):
            if _norm(word) not in known:
                known.add(_norm(word))
                variants.append(word)

        queries = [query]
        candidates: list[dict] = []
        results = search_one(query)
        if results:
            candidates = self._window(query, results)
        best: dict | None = None
        score, year_ok = 0.0, False
        if candidates:
            best, score, year_ok = self._pick(candidates, queries, year, is_tv)
        status = self._decide(best, score, year_ok, year, is_tv)

        if status != "已匹配":
            for word in variants:
                found = search_one(word)
                if not found:
                    continue
                queries.append(word)
                seen = {item.get("id") for item in candidates}
                candidates += [it for it in self._window(word, found) if it.get("id") not in seen]
                best, score, year_ok = self._pick(candidates, queries, year, is_tv)
                status = self._decide(best, score, year_ok, year, is_tv)
                if status == "已匹配":
                    break

        if best is None or status == "无匹配":
            self.row_updated.emit(index - 1, {"query": query}, "无匹配")
            return matched, pending + 1
        if not best.get("overview") and hasattr(self.client, "enrich"):
            try:
                best = self.client.enrich(best)
            except Exception:  # noqa: BLE001 - 简介拿不到不影响改名
                pass

        # 综艺：加更/花絮/先导篇 TMDB 不收录集条目，归 Specials（Season 00）；
        # 正片用播出日期/期号在集表里定位。上下是独立集，数学映射上=N*2-1 下=N*2
        if is_tv and variety.get("special"):
            record["kind"] = "special"
            record["season"] = 0
            record["episode"] = 0  # organizer 按播出日期统一编 S00E
            record["ep_title"] = variety.get("title") or ""
        elif is_tv and variety.get("qi"):
            hit = self._match_variety_episode(best.get("id"), record.get("season"), variety)
            if hit:
                record["season"], record["episode"] = hit[0], hit[1]
                best = dict(best)
                best["ep_title"] = hit[2]
            else:
                # 集表里定位不到（TMDB 缺集/期号异常），不动集号，交人工
                status = "需人工确认"

        # 剧集补集标题。同一季的 season_details 有磁盘缓存，12 集只打一次请求
        if is_tv and record.get("season") and record.get("episode"):
            try:
                episodes = self.client.season_details(best["id"], int(record["season"]))
                want = int(record["episode"])
                for item in episodes:
                    if item.get("number") == want:
                        best = dict(best)
                        best["ep_title"] = item.get("title") or ""
                        break
            except Exception:  # noqa: BLE001 - 集标题拿不到不影响改名
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

    def __init__(self, ops, client=None, root: Path | None = None) -> None:
        super().__init__()
        self.ops = ops
        self.client = client
        self.root = root
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
            root=self.root,
        )
        self.done.emit(report)


class CleanScanWorker(QThread):
    """找空目录。NAS 上一个目录一个目录地扫，几秒钟，放后台别卡界面。"""

    scanned = Signal(object)

    def __init__(self, root: Path, workers: int = 8) -> None:
        super().__init__()
        self.root = root
        self.workers = workers

    def run(self) -> None:  # noqa: D102
        from .. import cleanup

        try:
            found = cleanup.scan(self.root, concurrency=self.workers)
        except Exception:  # noqa: BLE001 - 扫不了就当没有，别让线程炸掉界面
            found = []
        self.scanned.emit(found)


class CleanApplyWorker(QThread):
    """删空目录。只做文件级删除，不碰有视频的目录。"""

    done = Signal(object)

    def __init__(self, candidates) -> None:
        super().__init__()
        self.candidates = candidates

    def run(self) -> None:  # noqa: D102
        from .. import cleanup

        self.done.emit(cleanup.remove(self.candidates))

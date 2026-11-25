"""免 Key 数据源：Wikidata 搜索 + 标签，维基百科补简介。

维基百科的全文本搜索对中文片名很不可靠（搜「不要见怪」会返回一堆人名）。
Wikidata 的 `wbsearchentities` 按标签和别名匹配，更适合查片名，
还能直接拿到简体中文名、英文名、上映年份和 IMDb ID，因此作为无 Key 时的默认数据源。

能力边界：没有评分、演员表、TMDB ID。要完整元数据仍然需要 TMDB。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from . import config as cfg

WD_API = "https://www.wikidata.org/w/api.php"
WP_API = "https://zh.wikipedia.org/w/api.php"
TIMEOUT = 12
# 结果结构变化时改这个前缀，避免旧缓存永远盖住新逻辑
CACHE_VERSION = "v2"
UA = {"User-Agent": f"MediaShelf/{cfg.APP_VERSION} (movie library tool)"}

# 消歧后缀：《2012 (電影)》《Dune (film)》
DISAMBIG = re.compile(
    r"\s*[\(（][^\)）]*?(?:电影|電影|film|movie|電視劇|电视剧)[^\)）]*?[\)）]\s*$"
)

# 属于「电影」的 P31 取值
FILM_CLASSES = {
    "Q11424",  # film
    "Q202866",  # animated film
    "Q506240",  # television film
    "Q93204",  # documentary film
    "Q17123180",  # live-action film
}
FILM_HINTS = ("电影", "電影", "film", "movie", "動畫", "动画")


class WikiError(RuntimeError):
    pass


def clean_title(title: str) -> str:
    return DISAMBIG.sub("", (title or "").strip()).strip()


def normalize(text: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (text or "").lower())


class WikiProvider:
    """接口形状对齐 TMDBClient，界面可以无差别调用。"""

    def __init__(self, language: str = "zh-CN") -> None:
        self.api_key = "wikidata"
        self.language = language
        self.session = requests.Session()
        self.session.headers.update(UA)
        self._wiki_titles: dict[str, str] = {}
        self._last_call = 0.0
        self.cache_path: Path = cfg.data_dir() / "wikidata_cache.json"
        self.cache: dict[str, Any] = {}
        if self.cache_path.exists():
            try:
                self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.cache = {}

    def save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------ 搜索
    def search(self, query: str, year: str = "") -> list[dict[str, Any]]:
        """两路并查：Wikidata 标签精确匹配 + 中文维基全文搜索。

        只用 Wikidata 会漏掉「冰海陷落」这类中文标签缺失的片子；
        只用维基全文本又会把人物条目混进来。合并后交给上层按相似度打分。
        """
        query = (query or "").strip()
        if not query:
            return []
        key = f"{CACHE_VERSION}:search:{query}|{year}"
        if key in self.cache:
            return self.cache[key]

        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        limited = False

        try:
            wd_items = self._search_wikidata(query)
        except WikiError as exc:
            wd_items = []
            limited = "429" in str(exc)

        for item in wd_items:
            desc = item.get("description") or ""
            results.append(
                {
                    "id": item["id"],
                    "cn": clean_title(item.get("label") or ""),
                    "original": "",
                    "en": "",
                    "year": _year_from_text(desc),
                    "overview": desc,
                    "rating": 0.0,
                    "poster": "",
                }
            )
            seen.add(normalize(item.get("label") or ""))
            seen.add(normalize(item.get("id") or ""))

        if not results:  # Wikidata 没结果才去维基全文搜，避免被限流
            for title in self._search_wiki(query, year):
                if normalize(title) in seen:
                    continue
                results.append(
                    {
                        "id": f"wp:{title}",
                        "cn": clean_title(title),
                        "original": "",
                        "en": "",
                        "year": "",
                        "overview": "",
                        "rating": 0.0,
                        "poster": "",
                    }
                )

        if not results and limited:
            raise WikiError("HTTP 429 被限流，稍后再试或改用 TMDB")
        # 空结果不写缓存：一次网络抖动不该固化成永久「查不到」
        if results:
            self.cache[key] = results
        return results

    def _search_wikidata(self, query: str) -> list[dict[str, Any]]:
        langs = ["zh", "zh-hans"] if not query.isascii() else ["zh", "en"]
        last_error: Exception | None = None
        for lang in langs:
            try:
                raw = (
                    self._get(
                        WD_API,
                        {
                            "action": "wbsearchentities",
                            "search": query,
                            "language": lang,
                            "uselang": "zh",
                            "type": "item",
                            "limit": 10,
                        },
                        attempts=1,
                        timeout=6,
                    ).get("search")
                    or []
                )
            except WikiError as exc:
                last_error = exc
                continue
            if raw:
                # 描述里带「电影 / film」的排前面，纯人名、姓氏的靠后
                def rank(item: dict[str, Any]) -> int:
                    desc = (item.get("description") or "").lower()
                    if not desc:
                        return 1
                    return 0 if any(hint in desc for hint in FILM_HINTS) else 2

                return sorted(raw, key=rank)[:8]
        if last_error and "429" in str(last_error):
            raise WikiError("HTTP 429 被限流")
        return []

    def _search_wiki(self, query: str, year: str) -> list[str]:
        keyword = f"{query} {year} 电影".strip() if year else f"{query} 电影"
        try:
            data = self._get(
                WP_API,
                {"action": "query", "list": "search", "srsearch": keyword, "srlimit": 8},
                attempts=1,
                timeout=8,
            )
        except WikiError:
            return []
        hits = ((data.get("query") or {}).get("search")) or []
        return [clean_title(hit.get("title") or "") for hit in hits if hit.get("title")]

    # ------------------------------------------------------------------ 详情
    def details(self, item_id: str) -> dict[str, Any]:
        if item_id.startswith("wp:"):
            return self._details_from_wiki(item_id[3:])

        key = f"{CACHE_VERSION}:item:{item_id}"
        if key in self.cache:
            return self.cache[key]

        data = self._get(
            WD_API,
            {
                "action": "wbgetentities",
                "ids": item_id,
                "props": "labels|descriptions|claims|sitelinks",
                "languages": "zh|zh-hans|zh-cn|zh-hant|en",
                "languagefallback": 1,
                "sitefilter": "zhwiki",
            },
        )
        entity = (data.get("entities") or {}).get(item_id) or {}
        if not entity or "missing" in entity:
            raise WikiError(f"Wikidata 没有该条目：{item_id}")

        labels = _flatten(entity.get("labels") or {})
        descs = _flatten(entity.get("descriptions") or {})
        claims = entity.get("claims") or {}

        cn = _pick(labels, ["zh-hans", "zh-cn", "zh", "zh-hant"]) or _pick(labels, ["en"])
        en = _pick(labels, ["en"])
        year = _claim_year(claims)
        kinds = _claim_ids(claims, "P31")

        # P31 明确不是电影就跳过，避免把人物、电视剧当片子
        if kinds and not (set(kinds) & FILM_CLASSES):
            raise WikiError("该条目不是电影")

        overview = _pick(descs, ["zh-hans", "zh-cn", "zh"])
        self._wiki_titles[item_id] = ((entity.get("sitelinks") or {}).get("zhwiki") or {}).get(
            "title", ""
        )

        meta = {
            "id": item_id,
            "cn": clean_title(cn),
            "en": clean_title(en),
            "original": clean_title(en),
            "year": year,
            "release_date": "",
            "runtime": 0,
            "rating": 0.0,
            "votes": 0,
            "overview": overview[:600],
            "tagline": "",
            "genres": [],
            "tmdbid": None,
            "imdbid": _claim_string(claims, "P345"),
            "collection": "",
            "poster": _claim_image(claims),
            "backdrop": "",
            "directors": [],
            "actors": [],
            "country": [],
            "provider": "wikidata",
        }
        self.cache[key] = meta
        self.save_cache()
        return meta

    def _details_from_wiki(self, title: str) -> dict[str, Any]:
        """维基条目：先查对应的 Wikidata 条目拿英文名，拿不到就用条目本身的信息。"""
        key = f"{CACHE_VERSION}:wp:{title}"
        if key in self.cache:
            return self.cache[key]

        qid = self._qid_for_title(title)
        if qid:
            try:
                meta = dict(self.details(qid))
                if not meta.get("cn"):
                    meta["cn"] = clean_title(title)
                self.cache[key] = meta
                return meta
            except WikiError:
                pass

        text = self._extract(title)
        meta = {
            "id": key,
            "cn": clean_title(title),
            "en": "",
            "original": "",
            "year": _year_from_text(text),
            "release_date": "",
            "runtime": 0,
            "rating": 0.0,
            "votes": 0,
            "overview": text[:600],
            "tagline": "",
            "genres": [],
            "tmdbid": None,
            "imdbid": "",
            "collection": "",
            "poster": "",
            "backdrop": "",
            "directors": [],
            "actors": [],
            "country": [],
            "provider": "wikipedia",
        }
        self.cache[key] = meta
        self.save_cache()
        return meta

    def _qid_for_title(self, title: str) -> str:
        key = f"{CACHE_VERSION}:qid:{title}"
        if key in self.cache:
            return self.cache[key]
        try:
            data = self._get(
                WP_API,
                {"action": "query", "prop": "pageprops", "redirects": 1, "titles": title},
            )
        except WikiError:
            return ""
        pages = (data.get("query") or {}).get("pages") or []
        page = pages[0] if isinstance(pages, list) and pages else {}
        qid = ((page.get("pageprops") or {}).get("wikibase_item")) or ""
        self.cache[key] = qid
        return qid

    def _extract(self, title: str) -> str:
        key = f"{CACHE_VERSION}:extract:{title}"
        if key in self.cache:
            return self.cache[key]
        try:
            data = self._get(
                WP_API,
                {
                    "action": "query",
                    "prop": "extracts",
                    "exintro": 1,
                    "explaintext": 1,
                    "redirects": 1,
                    "titles": title,
                },
            )
        except WikiError:
            return ""
        pages = (data.get("query") or {}).get("pages") or []
        page = pages[0] if isinstance(pages, list) and pages else {}
        text = re.sub(r"\s+", " ", page.get("extract") or "").strip()
        self.cache[key] = text
        return text

    def enrich(self, meta: dict[str, Any]) -> dict[str, Any]:
        """补简介。只对确定采用的条目调用，避免给每个候选都多打一次请求。"""
        if meta.get("overview"):
            return meta
        item_id = str(meta.get("id") or "")
        title = self._wiki_titles.get(item_id, "")
        if not title and item_id.startswith("wp:"):
            title = item_id[3:]
        if not title:
            return meta
        text = self._extract(title)
        if text:
            meta = dict(meta)
            meta["overview"] = text[:600]
            self.cache[f"{CACHE_VERSION}:item:{item_id}"] = meta
        return meta

    def download_image(self, remote: str, dest: Path) -> bool:
        if not remote:
            return False
        try:
            resp = self.session.get(remote, timeout=TIMEOUT, stream=True)
            if resp.status_code != 200:
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as handle:
                for chunk in resp.iter_content(65536):
                    if chunk:
                        handle.write(chunk)
            return dest.stat().st_size > 1024
        except (requests.RequestException, OSError):
            return False

    # ------------------------------------------------------------------
    def _get(
        self, url: str, params: dict[str, Any], attempts: int = 2, timeout: float = TIMEOUT
    ) -> dict[str, Any]:
        params = {**params, "format": "json", "formatversion": 2}
        last = ""
        for attempt in range(attempts):
            # 温和限速：Wikimedia 对连续请求会回 429，被限流后整批都会查不到
            gap = time.time() - self._last_call
            if gap < 0.25:
                time.sleep(0.25 - gap)
            try:
                resp = self.session.get(url, params=params, timeout=timeout)
                self._last_call = time.time()
            except requests.RequestException as exc:
                last = f"网络错误：{exc}"
                time.sleep(0.8 * (attempt + 1))
                continue
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise WikiError(f"返回内容不是 JSON：{exc}") from exc
            last = f"HTTP {resp.status_code}"
            if resp.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
            time.sleep(0.8 * (attempt + 1))
        raise WikiError(last or "请求失败")


# ---------------------------------------------------------------------- 工具
def _flatten(mapping: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for lang, value in mapping.items():
        if isinstance(value, dict):
            out[lang] = str(value.get("value") or "")
        elif value:
            out[lang] = str(value)
    return out


def _pick(mapping: dict[str, str], prefs: list[str]) -> str:
    for lang in prefs:
        if mapping.get(lang):
            return mapping[lang]
    return ""


def _claim_values(claims: dict[str, Any], prop: str) -> list[Any]:
    out = []
    for claim in claims.get(prop) or []:
        try:
            out.append(claim["mainsnak"]["datavalue"]["value"])
        except (KeyError, TypeError):
            continue
    return out


def _claim_ids(claims: dict[str, Any], prop: str) -> list[str]:
    return [v.get("id", "") for v in _claim_values(claims, prop) if isinstance(v, dict)]


def _claim_string(claims: dict[str, Any], prop: str) -> str:
    for value in _claim_values(claims, prop):
        if isinstance(value, str):
            return value
    return ""


def _claim_year(claims: dict[str, Any]) -> str:
    for value in _claim_values(claims, "P577"):
        if isinstance(value, dict):
            match = re.match(r"[+-](\d{4})", value.get("time", "") or "")
            if match:
                return match.group(1)
    return ""


def _claim_image(claims: dict[str, Any]) -> str:
    name = _claim_string(claims, "P18")
    if not name:
        return ""
    return f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(name)}?width=780"


def _year_from_text(text: str) -> str:
    match = re.search(r"(19\d{2}|20\d{2})", text or "")
    return match.group(1) if match else ""

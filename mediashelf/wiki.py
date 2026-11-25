"""维基百科兜底接口：不需要任何 Key。

能力有限但足够拿到「中英文名 + 年份 + 简介」，用于在未配置 TMDB 时
仍然能生成可用的 nfo。没有 TMDB ID、评分、演员表。
"""

from __future__ import annotations

import difflib
import json
import re
import time
from pathlib import Path
from typing import Any

import requests

from . import config as cfg

API = "https://zh.wikipedia.org/w/api.php"
TIMEOUT = 15


class WikiError(RuntimeError):
    pass


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (text or "").lower())


class WikiProvider:
    """接口形状对齐 TMDBClient，方便界面无差别调用。"""

    def __init__(self, language: str = "zh-CN") -> None:
        self.api_key = "wikipedia"
        self.language = language
        self.session = requests.Session()
        self.session.headers["User-Agent"] = f"MediaShelf/{cfg.APP_VERSION} (movie library tool)"
        self.cache_path: Path = cfg.data_dir() / "wiki_cache.json"
        self.cache: dict[str, Any] = {}
        if self.cache_path.exists():
            try:
                self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.cache = {}

    def save_cache(self) -> None:
        try:
            self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------
    def search(self, query: str, year: str = "") -> list[dict[str, Any]]:
        keyword = f"{query} {year} 电影".strip() if year else f"{query} 电影"
        data = self._get(
            {
                "action": "query",
                "list": "search",
                "srsearch": keyword,
                "srlimit": 6,
                "srprop": "snippet",
            }
        )
        hits = ((data.get("query") or {}).get("search")) or []
        if not hits:
            data = self._get({"action": "query", "list": "search", "srsearch": query, "srlimit": 6})
            hits = ((data.get("query") or {}).get("search")) or []

        results = []
        for hit in hits:
            title = hit.get("title") or ""
            results.append(
                {
                    "id": title,
                    "cn": title,
                    "original": "",
                    "year": "",
                    "overview": _strip_html(hit.get("snippet") or ""),
                    "rating": 0.0,
                    "poster": "",
                }
            )
        results.sort(
            key=lambda item: difflib.SequenceMatcher(None, _norm(query), _norm(item["cn"])).ratio(),
            reverse=True,
        )
        return results

    def details(self, page_title: str) -> dict[str, Any]:
        key = f"page:{page_title}"
        if key in self.cache:
            return self.cache[key]

        data = self._get(
            {
                "action": "query",
                "prop": "extracts|langlinks|pageimages",
                "exintro": 1,
                "explaintext": 1,
                "lllang": "en",
                "piprop": "original",
                "titles": page_title,
            }
        )
        pages = ((data.get("query") or {}).get("pages")) or []
        page = pages[0] if isinstance(pages, list) and pages else {}
        if not page or page.get("missing"):
            raise WikiError(f"维基百科没有该条目：{page_title}")

        extract = page.get("extract") or ""
        langlinks = page.get("langlinks") or []
        english = langlinks[0].get("title") if langlinks else ""
        english = re.sub(r"\s*\([^)]*(?:film|movie|電影|电影)[^)]*\)\s*$", "", english or "")
        year = _find_year(extract)
        image = ((page.get("original") or {}).get("source")) or ""

        meta = {
            "id": page_title,
            "cn": page.get("title") or page_title,
            "en": english,
            "original": english,
            "year": year,
            "release_date": "",
            "runtime": 0,
            "rating": 0.0,
            "votes": 0,
            "overview": _shorten(extract, 400),
            "tagline": "",
            "genres": [],
            "tmdbid": None,
            "imdbid": "",
            "collection": "",
            "poster": image,
            "backdrop": "",
            "directors": [],
            "actors": [],
            "country": [],
            "provider": "wikipedia",
        }
        self.cache[key] = meta
        self.save_cache()
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
    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        params = {**params, "format": "json", "formatversion": 2}
        last = ""
        for attempt in range(3):
            try:
                resp = self.session.get(API, params=params, timeout=TIMEOUT)
            except requests.RequestException as exc:
                last = f"网络错误：{exc}"
                time.sleep(1 + attempt)
                continue
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise WikiError(f"返回内容不是 JSON：{exc}") from exc
            last = f"HTTP {resp.status_code}"
            time.sleep(1 + attempt)
        raise WikiError(last or "维基百科请求失败")


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _shorten(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit]


def _find_year(text: str) -> str:
    matches = re.findall(r"(19\d{2}|20\d{2})\s*年", text or "")
    return matches[0] if matches else ""

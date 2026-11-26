"""TMDB 接口封装。

支持两种凭据：
· v3 API Key（32 位十六进制）→ 走 api_key 参数
· v4 Read Access Token（eyJ 开头的 JWT）→ 走 Bearer 头

中文名取 `title`（language=zh-CN），英文名取 `original_title`，
若原名不是英文（比如法语片），再从 alternative_titles 里找 US/GB 的标题。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import requests

from . import config as cfg

API = "https://api.themoviedb.org/3"
IMAGE = "https://image.tmdb.org/t/p/original"
TIMEOUT = 20


class TMDBError(RuntimeError):
    pass


class TMDBClient:
    def __init__(self, api_key: str, language: str = "zh-CN") -> None:
        self.api_key = (api_key or "").strip()
        self.language = language or "zh-CN"
        self.session = requests.Session()
        self.session.headers["User-Agent"] = f"MediaShelf/{cfg.APP_VERSION}"
        if self.api_key.startswith("eyJ"):
            self.session.headers["Authorization"] = f"Bearer {self.api_key}"
            self._use_bearer = True
        else:
            self._use_bearer = False
        self.cache_path: Path = cfg.data_dir() / "tmdb_cache.json"
        self.cache: dict[str, Any] = {}
        self._load_cache()

    # ------------------------------------------------------------------ 缓存
    def _load_cache(self) -> None:
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

    # ------------------------------------------------------------------ 请求
    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.api_key:
            raise TMDBError("未配置 TMDB API Key")

        params = dict(params or {})
        params.setdefault("language", self.language)
        if not self._use_bearer:
            params["api_key"] = self.api_key

        last_error = ""
        for attempt in range(3):
            try:
                resp = self.session.get(API + path, params=params, timeout=TIMEOUT)
            except requests.RequestException as exc:
                last_error = f"网络错误：{short_error(exc)}"
                time.sleep(1.0 + attempt)
                continue

            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise TMDBError(f"返回内容不是 JSON：{exc}") from exc
            if resp.status_code == 401:
                raise TMDBError("API Key 无效或已失效（401）")
            if resp.status_code == 404:
                raise TMDBError("资源不存在（404）")
            if resp.status_code == 429:
                time.sleep(2.0 + attempt * 2)
                last_error = "请求过于频繁（429）"
                continue
            last_error = f"HTTP {resp.status_code}: {resp.text[:120]}"

        raise TMDBError(last_error or "请求失败")

    # ------------------------------------------------------------------ 接口
    def search(self, query: str, year: str = "") -> list[dict[str, Any]]:
        params: dict[str, Any] = {"query": query, "include_adult": "false"}
        if year:
            params["year"] = year
        data = self._get("/search/movie", params)
        results = data.get("results") or []
        if not results and year:
            # 年份记错的情况很常见，退一步再搜一次
            data = self._get("/search/movie", {"query": query, "include_adult": "false"})
            results = data.get("results") or []
        return [self._brief(item) for item in results]

    def search_any(self, query: str, year: str = "") -> list[dict[str, Any]]:
        """搜索 + 详情，返回可直接展示的完整结果。"""
        briefs = self.search(query, year)
        out = []
        for brief in briefs[:8]:
            try:
                out.append(self.details(brief["id"]))
            except TMDBError:
                out.append(brief)
        return out

    def details(self, tmdb_id: int) -> dict[str, Any]:
        key = f"detail:{tmdb_id}:{self.language}"
        if key in self.cache:
            return self.cache[key]

        data = self._get(
            f"/movie/{tmdb_id}",
            {"append_to_response": "alternative_titles,credits,external_ids,release_dates"},
        )
        meta = self._normalize(data)
        self.cache[key] = meta
        self.save_cache()
        return meta

    # ------------------------------------------------------------------ 剧集（TV）
    def search_tv(self, query: str, year: str = "") -> list[dict[str, Any]]:
        """剧集搜索。TMDB 的剧集和电影是两套接口，不能混用。"""
        params: dict[str, Any] = {"query": query, "include_adult": "false"}
        if year:
            params["first_air_date_year"] = year
        data = self._get("/search/tv", params)
        results = data.get("results") or []
        if not results and year:
            data = self._get("/search/tv", {"query": query, "include_adult": "false"})
            results = data.get("results") or []
        return [self._brief_tv(item) for item in results]

    def tv_details(self, tmdb_id: int) -> dict[str, Any]:
        key = f"tvdetail:{tmdb_id}:{self.language}"
        if key in self.cache:
            return self.cache[key]
        data = self._get(
            f"/tv/{tmdb_id}",
            {"append_to_response": "alternative_titles,credits,external_ids"},
        )
        meta = self._normalize_tv(data)
        self.cache[key] = meta
        self.save_cache()
        return meta

    def season_details(self, tmdb_id: int, season: int) -> list[dict[str, Any]]:
        """某一季的集列表：集号 + 集标题。文件重命名时用得上。"""
        key = f"tvseason:{tmdb_id}:{season}:{self.language}"
        if key in self.cache:
            return self.cache[key]
        data = self._get(f"/tv/{tmdb_id}/season/{season}")
        episodes = [
            {
                "number": item.get("episode_number") or 0,
                "title": (item.get("name") or "").strip(),
                "air_date": item.get("air_date") or "",
            }
            for item in data.get("episodes") or []
        ]
        self.cache[key] = episodes
        self.save_cache()
        return episodes

    def _brief_tv(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": item.get("id"),
            "kind": "tv",
            "cn": item.get("name") or "",
            "original": item.get("original_name") or "",
            "year": (item.get("first_air_date") or "")[:4],
            "overview": item.get("overview") or "",
            "rating": round(float(item.get("vote_average") or 0), 1),
            "poster": item.get("poster_path") or "",
            "original_language": item.get("original_language") or "",
        }

    def _normalize_tv(self, data: dict[str, Any]) -> dict[str, Any]:
        original = (data.get("original_name") or "").strip()
        english = _english_title(data, original)
        air = data.get("first_air_date") or ""
        genres = [g.get("name", "") for g in data.get("genres") or []]
        crew = ((data.get("credits") or {}).get("crew")) or []
        cast = ((data.get("credits") or {}).get("cast")) or []
        external = data.get("external_ids") or {}

        return {
            "id": data.get("id"),
            "kind": "tv",
            "cn": data.get("name") or original,
            "en": english,
            "original": original,
            "year": air[:4] if air else "",
            "release_date": air,
            "runtime": (data.get("episode_run_time") or [0])[0],
            "rating": round(float(data.get("vote_average") or 0), 1),
            "votes": data.get("vote_count") or 0,
            "overview": data.get("overview") or "",
            "tagline": data.get("tagline") or "",
            "genres": genres,
            "tmdbid": data.get("id"),
            "imdbid": external.get("imdb_id") or "",
            "collection": "",
            "collection_id": "",
            "poster": data.get("poster_path") or "",
            "backdrop": data.get("backdrop_path") or "",
            "directors": [p.get("name") for p in crew if p.get("job") == "Creator"][:3],
            "actors": [
                {
                    "name": p.get("name") or "",
                    "role": p.get("character") or "",
                    "thumb": p.get("profile_path") or "",
                }
                for p in cast[:12]
            ],
            "country": data.get("origin_country") or [],
            "original_language": data.get("original_language") or "",
            "seasons": [
                {
                    "number": s.get("season_number") or 0,
                    "name": s.get("name") or "",
                    "episodes": s.get("episode_count") or 0,
                }
                for s in data.get("seasons") or []
            ],
            "in_production": bool(data.get("in_production")),
        }

    # ------------------------------------------------------------------ 归一
    def _brief(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": item.get("id"),
            "kind": "movie",
            "cn": item.get("title") or "",
            "original": item.get("original_title") or "",
            "year": (item.get("release_date") or "")[:4],
            "overview": item.get("overview") or "",
            "rating": round(float(item.get("vote_average") or 0), 1),
            "poster": item.get("poster_path") or "",
            "original_language": item.get("original_language") or "",
        }

    def _normalize(self, data: dict[str, Any]) -> dict[str, Any]:
        original = (data.get("original_title") or "").strip()
        english = _english_title(data, original)

        release = data.get("release_date") or ""
        genres = [g.get("name", "") for g in data.get("genres") or []]
        crew = ((data.get("credits") or {}).get("crew")) or []
        cast = ((data.get("credits") or {}).get("cast")) or []
        collection = data.get("belongs_to_collection") or {}
        external = data.get("external_ids") or {}

        return {
            "id": data.get("id"),
            "kind": "movie",
            "cn": data.get("title") or original,
            "en": english,
            "original": original,
            "year": release[:4] if release else "",
            "release_date": release,
            "runtime": data.get("runtime") or 0,
            "rating": round(float(data.get("vote_average") or 0), 1),
            "votes": data.get("vote_count") or 0,
            "overview": data.get("overview") or "",
            "tagline": data.get("tagline") or "",
            "genres": genres,
            "tmdbid": data.get("id"),
            "imdbid": external.get("imdb_id") or "",
            "collection": collection.get("name") or "",
            "collection_id": collection.get("id") or "",
            "poster": data.get("poster_path") or "",
            "backdrop": data.get("backdrop_path") or "",
            "directors": [p.get("name") for p in crew if p.get("job") == "Director"],
            "actors": [
                {
                    "name": p.get("name") or "",
                    "role": p.get("character") or "",
                    "thumb": p.get("profile_path") or "",
                }
                for p in cast[:12]
            ],
            "country": [c.get("iso_3166_1") for c in data.get("production_countries") or []],
            "original_language": data.get("original_language") or "",
        }

    # ------------------------------------------------------------------ 图片
    def download_image(self, remote: str, dest: Path) -> bool:
        if not remote:
            return False
        url = remote if remote.startswith("http") else IMAGE + remote
        try:
            resp = self.session.get(url, timeout=TIMEOUT, stream=True)
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


def _english_title(data: dict[str, Any], original: str) -> str:
    """判断原名是不是拉丁字母；不是就去 alias 里找英文标题。"""
    if original and all(ord(ch) < 128 or not ch.isalpha() for ch in original):
        return original

    # 电影叫 titles，剧集叫 results，两种都得认
    alt = data.get("alternative_titles") or {}
    aliases = alt.get("titles") or alt.get("results") or []
    for region in ("US", "GB", "XWW"):
        for item in aliases:
            if item.get("iso_3166_1") == region and item.get("title"):
                return item["title"]
    for item in aliases:
        title = item.get("title") or ""
        if title and all(ord(ch) < 128 for ch in title):
            return title
    return original


def short_error(message: str) -> str:
    """把 requests 的长报错压成一句人话。

    `HTTPConnectionPool(host='api.themoviedb.org', port=443): Max retries exceeded
    with url: /search/movie?query=... (Caused by ConnectTimeoutError(...))`
    这种文本有几百字符，直接显示会把界面撑变形。
    """
    text = str(message or "")
    low = text.lower()
    if "timed out" in low or "timeout" in low:
        return "连接超时，检查网络或代理"
    if "max retries" in low or "connection" in low or "connectionerror" in low:
        return "无法连接，检查网络或代理"
    if "401" in text:
        return "Key 无效（401）"
    if "429" in text:
        return "被限流（429），稍后再试"
    if "404" in text:
        return "接口不存在（404）"
    text = text.strip().splitlines()[0] if text.strip() else "未知错误"
    return text[:40]


def test_key(api_key: str) -> tuple[bool, str]:
    """设置界面用：验证凭据是否可用。"""
    try:
        client = TMDBClient(api_key)
        results = client.search("Avatar", "2009")
    except TMDBError as exc:
        return False, short_error(exc)
    if results:
        return True, f"连接正常，示例结果：{results[0]['cn']}（{results[0]['year']}）"
    return True, "连接正常，但示例搜索无结果"

"""Kodi / Jellyfin / Emby 通用的 movie.nfo 生成与删除。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

NFO_NAME = "movie.nfo"


def _text(value: Any) -> str:
    return escape(str(value or "").strip())


def build_nfo(meta: dict[str, Any], art: bool = True) -> str:
    """生成标准 Kodi movie nfo。图片用相对文件名，换机器也不会失效。"""
    lines: list[str] = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', "<movie>"]

    title = meta.get("cn") or meta.get("en") or ""
    original = meta.get("en") or meta.get("original") or ""

    lines.append(f"  <title>{_text(title)}</title>")
    if original:
        lines.append(f"  <originaltitle>{_text(original)}</originaltitle>")
    if title:
        lines.append(f"  <sorttitle>{_text(title)}</sorttitle>")
    if meta.get("year"):
        lines.append(f"  <year>{_text(meta['year'])}</year>")
    if meta.get("release_date"):
        lines.append(f"  <premiered>{_text(meta['release_date'])}</premiered>")
    if meta.get("runtime"):
        lines.append(f"  <runtime>{int(meta['runtime'])}</runtime>")
    if meta.get("rating"):
        lines.append(f"  <rating>{meta['rating']:.1f}</rating>")
    if meta.get("votes"):
        lines.append(f"  <votes>{int(meta['votes'])}</votes>")
    if meta.get("tagline"):
        lines.append(f"  <tagline>{_text(meta['tagline'])}</tagline>")
    if meta.get("overview"):
        lines.append(f"  <plot>{_text(meta['overview'])}</plot>")
        lines.append(f"  <outline>{_text(meta['overview'][:200])}</outline>")

    for genre in meta.get("genres") or []:
        lines.append(f"  <genre>{_text(genre)}</genre>")
    for director in meta.get("directors") or []:
        lines.append(f"  <director>{_text(director)}</director>")

    if meta.get("tmdbid"):
        lines.append(f"  <tmdbid>{_text(meta['tmdbid'])}</tmdbid>")
    if meta.get("imdbid"):
        lines.append(f"  <imdbid>{_text(meta['imdbid'])}</imdbid>")
    if meta.get("tmdbid"):
        lines.append(f'  <uniqueid type="tmdb" default="true">{_text(meta["tmdbid"])}</uniqueid>')
    if meta.get("imdbid"):
        lines.append(f'  <uniqueid type="imdb">{_text(meta["imdbid"])}</uniqueid>')

    lines.append("  <status>Released</status>")
    for country in (meta.get("country") or [])[:3]:
        if country:
            lines.append(f"  <country>{_text(country)}</country>")

    for index, actor in enumerate(meta.get("actors") or []):
        if not actor.get("name"):
            continue
        lines.append("  <actor>")
        lines.append(f"    <name>{_text(actor['name'])}</name>")
        lines.append(f"    <role>{_text(actor.get('role', ''))}</role>")
        lines.append(f"    <order>{index}</order>")
        lines.append("  </actor>")

    if art:
        lines.append("  <art>")
        lines.append("    <poster>poster.jpg</poster>")
        lines.append("    <fanart>fanart.jpg</fanart>")
        lines.append("  </art>")

    lines.append("</movie>")
    return "\n".join(lines) + "\n"


def write_nfo(folder: Path, meta: dict[str, Any], name: str = NFO_NAME, art: bool = True) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    target.write_text(build_nfo(meta, art=art), encoding="utf-8")
    return target


def find_nfos(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.nfo") if p.is_file())


def delete_nfos(paths: list[Path], backup_dir: Path | None = None) -> tuple[int, int]:
    """返回 (已删除, 已备份)。给了 backup_dir 就先复制过去，不动原数据。"""
    deleted = 0
    backed = 0
    for path in paths:
        try:
            if backup_dir is not None:
                backup_dir.mkdir(parents=True, exist_ok=True)
                rel = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", str(path.parent))[-60:]
                target = backup_dir / f"{rel}_{path.name}"
                target.write_bytes(path.read_bytes())
                backed += 1
            path.unlink()
            deleted += 1
        except OSError:
            continue
    return deleted, backed

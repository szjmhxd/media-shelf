"""配置读写：全部落在用户配置目录，不污染程序目录。"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

APP_NAME = "MediaShelf"
APP_VERSION = "0.3.0"

# 第二段用 {orig}：日本片出日文原名，英语片出英文名，中文片退化成英文名，
# 由 naming.language_original() 按 TMDB 的 original_language 决定。
DEFAULT_TEMPLATE = "{cn}.{orig}.{year}.{source}.{res}.{codec}.{hdr}.{audio}-{group}"
DEFAULT_FOLDER_TEMPLATE = "{cn} ({year})"

# 剧集走 TVDB/Jellyfin 惯例的 SxxExx，文件放 Season 子目录
DEFAULT_EP_TEMPLATE = "{cn}.{orig}.S{season}E{episode}.{res}.{codec}"
DEFAULT_SPECIAL_TEMPLATE = "{cn}.S{season}E{episode}.{ep_title}"
DEFAULT_EP_FOLDER_TEMPLATE = "{cn} ({year})/Season {season}"

PRESETS: list[tuple[str, str, str]] = [
    ("媒体库简洁（推荐刮削）", "{cn} ({year})", "{cn} ({year})"),
    (
        "多语言原名详细",
        "{cn}.{orig}.{year}.{source}.{res}.{codec}.{hdr}.{audio}-{group}",
        "{cn} ({year})",
    ),
    (
        "中英双语详细",
        "{cn}.{en}.{year}.{source}.{res}.{codec}.{hdr}.{audio}-{group}",
        "{cn} ({year})",
    ),
    ("标准场景命名", "{en}.{year}.{res}.{source}.{codec}-{group}", "{cn} ({year})"),
    ("仅英文", "{en} ({year})", "{en} ({year})"),
]

DEFAULTS: dict[str, Any] = {
    "tmdb_key": "",
    # v3 的 api_key 与 v4 的 Bearer Token 都支持，长度判断即可
    "language": "zh-CN",
    "ffprobe": "",
    # 并发探测路数：NAS/机械盘 4 路最优，SSD 可调到 8
    "probe_workers": 4,
    "template": DEFAULT_TEMPLATE,
    "folder_template": DEFAULT_FOLDER_TEMPLATE,
    "collection_suffix": "Collection",
    "group_collections": True,
    "download_images": True,
    "write_nfo": True,
    "keep_nfo_backup": True,
    "video_exts": [".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".iso", ".mov", ".wmv", ".rmvb", ".flv"],
    "sub_exts": [".ass", ".srt", ".ssa", ".sub", ".sup", ".vtt"],
    "recursive": True,
    "last_dir": "",
}


def config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_dir() -> Path:
    path = config_dir() / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_dir() -> Path:
    path = config_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


class Config:
    def __init__(self) -> None:
        self.path = config_dir() / "config.json"
        self._data: dict[str, Any] = dict(DEFAULTS)
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    self._data.update({k: v for k, v in saved.items() if k in DEFAULTS})
            except (OSError, ValueError):
                pass

    def save(self) -> None:
        self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def update(self, values: dict[str, Any]) -> None:
        self._data.update(values)

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)


def find_ffprobe(configured: str = "") -> str:
    """按 配置 → PATH → 常见安装位置 的顺序找 ffprobe。"""
    if configured and Path(configured).exists():
        return configured

    found = shutil.which("ffprobe")
    if found:
        return found

    candidates = [
        Path("C:/ffmpeg/bin/ffprobe.exe"),
        Path("C:/Program Files/ffmpeg/bin/ffprobe.exe"),
        Path("D:/environment/ffmpeg/bin/ffprobe.exe"),
        Path("D:/ffmpeg/bin/ffprobe.exe"),
        Path("/usr/bin/ffprobe"),
        Path("/usr/local/bin/ffprobe"),
        Path("/opt/homebrew/bin/ffprobe"),
    ]
    for item in candidates:
        if item.exists():
            return str(item)
    return ""


def find_ffmpeg(ffprobe_path: str) -> str:
    if not ffprobe_path:
        return ""
    sibling = Path(ffprobe_path).with_name("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if sibling.exists():
        return str(sibling)
    plain = Path(ffprobe_path).with_name("ffmpeg" + (".exe" if os.name == "nt" else ""))
    return str(plain) if plain.exists() else ""

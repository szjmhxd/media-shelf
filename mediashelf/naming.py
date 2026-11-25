"""命名模板引擎与文件名清洗。

字段用 `{name}` 占位，渲染后做一次归一化：
· 空字段留下的连续分隔符会被压掉
· 中英文重复时只保留中文
· 非法字符（: / \\ * ? " < > |）全部替换掉
"""

from __future__ import annotations

import re
from pathlib import Path

FIELD_DOCS: list[tuple[str, str, str]] = [
    ("{cn}", "中文片名", "阿凡达"),
    ("{en}", "原始/英文片名", "Avatar"),
    ("{year}", "上映年份", "2009"),
    ("{res}", "分辨率", "2160p"),
    ("{source}", "片源", "BluRay"),
    ("{codec}", "编码", "x265.10bit"),
    ("{hdr}", "HDR 标记", "DV.HDR"),
    ("{audio}", "最佳音轨", "TrueHD.7.1.Atmos"),
    ("{group}", "发布组", "DreamHD"),
    ("{edition}", "版本标记", "EXTENDED"),
    ("{collection}", "系列名", "阿凡达"),
    ("{ext}", "扩展名", "mkv"),
]

ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MULTI_DOT = re.compile(r"\.{2,}")
MULTI_SPACE = re.compile(r"\s{2,}")

TAG_WORDS = {
    "bluray",
    "blu-ray",
    "bdrip",
    "brrip",
    "bd",
    "webrip",
    "web-dl",
    "webdl",
    "web",
    "hdtv",
    "hdrip",
    "dvdrip",
    "remux",
    "uhd",
    "bd2160p",
    "hq",
    "proper",
    "repack",
    "extended",
    "remastered",
    "unrated",
    "imax",
    "multi",
    "dubbed",
    "subbed",
    "x264",
    "x265",
    "h264",
    "h265",
    "hevc",
    "avc",
    "av1",
    "10bit",
    "8bit",
    "hdr",
    "hdr10",
    "dovi",
    "dv",
    "sdr",
    "atmos",
    "dts",
    "dts-hd",
    "truehd",
    "ddp",
    "dd",
    "ac3",
    "eac3",
    "aac",
    "flac",
    "5.1",
    "7.1",
    "2.0",
    "60fps",
    "120fps",
    "24fps",
    "2160p",
    "1080p",
    "720p",
    "4k",
    "2audio",
    "3audio",
    "5audio",
    "中字",
    "中英",
    "国语",
    "双语",
    "特效字幕",
    "注释中英字幕",
    "hdcam",
    "cam",
    "ts",
    "tc",
}


def sanitize(text: str) -> str:
    text = ILLEGAL.sub(" ", text or "")
    return text.strip()


def render(template: str, fields: dict[str, str], is_filename: bool = True) -> str:
    """渲染模板并归一化。"""
    values = {key: sanitize(str(val or "")) for key, val in fields.items()}
    values.setdefault("ext", "")

    try:
        text = template.format_map(_SafeDict(values))
    except (ValueError, IndexError, KeyError):
        text = template

    text = text.replace("\u3000", " ")
    if is_filename:
        # 点分隔的模板里，标题内部的空格也统一换成点，
        # 避免「Dune Part Two」混在 Dune.Part.Two 这种结构里。
        dotted = "." in template and " (" not in template
        if dotted:
            text = re.sub(r"\s+", ".", text)
        text = MULTI_DOT.sub(".", text)
        text = re.sub(r"\s*\.\s*", ".", text)
        text = re.sub(r"\.-", "-", text)
        text = MULTI_SPACE.sub(" ", text)
        text = text.strip(" .-_")
    else:
        text = MULTI_SPACE.sub(" ", text)
        text = text.strip(" .")

    return text


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:  # noqa: D105
        return ""


def build_fields(record: dict) -> dict[str, str]:
    """把一条记录（探测结果 + 元数据）摊平成模板字段。"""
    probe = record.get("probe")
    meta = record.get("meta") or {}

    cn = (meta.get("cn") or "").strip()
    en = (meta.get("en") or "").strip()
    if not cn:
        cn = en or Path(record.get("path", "")).stem
    if not en:
        en = meta.get("original") or ""
    if en and en.strip().lower() == cn.strip().lower():
        en = ""

    fields = {
        "cn": cn,
        "en": en,
        "year": str(meta.get("year") or ""),
        "res": getattr(probe, "res", "") or "",
        "source": record.get("source") or (probe.source_guess() if probe else ""),
        "codec": getattr(probe, "codec", "") or "",
        "hdr": getattr(probe, "hdr", "") or "",
        "audio": getattr(probe, "best_audio", "") or "",
        "group": record.get("group") or "",
        "edition": record.get("edition") or "",
        "collection": (meta.get("collection") or "").strip(),
    }
    return fields


def render_filename(record: dict, template: str, ext: str | None = None) -> str:
    fields = build_fields(record)
    suffix = (ext if ext is not None else Path(record.get("path", "")).suffix).lstrip(".")
    fields["ext"] = ""
    name = render(template, fields, is_filename=True)
    if not name:
        name = Path(record.get("path", "")).stem
    return f"{name}.{suffix}" if suffix else name


def render_folder(record: dict, template: str) -> str:
    fields = build_fields(record)
    name = render(template, fields, is_filename=False)
    return name or Path(record.get("path", "")).stem


GENERIC_DIRS = {
    "电影",
    "影片",
    "影视",
    "视频",
    "下载",
    "合集",
    "媒体",
    "媒体库",
    "movie",
    "movies",
    "film",
    "films",
    "video",
    "videos",
    "media",
    "downloads",
}


def guess_query(path: str | Path) -> tuple[str, str]:
    """从目录名或文件名里提取搜索词与年份。

    优先用父目录名（整理过的库目录名最干净），但父目录是「电影」这类
    通用容器时跳过，否则会把整个库目录名当成片名去搜。
    """
    path = Path(path)
    candidates = [path.parent.name, path.stem]
    fallback: tuple[str, str] | None = None
    for raw in candidates:
        if raw.strip().lower() in GENERIC_DIRS:
            continue
        query, year = _parse_title(raw)
        if not query:
            continue
        if year:
            return query, year
        if fallback is None:
            fallback = (query, year)
    if fallback:
        return fallback
    return _parse_title(path.stem)


def _parse_title(raw: str) -> tuple[str, str]:
    text = raw
    # 站点前缀 [xxx]、【xxx】
    text = re.sub(r"[\[【][^\]】]*[\]】]", " ", text)
    text = re.sub(r"@[\w\u4e00-\u9fff]+", " ", text)

    year = ""
    match = re.search(r"(19\d{2}|20\d{2})", text)
    if match:
        year = match.group(1)
        text = text[: match.start()]

    # 中文名优先：取年份前第一段中文
    cn = re.search(r"[\u4e00-\u9fff][\u4e00-\u9fff0-9：:·、\s]*", text)
    if cn:
        name = cn.group(0).strip(" .-_·:：")
        name = re.sub(r"\s+", " ", name)
        if len(name) >= 2:
            return name, year

    # 否则按点/下划线切开，去掉技术词
    parts = re.split(r"[._\s]+", text)
    kept = [p for p in parts if p and p.lower() not in TAG_WORDS]
    name = " ".join(kept).strip(" -")
    return name, year


def detect_edition(raw: str) -> str:
    """从原文件名里认出 EXTENDED / PROPER 这类版本标记。"""
    upper = raw.upper()
    for tag in ("EXTENDED", "UNRATED", "REMASTERED", "IMAX", "PROPER", "REPACK", "DC", "CUT"):
        if re.search(rf"\b{tag}\b", upper):
            return "DC" if tag == "CUT" else tag
    return ""


def detect_group(raw: str) -> str:
    """取最后一个 `-` 后面的发布组，过滤掉常见误判。"""
    stem = Path(raw).stem
    stem = re.sub(r"\s*\(\d\)\s*$", "", stem).strip()
    if "-" not in stem:
        return ""
    tail = stem.rsplit("-", 1)[-1].strip()
    if not tail or len(tail) > 24:
        return ""
    if re.fullmatch(r"[\d.]+", tail):
        return ""
    if tail.lower() in TAG_WORDS:
        return ""
    return tail

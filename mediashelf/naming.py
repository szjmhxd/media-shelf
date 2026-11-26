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
    ("{orig}", "语言原名（日文/英文）", "Avatar"),
    ("{en}", "英文片名", "Avatar"),
    ("{year}", "上映年份", "2009"),
    ("{res}", "分辨率", "2160p"),
    ("{source}", "片源", "BluRay"),
    ("{codec}", "编码", "x265.10bit"),
    ("{hdr}", "HDR 标记", "DV.HDR"),
    ("{audio}", "最佳音轨", "TrueHD.7.1.Atmos"),
    ("{group}", "发布组", "DreamHD"),
    ("{edition}", "版本标记", "EXTENDED"),
    ("{collection}", "系列名", "阿凡达"),
    ("{season}", "季（剧集）", "01"),
    ("{episode}", "集（剧集）", "02"),
    ("{ep_title}", "集标题（剧集）", "那场决斗"),
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

    # 空字段留下的空括号直接去掉：「碧蓝之海真人版 ()」要比「碧蓝之海真人版」难看得多
    text = re.sub(r"[\(（]\s*[\)）]", "", text)

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
    """把一条记录（探测结果 + 元数据）摊平成模板字段。

    元数据缺失时按文件名就地解析片名与年份——不联网也应该能整理出干净结构，
    否则「奥本海默 (2023).mkv」会被当成整段片名塞进模板。
    """
    probe = record.get("probe")
    meta = record.get("meta") or {}
    path = Path(record.get("path", ""))

    cn = (meta.get("cn") or "").strip()
    en = (meta.get("en") or "").strip()
    year = str(meta.get("year") or "").strip()

    if not cn:
        stem_title, stem_year = _parse_title(path.stem)
        dir_title, dir_year = ("", "")
        if path.parent.name.strip().lower() not in GENERIC_DIRS:
            dir_title, dir_year = _parse_title(path.parent.name)
        cn = stem_title or dir_title or path.stem
        if not year:
            year = stem_year or dir_year
    if en.strip().lower() == cn.strip().lower():
        en = ""

    fields = {
        "cn": cn,
        "en": en,
        "orig": language_original(meta, cn, en),
        "year": year,
        "res": getattr(probe, "res", "") or "",
        "source": record.get("source") or (probe.source_guess() if probe else ""),
        "codec": getattr(probe, "codec", "") or "",
        "hdr": getattr(probe, "hdr", "") or "",
        "audio": getattr(probe, "best_audio", "") or "",
        "group": record.get("group") or "",
        "edition": record.get("edition") or "",
        "collection": (meta.get("collection") or "").strip(),
        # special 的 season 是 0（Season 00/Specials），不能当 falsy 丢掉
        "season": (
            "00"
            if record.get("kind") == "special"
            else (str(record.get("season") or "").zfill(2) if record.get("season") else "")
        ),
        "episode": (
            str(record.get("episode") or 0).zfill(2)
            if record.get("kind") == "special" or record.get("episode")
            else ""
        ),
        "ep_title": (meta.get("ep_title") or record.get("ep_title") or "").strip(),
    }
    return fields


def language_original(meta: dict, cn: str = "", en: str = "") -> str:
    """按作品的语言挑第二段名字。

    日本动画 → 日文原名（ぐらんぶる），英语片 → 英文名（Zootopia），
    韩国片 → 韩文原名。判断依据是 TMDB 的 original_language 字段，
    不猜文本里有没有假名——纯汉字的日文片名是存在的。

    中文作品（original_language 以 zh 开头）的原名就是中文，会和 {cn}
    重复，此时退化成英文名，否则文件名里会出来两遍一样的字。
    """
    language = (meta.get("original_language") or "").strip().lower()
    original = (meta.get("original") or "").strip()
    cn = cn or (meta.get("cn") or "").strip()
    en = en or (meta.get("en") or "").strip()

    if language.startswith("zh"):
        picked = en
    else:
        picked = original or en
    # 原名和中文名撞了（日文用汉字名时会发生），退回英文名；
    # 连英文名也一样就只能留空，宁可少一段也不要重复写两遍
    if picked.strip().lower() == cn.strip().lower():
        picked = en
    return "" if picked.strip().lower() == cn.strip().lower() else picked


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
    "电视剧",
    "剧集",
    "电视",
    "tv",
    "tvs",
    "series",
    "show",
    "shows",
    "anime",
    "动画",
    "动漫",
    "番剧",
    "综艺",
    "纪录片",
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


# 剧集目录里常见的「容器名」，不是片名。从匹配点吃到末尾。
# 「2K 修复」「蓝光修复」这类尾巴也要剥，否则查询词变成「武林外传 2K 修复」搜不出正主。
_SERIES_NOISE = re.compile(
    r"(日语|简中|简日|双语|国语|粤语|特效字幕|B站|字幕|内封|外挂"
    r"|4K|2K|1080P|2160P|1080p|2160p|HDR|SDR|DV|杜比|高码|抢先|修复|蓝光|版)"
    r".*$"
)
# 方括号内容里的噪音段：[人民公仆][全1-3季][英语中字][1080P蓝光][42G] 这种
# 整段信息都包在括号里的命名，剥光后没词可搜，得回头从括号里挑剧名。
_NOISE_BRACKET = re.compile(
    r"(字幕|中字|国语|英语|日语|韩语|粤语|双语"
    r"|全\s*\d|共\s*\d|第.+[季部话話集]"
    r"|\d+\s*[PG]\b|\d+[PpKk]\b|蓝光|高清|修复|收藏版|合集|\d+\s*[Gg]\b)"
)
# 「第二季」「S2」「Season 01」都是容器名尾巴，不是剧名。
# （只认 S+数字、不认裸 S 结尾，否则「KIDS」会被啃掉尾巴。）
_SEASON_TAIL = re.compile(
    r"(第\s*[一二三四五六七八九十\d]+\s*[季部].*|[Ss](?:eason)?\s*\d{1,2}\s*)$"
)
# 「Dan Da Dan - 01-12」「Dan Da Dan S2 - 01」这类尾巴是集号范围，不是剧名
_EP_RANGE = re.compile(r"[\s._-]*[Ss]?\d{1,3}\s*-\s*\d{1,3}\s*$|[\s._-]+\d{1,3}\s*$")


def _bracket_query(raw: str) -> str:
    """从方括号内容里挑剧名。

    常规命名里方括号是字幕组噪音（[DBD-Raws]、[IQIYI WebRip]），剥掉就行；
    但 [人民公仆][全1-3季][英语中字][1080P蓝光][42G] 这种整段信息都包在括号里，
    剥光后没词可搜。只在剥光后没词时才走这里，取第一个像剧名的中文段。
    """
    for seg in re.findall(r"[\[【]([^\]】]+)[\]】]", raw):
        seg = seg.strip(" .-_·:：")
        if not seg or not re.search(r"[一-鿿]", seg):
            continue
        if _NOISE_BRACKET.search(seg):
            continue
        if len(seg) >= 2:
            return seg
    return ""


def guess_series_query(path: str | Path) -> tuple[str, str]:
    """剧集的搜索词：文件名是「01.flv」「第01话…」时没有片名，得往上层目录找。

    典型情况：
      全职猎人/全职猎人1999/01.flv          → 全职猎人 (1999)
      碧蓝之海/第一季/xxx[01].mkv            → 碧蓝之海
      胆大党S01/日语特效字幕.B站版.4K/第01话.mp4 → 胆大党
    """
    path = Path(path)
    for parent in (path.parent, path.parent.parent, path.parent.parent.parent):
        raw = parent.name
        # 「_DT5RR~R」这类 Windows 8.3 短名残留不是剧名，别拿去搜
        if not raw or raw.strip().lower() in GENERIC_DIRS or "~" in raw:
            continue
        # 顺序要紧：先去站点括号（[Feibanyama]、[IQIYI WebRip …]），
        # 否则「2160p」这类噪音词会从括号内部把后面整段吃掉
        cleaned = re.sub(r"[\[【][^\]】]*[\]】]", " ", raw)
        cleaned = _SEASON_TAIL.sub("", cleaned)
        cleaned = _EP_RANGE.sub("", cleaned)
        cleaned = _SERIES_NOISE.sub("", cleaned)
        cleaned = cleaned.strip(" .-_·:：")
        # 整理过的库目录是「剧名 (年)/Season 01/…」，Season 层是容器不是剧名；
        # 当成查询词去搜会匹配到毫不相干的片（拿「Season」搜出过《寻爱交配季》）。
        if not cleaned or re.fullmatch(r"(?i)seasons?|specials?|extras?", cleaned):
            bracket = _bracket_query(raw)
            if bracket:
                _, year = _parse_title(raw)
                return bracket, year
            continue
        query, year = _parse_title(cleaned)
        if query and len(query) >= 2:
            return query, year
    return guess_query(path)


def _parse_title(raw: str) -> tuple[str, str]:
    text = raw
    # 站点前缀 [xxx]、【xxx】
    text = re.sub(r"[\[【][^\]】]*[\]】]", " ", text)
    text = re.sub(r"@[\w\u4e00-\u9fff]+", " ", text)
    # 「W 国剧 武林外传」「国剧 家有儿女」这类发行前缀，不是片名
    text = re.sub(r"^[\s\-_.]*(?:W\s*国剧|国剧)\s*", "", text)

    # 年份要从「末尾括号」或「最后一个独立年份」里取，不能取第一个四位数：
    # 《2012》这种片名本身以年份开头，取第一个会得到空片名。
    year = ""
    tail = re.search(r"[\(\[]\s*((?:19|20)\d{2})\s*[\)\]]\s*$", text)
    if tail:
        year = tail.group(1)
        text = text[: tail.start()]
    else:
        for match in re.finditer(r"(?<![0-9])((?:19|20)\d{2})(?![0-9])", text):
            head = text[: match.start()].strip(" .-_·:：")
            if head:
                year = match.group(1)
                text = head
        # 没有可用前文（如「2012.mkv」），年份视为片名的一部分

    # 中文名优先：按分隔符切段，取第一段含中文的整段。
    # 不能只匹配"连续中文"，否则「名侦探柯南VS怪盗基德」会在 VS 处被截成
    # 「名侦探柯南」，「F1：狂飙飞车」会丢掉 F1 前缀。
    segments = [seg for seg in re.split(r"[._\[\]()（）]+", text) if seg.strip()]
    for seg in segments:
        if re.search(r"[\u4e00-\u9fff]", seg):
            name = seg.strip(" .-_·:：")
            name = re.sub(r"\s+", " ", name)
            if len(name) >= 2:
                return name, year

    # 否则按点/下划线切开，去掉技术词
    parts = re.split(r"[._\s]+", text)
    kept = [p for p in parts if p and p.lower() not in TAG_WORDS]
    name = " ".join(kept).strip(" -")
    return name, year


# TMDB 标题里不会带「真人版」这类媒介后缀，留着搜不到任何东西。
# 「碧蓝之海真人版」在 TMDB 上的条目就叫《碧蓝之海》（ぐらんぶる，2020 电影）。
_EDITION_TAIL = re.compile(
    r"(?:真人版|动画版|動畫版|動画版|剧场版|劇場版|电影版|電視版|电视版"
    r"|Live[\s._-]?Action)$",
    re.IGNORECASE,
)


def strip_edition_suffix(query: str) -> str:
    """「碧蓝之海真人版」→「碧蓝之海」；没有后缀就原样返回。"""
    return _EDITION_TAIL.sub("", (query or "").strip()).strip(" .-_·:：")


def _title_part(raw: str) -> str:
    """年份之前、去掉站点括号之后的那截才是片名材料，后面的全是发布参数。"""
    text = re.sub(r"[\[【][^\]】]*[\]】]", " ", raw)
    match = re.search(r"(?<![0-9])(?:19|20)\d{2}(?![0-9])", text)
    return text[: match.start()] if match else text


def _norm_key(text: str) -> str:
    return re.sub(r"[^0-9a-z一-鿿]+", "", (text or "").lower())


def query_variants(path: str | Path, primary: str = "") -> list[str]:
    """文件名里的其他标题段，用作补充搜索词（最多 2 个）。

    「长发公主.Tangled.2010…」只拿中文段搜索时，TMDB 返回的是
    《芭比之长发公主》——《魔发奇缘》的官方译名和「长发公主」对不上，
    中文查询词根本搜不到它。文件名里的英文/日文原名段补搜一轮才能命中。
    这里把年份之前的连续拉丁文段拼成查询词；中文段归主查询词管。
    """
    path = Path(path)
    seen = {_norm_key(primary)}
    out: list[str] = []
    for raw in (path.stem, path.parent.name):
        segments = [seg.strip(" .-_·:：") for seg in re.split(r"[._\[\]()（）]+", _title_part(raw))]
        run: list[str] = []
        for seg in segments:
            if not seg:
                continue
            is_word = (
                re.fullmatch(r"[A-Za-z][A-Za-z0-9'&:：.-]*", seg) is not None
                and seg.lower() not in TAG_WORDS
                # S01E01 这类集号是拉丁文但不是标题，别拿去当查询词
                and re.fullmatch(r"[Ss]\d{1,3}[Ee][Pp]?\d{1,3}", seg) is None
                and re.fullmatch(r"[Ss](?:eason)?\s*\d{1,2}", seg) is None
            )
            if is_word:
                run.append(seg)
                continue
            if run:
                word = " ".join(run)
                if len(word) >= 3 and _norm_key(word) not in seen:
                    seen.add(_norm_key(word))
                    out.append(word)
                run = []
        if run:
            word = " ".join(run)
            if len(word) >= 3 and _norm_key(word) not in seen:
                seen.add(_norm_key(word))
                out.append(word)
    return out[:2]


def detect_edition(raw: str) -> str:
    """从原文件名里认出 EXTENDED / PROPER 这类版本标记。"""
    upper = raw.upper()
    for tag in ("EXTENDED", "UNRATED", "REMASTERED", "IMAX", "PROPER", "REPACK", "DC", "CUT"):
        if re.search(rf"\b{tag}\b", upper):
            return "DC" if tag == "CUT" else tag
    return ""


# ------------------------------------------------------------------ 剧集集号
# 剧集文件名里的集号格式五花八门。实测一个库 333 个视频里 SxxExx 出现 0 次，
# 反倒是「01.flv」「[01]」「第01话」这种到处都是。四种都得认。

# 只挡真正的技术参数。千万别把 1、2、3 放进来——那就是第一集第二集。
# 分辨率是四位数，方括号模式的 \d{2,3} 本来也吃不下，这里的集合是第二道保险。
_NOISE_NUMBERS = {
    1080,
    2160,
    720,
    480,
    576,
    3840,
    1920,
    1440,  # 分辨率
    264,
    265,  # x264 / x265
}

_EP_PATTERNS: list[tuple[re.Pattern, str]] = [
    # S01E02 / S1.E2 / s01e02
    (re.compile(r"[Ss](\d{1,2})\s*[._ -]?\s*[Ee][Pp]?(\d{1,3})"), "sxxexx"),
    # EP04 / EP 04 / Ep.04 ——「老九门.EP04.2016」这种。EP 是明确的集号标记，
    # 不会和片名里的 E 开头单词冲突（要求 EP 两字母）
    (re.compile(r"\bEP\.?\s*(\d{1,3})\b"), "tag"),
    # 第01话 / 第 2 集 / 第3話
    (re.compile(r"第\s*(\d{1,3})\s*[集话話]"), "cn"),
    # 第5期上 / 第5期 —— 综艺的「期」。上下折算成集号粗值（上=N*2-1 下=N*2），
    # 真集号由综艺匹配引擎对 TMDB 集表修正；这里只为凑够 classify 的集号判定，
    # 否则「第N期」文件全解不出集号、整目录会被误判成电影（同 EP04 的老坑）
    (re.compile(r"第\s*(\d{1,3})\s*期\s*(上|下)?"), "variety"),
    # [E01] / 【Ep02】
    (re.compile(r"[\[【(（]\s*[Ee][Pp]?(\d{1,3})\s*[\]】)）]"), "tag"),
    # [01] / 【12】 —— 方括号里两位数，最常见的字幕组格式
    (re.compile(r"[\[【(（]\s*(\d{2,3})\s*[\]】)）]"), "bracket"),
    # 01.flv / 01 - 开头。注意这里吃原名不吃 stem：
    # 「01.flv」去掉扩展名就成了「01」，结尾没有分隔符可匹配。
    (re.compile(r"^\s*(\d{1,3})\s*[._ -]"), "leading"),
    # 纯数字的文件名：01.flv → 01
    (re.compile(r"^\s*(\d{1,3})\s*$"), "leading"),
    # 集号在末尾：HUNTER×HUNTER(2011) 001.mkv → 001
    # 必须两位以上，否则「阿凡达.2.mkv」这类续集编号会被当成集号
    (re.compile(r"[\s._-](\d{2,3})\s*$"), "trailing"),
    # 数字被分隔符夹着：Dan Da Dan - 01 [IQIYI...] → 01。
    # 265 这类编码数字靠 >200 的上限挡掉，10bit 不匹配是因为后面没有分隔符
    (re.compile(r"[\s._-](\d{2,3})[\s._\[(（【]"), "mid"),
]

_CN_DIGITS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
}

_SEASON_PATTERNS: list[re.Pattern] = [
    # 「第二季」「第 2 部」——中文数字在中文字幕组命名里比阿拉伯数字更常见
    re.compile(r"第\s*(\d{1,2}|十一|十二|[一二三四五六七八九十])\s*[季部]"),
    re.compile(r"[Ss]eason\s*(\d{1,2})", re.I),
    re.compile(r"[Ss](\d{1,2})\s*$"),
    re.compile(r"[Ss](\d{1,2})\s*[^Ee\d]"),
]


# ---- 综艺（variety） ----
# 「第N期上/下」「第N期」「第N期」——综艺的集号是「期」，一期拆上下两集
_VARIETY_QI = re.compile(r"第\s*(\d+)\s*期\s*(上|下)?")
# 播出日期：2026-07-25 / 2026.08.02 / 20260725（文件名前缀最常见）
_AIR_DATE = re.compile(r"(20\d{2})[.\-_/](\d{2})[.\-_/](\d{2})")
# TMDB 不收录的非正片：加更/花絮/抢先看/先导……
_SPECIAL_TOKENS = (
    "加更",
    "花絮",
    "抢先看",
    "抢先",
    "先行",
    "超前",
    "会员",
    "彩蛋",
    "未播",
    "纯享",
    "预告",
    "看点",
    "幕后",
    "番外",
    "片花",
    "体验篇",
    "尝鲜篇",
    "VLOG",
    "vlog",
    "直播回放",
)


def parse_air_date(name: str) -> str:
    """从文件名里提取播出日期，返回 'YYYY-MM-DD'；没有返回空。"""
    m = _AIR_DATE.search(name or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def parse_variety(name: str) -> dict:
    """综艺文件名解析。

    「2026-07-25 第5期上.mkv」→ qi=5 part=上 air_date=2026-07-25
    「2023-08-16 第1期加更：云南神秘食物.mkv」→ qi=1 special=True
    「2023-07-30 森林体验篇：…mkv」→ special=True（无期数的「篇」类）
    """
    text = name or ""
    stem = Path(text).stem
    qi, part = 0, ""
    m = _VARIETY_QI.search(stem)
    if m:
        qi, part = int(m.group(1)), m.group(2) or ""
    special = any(tok in text for tok in _SPECIAL_TOKENS)
    # 无期数的「XX篇」是先导/特别篇（森林体验篇、尝鲜篇）；
    # 正片标题带「篇」时有期数兜着，不会误判
    if not qi and re.search(r"[\u4e00-\u9fff]{2,4}篇", stem):
        special = True
    air_date = parse_air_date(stem)
    # 标题：去掉日期、期数、技术词后的剩余文字
    # 方括号整段是来源/体积标记（[322458_suhd_25fps_tv_3.73GB]），先删
    title = re.sub(r"[\[【][^\]】]*[\]】]", " ", stem)
    title = _AIR_DATE.sub(" ", title)
    title = _VARIETY_QI.sub(" ", title)
    title = re.sub(r"[()（）]", " ", title)
    title = re.sub(
        r"\d{3,4}[pPkK]\b|[248]K\b|x26[45]|10bit|WEB-?DL|BluRay|HDTV|HEVC|AAC"
        r"|DDP?\d*(?:\.\d)?|H\.?26[45]|高码率",
        " ",
        title,
        flags=re.I,
    )
    title = re.sub(r"[_\-.]+", " ", title).strip(" 　-:：|")
    return {"qi": qi, "part": part, "special": special, "title": title, "air_date": air_date}


def variety_expected_episode(qi: int, part: str) -> int:
    """「第N期上/下」→ TMDB 预期集号（一期拆两集：上=N*2-1 下=N*2）。

    只是预期值，TMDB 布局偶尔有例外（加更占位），最终以匹配结果为准。
    """
    if not qi:
        return 0
    base = (qi - 1) * 2
    return base + (2 if part == "下" else 1)


def parse_episode(name: str) -> tuple[int, int] | None:
    """从文件名里解出 (季, 集)。解不出返回 None。

    「[DBD-Raws][Grand Blue][01][1080P][BDRip]」这种，方括号里既有集号也有
    分辨率，靠 _NOISE_NUMBERS 挡住 1080 之类。
    """
    text = name or ""
    stem = Path(text).stem
    for pattern, kind in _EP_PATTERNS:
        # leading 系列要看到「01.flv」里的点，只能用原名
        subject = text if kind == "leading" else stem
        for match in pattern.finditer(subject):
            if kind == "sxxexx":
                season, episode = int(match.group(1)), int(match.group(2))
            elif kind == "variety":
                qi = int(match.group(1))
                part = match.group(2) or ""
                season, episode = 0, (qi - 1) * 2 + (2 if part == "下" else 1)
            else:
                episode = int(match.group(1))
                season = 0  # 稍后由父目录补
            if episode in _NOISE_NUMBERS or episode > 200:
                continue
            # 季号优先从文件名找（「Grand Blue S2 [07]」的 S2 就在文件名里），
            # 找不到再由调用方拿父目录兜底
            if season == 0:
                season = parse_season(stem, 0)
            return season, episode
    return None


def parse_season(dir_name: str, fallback: int = 1) -> int:
    """从父目录名推季号：「第一季」「Season 02」「胆大党S01」都能认。"""
    text = (dir_name or "").strip()
    for pattern in _SEASON_PATTERNS:
        match = pattern.search(text)
        if match:
            token = match.group(1)
            value = _CN_DIGITS.get(token) if not token.isdigit() else int(token)
            if value and 1 <= value <= 50:
                return value
    return fallback


def normalize_collection(name: str) -> str:
    """把 TMDB 五花八门的合集名归一。

    同一个系列，TMDB 可能给「海扁王（系列）」，也可能给「海扁王 Collection」，
    直接拿名字拼目录就会变成两个文件夹，一个有片一个空着。
    """
    text = (name or "").strip()
    if not text:
        return ""
    text = re.sub(r"\s*[（(]\s*(?:系列|合集|全集|collection|series)\s*[)）]", "", text, flags=re.I)
    text = re.sub(r"\b(collection|series|franchise)\b", "", text, flags=re.I)
    text = re.sub(r"(系列|合集|全集)$", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -_·:")


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

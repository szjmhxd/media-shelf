"""扫描 → 生成整理计划。计划是纯数据，执行与预览共用同一份。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import naming
from . import nfo as nfo_mod

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".iso", ".mov", ".wmv", ".rmvb", ".flv"}
SUB_EXTS = {".ass", ".srt", ".ssa", ".sub", ".sup", ".vtt"}


@dataclass
class Op:
    kind: str  # move / write_nfo / download / mkdir
    src: str = ""
    dst: str = ""
    note: str = ""
    payload: dict = field(default_factory=dict)
    # 属于哪一个源视频。预览界面按它把同一部片的改名/nfo/图片合并成一行。
    owner: str = ""

    def as_row(self) -> dict[str, str]:
        return {
            "操作": OP_LABEL.get(self.kind, self.kind),
            "源": self.src,
            "目标": self.dst,
            "备注": self.note,
        }


OP_LABEL = {
    "move": "移动/改名",
    "write_nfo": "写入 nfo",
    "download": "下载图片",
    "mkdir": "新建目录",
}


def scan(root: Path, recursive: bool = True, max_depth: int = 6) -> list[Path]:
    """找出所有视频文件，跳过回收站、隐藏目录和 @eaDir 这类系统目录。"""
    skip = {"@eadir", "$recycle.bin", "system volume information", ".git", "node_modules"}
    results: list[Path] = []

    if recursive:
        stack = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            if depth > max_depth:
                continue
            try:
                entries = list(current.iterdir())
            except OSError:
                continue
            for entry in entries:
                name_lower = entry.name.lower()
                if name_lower in skip or entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    stack.append((entry, depth + 1))
                elif entry.suffix.lower() in VIDEO_EXTS:
                    results.append(entry)
    else:
        try:
            for entry in root.iterdir():
                if entry.is_file() and entry.suffix.lower() in VIDEO_EXTS:
                    results.append(entry)
        except OSError:
            pass

    return sorted(results)


def sidecars(video: Path) -> list[Path]:
    """同目录下与视频同名的字幕/图片文件。"""
    stem = video.stem
    out: list[Path] = []
    try:
        for entry in video.parent.iterdir():
            if not entry.is_file() or entry == video:
                continue
            if entry.stem == stem and entry.suffix.lower() in SUB_EXTS:
                out.append(entry)
            elif entry.name.startswith(stem + ".") and entry.suffix.lower() in SUB_EXTS:
                out.append(entry)
    except OSError:
        pass
    return sorted(out)


def build_plan(
    records: list[dict],
    root: Path,
    template: str,
    folder_template: str,
    organize: bool = True,
    group_collections: bool = True,
    collection_suffix: str = "Collection",
    write_nfo: bool = True,
    download_images: bool = True,
) -> list[Op]:
    """按记录生成计划。

    改名不依赖联网元数据：片名和年份可以从文件名就地解析，所以即使一部都没识别，
    也能把库整理成「片名 (年份)/片名 (年份).mkv」。nfo 与海报必须有元数据才会生成。
    """
    ops: list[Op] = []
    taken: dict[str, str] = {}

    for record in records:
        if not record.get("checked", True):
            continue
        meta = record.get("meta") or {}
        has_meta = bool(meta.get("cn") or meta.get("en"))

        video = Path(record["path"])
        filename = naming.render_filename(record, template)
        if not filename:
            continue

        if organize:
            folder_name = naming.render_folder(record, folder_template)
            target_dir = root / folder_name
            collection = (meta.get("collection") or "").strip()
            if group_collections and collection:
                target_dir = root / f"{collection} {collection_suffix}".strip() / folder_name
        else:
            target_dir = video.parent

        target = target_dir / filename
        record["target"] = str(target)

        key = str(target).lower()
        if key in taken:
            ops.append(
                Op("move", str(video), "", f"跳过：与 {taken[key]} 目标同名", owner=str(video))
            )
            record["status"] = "目标重名"
            continue
        taken[key] = str(video)

        note = "改名/归档" if has_meta else "改名/归档（未识别，片名取自文件名）"
        if str(video) != str(target):
            ops.append(Op("move", str(video), str(target), note, owner=str(video)))
            for extra in sidecars(video):
                suffix = extra.name[len(video.stem) :]
                ops.append(
                    Op(
                        "move",
                        str(extra),
                        str(target_dir / f"{Path(filename).stem}{suffix}"),
                        "字幕跟随",
                        owner=str(video),
                    )
                )

        if write_nfo and has_meta:
            ops.append(
                Op(
                    "write_nfo",
                    "",
                    str(target_dir / nfo_mod.NFO_NAME),
                    f"{meta.get('cn') or meta.get('en')} ({meta.get('year', '')})",
                    {"meta": meta},
                    owner=str(video),
                )
            )
            if download_images:
                if meta.get("poster"):
                    ops.append(
                        Op(
                            "download",
                            meta["poster"],
                            str(target_dir / "poster.jpg"),
                            "海报",
                            owner=str(video),
                        )
                    )
                if meta.get("backdrop"):
                    ops.append(
                        Op(
                            "download",
                            meta["backdrop"],
                            str(target_dir / "fanart.jpg"),
                            "背景图",
                            owner=str(video),
                        )
                    )

    return ops


def plan_summary(ops: list[Op]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for op in ops:
        summary[op.kind] = summary.get(op.kind, 0) + 1
    return summary

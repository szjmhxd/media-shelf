r"""N:\电视剧 批处理（一次性工具，不随版本发布）。

两阶段：
  python scripts/tv_run.py identify   扫描+探测+识别，落盘 JSON，打印统计
  python scripts/tv_run.py apply      按 JSON 生成计划并执行，只动「已匹配」行
"""

from __future__ import annotations

import dataclasses
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication  # noqa: E402

app = QCoreApplication([])  # noqa: F841

from mediashelf import apply as apply_mod  # noqa: E402
from mediashelf import cleanup, naming, organizer  # noqa: E402
from mediashelf import config as cfg  # noqa: E402
from mediashelf.config import Config  # noqa: E402
from mediashelf.probe import AudioTrack, ProbeResult, probe  # noqa: E402
from mediashelf.tmdb import TMDBClient  # noqa: E402
from mediashelf.ui.workers import IdentifyWorker, classify_media  # noqa: E402

ROOT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("N:/电视剧")
SNAPSHOT = Path(__file__).resolve().parent / (
    ".tv_records.json" if len(sys.argv) <= 2 else f".tv_records_{sys.argv[2].replace(chr(92), '/').rstrip('/').split('/')[-1]}.json"
)


def _record_from(video: Path, info: dict, ffprobe: str) -> dict:
    result = probe(video, ffprobe)
    return {
        "path": str(video),
        "name": video.name,
        "size": result.size,
        "probe": dataclasses.asdict(result),
        "meta": {},
        "source": result.source_guess(),
        "group": naming.detect_group(video.name) or naming.detect_group(video.parent.name),
        "edition": naming.detect_edition(video.name),
        "kind": info.get("kind", "movie"),
        "season": info.get("season", 0),
        "episode": info.get("episode", 0),
        "checked": True,
        "status": "待识别",
        "target": "",
    }


def _probe_back(data: dict) -> ProbeResult:
    payload = dict(data)
    audio = [AudioTrack(**item) for item in payload.pop("audio", [])]
    result = ProbeResult(**payload)
    result.audio = audio
    return result


def identify() -> None:
    config = Config()
    ffprobe = config.get("ffprobe")
    files = organizer.scan(ROOT, recursive=True)
    print(f"视频文件: {len(files)}", flush=True)
    kinds = classify_media(files)
    tv_n = sum(1 for v in kinds.values() if v["kind"] == "tv")
    print(f"剧集文件 {tv_n} / 电影文件 {len(files) - tv_n}", flush=True)

    def _one(video: Path) -> dict:
        return _record_from(video, kinds.get(str(video)) or {}, ffprobe)

    records: list[dict] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for i, rec in enumerate(pool.map(_one, files), 1):
            records.append(rec)
            if i % 100 == 0 or i == len(files):
                print(f"探测 {i}/{len(files)}", flush=True)

    client = TMDBClient(config.get("tmdb_key"))
    # 同一部剧的几十集查询词相同，搜索结果进内存缓存，别一集打一次请求
    memo: dict[tuple, list] = {}
    orig_search, orig_search_tv = client.search, client.search_tv

    def _sleepy(fn, tag):
        def wrapper(query, year=""):
            key = (tag, query, year)
            if key in memo:
                return memo[key]
            time.sleep(0.6)
            found = fn(query, year)
            memo[key] = found
            return found

        return wrapper

    client.search = _sleepy(orig_search, "m")
    client.search_tv = _sleepy(orig_search_tv, "t")

    worker = IdentifyWorker(client, records)
    captured: list = []
    worker.row_updated.connect(lambda _i, meta, st: captured.append((meta, st)))
    for idx, rec in enumerate(records, 1):
        captured.clear()
        try:
            worker._identify_one(idx, rec, 0, 0)
            meta, st = captured[0]
        except Exception as exc:  # noqa: BLE001 - 单条出错不能中断整批
            meta, st = {}, f"错误: {type(exc).__name__} {exc}"
        rec["meta"], rec["status"] = meta or {}, st
        if idx % 50 == 0 or idx == len(records):
            print(f"识别 {idx}/{len(records)}  {rec['name'][:28]} -> {st}", flush=True)
    client.save_cache()

    # 手动指定：错别字、TMDB 搜不到的条目（路径关键词 -> TMDB TV id）
    OVERRIDES = {
        "云项天宫": 128985,  # 目录错别字（云顶天宫），TMDB《盗墓笔记之云顶天宫》(2021)
    }
    for rec in records:
        # 注意不能用 rec.get("meta") 判断：无匹配时 meta 是 {"query": ...} 也非空
        if rec.get("status") == "已匹配":
            continue
        for key, tid in OVERRIDES.items():
            if key not in rec["path"]:
                continue
            full = client.tv_details(tid)
            if rec.get("season") and rec.get("episode"):
                try:
                    for item in client.season_details(tid, int(rec["season"])):
                        if item.get("number") == int(rec["episode"]):
                            full = dict(full)
                            full["ep_title"] = item.get("title") or ""
                            break
                except Exception:  # noqa: BLE001 - 集标题拿不到不影响改名
                    pass
            rec["meta"], rec["status"] = full, "已匹配"
            print(f"手动指定: {rec['name'][:30]} -> {full.get('cn')} #{tid}", flush=True)
            break
    client.save_cache()

    SNAPSHOT.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    _print_stats(records)
    print(f"快照已存: {SNAPSHOT}", flush=True)


def _print_stats(records: list[dict]) -> None:
    by_status: dict[str, int] = {}
    shows: dict[str, list] = {}
    for rec in records:
        by_status[rec["status"]] = by_status.get(rec["status"], 0) + 1
        meta = rec.get("meta") or {}
        key = f"{meta.get('cn') or rec.get('status') or '?'}|{meta.get('year') or ''}"
        shows.setdefault(key, []).append(rec)
    print("\n=== 状态汇总 ===", flush=True)
    for status, count in sorted(by_status.items(), key=lambda kv: -kv[1]):
        print(f"  {status}: {count}", flush=True)

    print("\n=== 按剧汇总 ===", flush=True)
    for key, items in sorted(shows.items(), key=lambda kv: -len(kv[1])):
        sample = items[0]
        kind = "TV" if sample["kind"] == "tv" else "电影"
        year = (sample.get("meta") or {}).get("year") or "-"
        tmdb = (sample.get("meta") or {}).get("tmdbid") or "-"
        print(f"  [{kind}] {key.split('|')[0]} ({year}) tmdb={tmdb} × {len(items)}", flush=True)

    problems = [r for r in records if r["status"] not in ("已匹配",)]
    if problems:
        print(f"\n=== 未定稿 {len(problems)} 行（不自动执行）===", flush=True)
        for rec in problems[:40]:
            print(f"  [{rec['status']}] {rec['path']}", flush=True)
        if len(problems) > 40:
            print(f"  … 另 {len(problems) - 40} 行", flush=True)


def apply_records() -> None:
    config = Config()
    records = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    for rec in records:
        rec["probe"] = _probe_back(rec["probe"]) if rec.get("probe") else None
        # 只动「已匹配」行，需人工确认/无匹配保持原地
        rec["checked"] = rec.get("status") == "已匹配"

    todo = [r for r in records if r["checked"]]
    print(f"可执行 {len(todo)} / 共 {len(records)}", flush=True)
    ops = organizer.build_plan(
        records,
        ROOT,
        template=config.get("template", cfg.DEFAULT_TEMPLATE),
        folder_template=config.get("folder_template", cfg.DEFAULT_FOLDER_TEMPLATE),
        organize=True,
        group_collections=config.get("group_collections", True),
        collection_suffix=config.get("collection_suffix", "Collection"),
        write_nfo=config.get("write_nfo", True),
        download_images=config.get("download_images", True),
        ep_template=cfg.DEFAULT_EP_TEMPLATE,
    )
    skipped = [op for op in ops if not op.dst]
    runnable = [op for op in ops if op.dst]
    print(f"计划: 可执行 {len(runnable)} 条，目标重名跳过 {len(skipped)} 条", flush=True)
    for op in runnable[:15]:
        print(f"  {op.kind}: {op.src} -> {op.dst}", flush=True)
    for op in skipped[:15]:
        print(f"  跳过: {op.src}（{op.note}）", flush=True)

    client = TMDBClient(config.get("tmdb_key")) if config.get("download_images") else None
    report = apply_mod.execute(runnable, client=client, root=ROOT)
    print(f"执行: 成功 {len(report.ok)}，失败 {len(report.failed)}", flush=True)
    for op, err in report.failed[:20]:
        print(f"  失败: {op.dst or op.src} :: {err}", flush=True)
    for item in report.residuals[:20]:
        print(f"  残留: {item}", flush=True)
    if client is not None:
        client.save_cache()

    shells = cleanup.scan(ROOT)
    removable = [c for c in shells if c.removable]
    print(f"空壳目录 {len(removable)} 个", flush=True)
    if removable:
        result = cleanup.remove(removable)
        print(f"清理: 成功 {result.ok}，失败 {len(result.failed)}", flush=True)
        for item in result.failed:
            print(f"  失败: {item}", flush=True)
    left = [r for r in records if not r["checked"]]
    if left:
        print(f"\n留在原地待人工处理 {len(left)} 行:", flush=True)
        for rec in left[:40]:
            print(f"  [{rec['status']}] {rec['path']}", flush=True)
    print("\n库目录:", flush=True)
    for p in sorted(ROOT.iterdir()):
        print(f"  {p.name}", flush=True)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "identify"
    if mode == "identify":
        identify()
    elif mode == "apply":
        apply_records()
    else:
        raise SystemExit("用法: tv_run.py identify|apply [库目录]")

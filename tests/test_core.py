"""核心逻辑自检：不依赖 PySide6，可直接 `python -m unittest` 跑。"""

from __future__ import annotations

import csv
import os
import tempfile
import unittest
from pathlib import Path

from mediashelf import apply as apply_mod
from mediashelf import cleanup, naming, nfo, organizer
from mediashelf import config as cfg
from mediashelf.probe import AudioTrack, ProbeResult
from mediashelf.ui.workers import classify_media


def _record(path: str, cn="阿凡达", en="Avatar", year="2009", **extra) -> dict:
    probe = ProbeResult(path=path)
    probe.width = 3840
    probe.height = 2160
    probe.vcodec = "hevc"
    probe.pix_fmt = "yuv420p10le"
    probe.hdr = "DV.HDR"
    probe.size = 60 * 1024**3
    probe.bitrate = 60_000_000
    probe.audio = [AudioTrack("truehd", 8, title="TrueHD Atmos 7.1")]
    record = {
        "path": path,
        "probe": probe,
        "meta": {"cn": cn, "en": en, "year": year},
        "source": "REMUX",
        "group": "DreamHD",
        "checked": True,
    }
    record.update(extra)
    return record


class NamingTest(unittest.TestCase):
    def test_full_template(self) -> None:
        name = naming.render_filename(_record("x/阿凡达 (2009).mkv"), cfg.DEFAULT_TEMPLATE)
        self.assertEqual(
            name, "阿凡达.Avatar.2009.REMUX.2160p.x265.10bit.DV.HDR.TrueHD.7.1.Atmos-DreamHD.mkv"
        )

    def test_empty_fields_collapse(self) -> None:
        record = _record("x/沙丘2.mp4", cn="沙丘2", en="Dune: Part Two", year="2024")
        record["probe"] = ProbeResult(path="x")  # 没有任何探测结果
        record["source"] = ""
        record["group"] = ""
        name = naming.render_filename(record, "{cn}.{en}.{year}.{res}.{audio}-{group}")
        self.assertEqual(name, "沙丘2.Dune.Part.Two.2024.mp4")

    def test_same_cn_en(self) -> None:
        record = _record("x/2012.mkv", cn="2012", en="2012", year="2009")
        name = naming.render_filename(record, "{cn}.{en}.{year}")
        self.assertFalse(name.startswith("2012.2012"))

    def test_illegal_chars(self) -> None:
        record = _record("x/a.mkv", cn="沙丘2: 帝国/战争*?", en="Dune")
        name = naming.render_filename(record, "{cn}.{year}")
        self.assertNotIn(":", name)
        self.assertNotIn("/", name)

    def test_folder_template(self) -> None:
        folder = naming.render_folder(_record("x/a.mkv"), "{cn} ({year})")
        self.assertEqual(folder, "阿凡达 (2009)")

    def test_guess_query_chinese(self) -> None:
        query, year = naming.guess_query("N:/电影/阿凡达 (2009)/阿凡达 (2009).mkv")
        self.assertEqual((query, year), ("阿凡达", "2009"))

    def test_guess_query_scene(self) -> None:
        query, year = naming.guess_query(
            "N:/电影/Indiana.Jones.and.the.Last.Crusade.1989.PROPER.2160p.BluRay.mkv"
        )
        self.assertEqual(year, "1989")
        self.assertIn("Indiana", query)

    def test_detect_group(self) -> None:
        self.assertEqual(
            naming.detect_group("Alita.Battle.Angel.2019.2160p-DreamHD.mkv"), "DreamHD"
        )
        self.assertEqual(naming.detect_group("阿凡达 (2009).mkv"), "")

    def test_guess_query_title_starts_with_year(self) -> None:
        """《2012》这种片名本身是年份，不能把年份当片名、片名当空。"""
        query, year = naming.guess_query("N:/电影/2012 (2009).mkv")
        self.assertEqual((query, year), ("2012", "2009"))

    def test_fields_without_meta_parse_from_filename(self) -> None:
        """没有联网元数据时，片名和年份要从文件名解析出来，不能整段塞进 {cn}。"""
        record = {"path": "N:/电影/奥本海默 (2023).mkv", "probe": None, "meta": {}}
        fields = naming.build_fields(record)
        self.assertEqual(fields["cn"], "奥本海默")
        self.assertEqual(fields["year"], "2023")

    def test_fields_ignore_generic_parent(self) -> None:
        record = {"path": "N:/电影/沙丘2.mp4", "probe": None, "meta": {}}
        fields = naming.build_fields(record)
        self.assertEqual(fields["cn"], "沙丘2")
        self.assertEqual(fields["year"], "")


class NfoTest(unittest.TestCase):
    def test_build_and_parse(self) -> None:
        import xml.etree.ElementTree as ET

        meta = {
            "cn": "阿凡达",
            "en": "Avatar",
            "year": "2009",
            "release_date": "2009-12-18",
            "runtime": 162,
            "rating": 7.6,
            "votes": 30000,
            "overview": "剧情简介",
            "tmdbid": 19995,
            "imdbid": "tt0499549",
            "genres": ["科幻"],
            "directors": ["詹姆斯·卡梅隆"],
            "actors": [{"name": "萨姆·沃辛顿", "role": "Jake"}],
        }
        xml = nfo.build_nfo(meta)
        root = ET.fromstring(xml)
        self.assertEqual(root.findtext("title"), "阿凡达")
        self.assertEqual(root.findtext("originaltitle"), "Avatar")
        self.assertEqual(root.findtext("tmdbid"), "19995")
        self.assertEqual(root.findtext("art/poster"), "poster.jpg")

    def test_escape(self) -> None:
        xml = nfo.build_nfo({"cn": "A & B <c>", "year": "2000"})
        self.assertIn("A &amp; B &lt;c&gt;", xml)


class PlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.video = self.root / "阿凡达 (2009).mkv"
        self.video.write_bytes(b"0" * 1024)
        (self.root / "阿凡达 (2009).zh.ass").write_text("sub", encoding="utf-8")
        self.record = _record(str(self.video))
        self.record["meta"]["collection"] = "阿凡达"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_plan_moves_and_sidecar(self) -> None:
        ops = organizer.build_plan(
            [self.record],
            self.root,
            template=cfg.DEFAULT_TEMPLATE,
            folder_template="{cn} ({year})",
            organize=True,
            group_collections=True,
            write_nfo=False,
        )
        moves = [op for op in ops if op.kind == "move"]
        self.assertEqual(len(moves), 2)
        target = Path(moves[0].dst)
        self.assertEqual(target.parent.name, "阿凡达 (2009)")
        self.assertEqual(target.parent.parent.name, "阿凡达 Collection")
        self.assertTrue(moves[1].dst.endswith(".zh.ass"))

    def test_collection_name_variants_merge(self) -> None:
        """同一系列在 TMDB 可能叫「海扁王（系列）」也可能叫「海扁王 Collection」，
        拼目录时必须落到同一个文件夹，否则会出现一个空目录 + 一个有片目录。"""
        self.assertEqual(naming.normalize_collection("海扁王（系列）"), "海扁王")
        self.assertEqual(naming.normalize_collection("海扁王 Collection"), "海扁王")
        self.assertEqual(naming.normalize_collection("Kick-Ass Collection"), "Kick-Ass")

        second = self.root / "海扁王2 (2013).mkv"
        second.write_bytes(b"0" * 512)
        other = _record(str(second))
        other["meta"].update({"cn": "海扁王2", "year": "2013", "collection": "海扁王 Collection"})
        self.record["meta"]["collection"] = "海扁王（系列）"
        self.record["meta"]["collection_id"] = "2344"
        other["meta"]["collection_id"] = "2344"

        ops = organizer.build_plan(
            [self.record, other],
            self.root,
            template="{cn} ({year})",
            folder_template="{cn} ({year})",
            group_collections=True,
            write_nfo=False,
        )
        dirs = {Path(op.dst).parent.parent.name for op in ops if op.kind == "move"}
        self.assertEqual(dirs, {"海扁王 Collection"}, f"合集目录不唯一：{dirs}")

    def test_unchecked_skipped(self) -> None:
        self.record["checked"] = False
        ops = organizer.build_plan(
            [self.record], self.root, template="{cn}", folder_template="{cn}", write_nfo=False
        )
        self.assertEqual(ops, [])

    def test_plan_renames_without_metadata(self) -> None:
        """一部都没识别出来时也必须能改名，否则工具在没 Key 的环境下完全没用。"""
        record = {"path": str(self.video), "checked": True, "meta": {}, "probe": None}
        ops = organizer.build_plan(
            [record],
            self.root,
            template="{cn}.{year}",
            folder_template="{cn} ({year})",
            organize=True,
            write_nfo=True,
        )
        moves = [op for op in ops if op.kind == "move"]
        self.assertTrue(moves, "没有元数据也应该生成改名操作")
        self.assertEqual(Path(moves[0].dst).parent.name, "阿凡达 (2009)")
        self.assertEqual(Path(moves[0].dst).name, "阿凡达.2009.mkv")
        # nfo 需要元数据，不能凭空生成
        self.assertFalse([op for op in ops if op.kind == "write_nfo"])

    def test_execute_and_undo(self) -> None:
        with tempfile.TemporaryDirectory() as fake_appdata:
            os.environ["APPDATA"] = fake_appdata
            os.environ["XDG_CONFIG_HOME"] = fake_appdata
            ops = organizer.build_plan(
                [self.record],
                self.root,
                template="{cn} ({year})",
                folder_template="{cn} ({year})",
                write_nfo=True,
                download_images=False,
            )
            report = apply_mod.execute(ops)
            self.assertEqual(len(report.failed), 0)
            target = Path(self.record["target"])
            self.assertTrue(target.exists())
            self.assertTrue((target.parent / "movie.nfo").exists())

            logs = list(Path(fake_appdata).rglob("执行日志_*.csv"))
            self.assertEqual(len(logs), 1)
            with logs[0].open(encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))
            self.assertTrue(all(row["结果"] == "成功" for row in rows))

            restored, removed = apply_mod.undo(logs[0])
            self.assertTrue(self.video.exists())
            self.assertEqual(restored, 2)  # 视频 + 同名字幕
            self.assertGreaterEqual(removed, 1)

    def test_residuals_detected_after_move(self) -> None:
        """片子搬走后，原目录里的海报/旧 nfo/样片要被识别出来交给用户处理。"""
        folder = self.root / "阿凡达 (2009)"
        folder.mkdir()
        video = folder / "阿凡达 (2009).mkv"
        video.write_bytes(b"0" * 1024)
        (folder / "poster.jpg").write_bytes(b"jpg")
        (folder / "movie.nfo").write_text("<movie/>", encoding="utf-8")
        (folder / "sample.mkv").write_bytes(b"0" * 2048)

        record = _record(str(video))
        record["meta"].update({"cn": "阿凡达", "year": "2009", "collection": "阿凡达"})
        ops = organizer.build_plan(
            [record],
            self.root,
            template="{cn} ({year})",
            folder_template="{cn} ({year})",
            write_nfo=False,
            download_images=False,
        )
        with tempfile.TemporaryDirectory() as fake_appdata:
            os.environ["APPDATA"] = fake_appdata
            os.environ["XDG_CONFIG_HOME"] = fake_appdata
            report = apply_mod.execute(ops, root=self.root)

        self.assertEqual(len(report.residuals), 1, report.residuals)
        names = {name for name, _size in report.residuals[0]["files"]}
        self.assertEqual(names, {"poster.jpg", "movie.nfo", "sample.mkv"})

        # 一起移走后原目录应该干净
        moved, errors = apply_mod.move_residuals(report.residuals)
        self.assertEqual(errors, [])
        self.assertEqual(moved, 3)
        self.assertEqual(list(folder.iterdir()), [])

    def test_root_level_move_makes_no_residual(self) -> None:
        """片子直接平铺在库根目录时，根目录本来就要保留，不该报残留。"""
        with tempfile.TemporaryDirectory() as fake_appdata:
            os.environ["APPDATA"] = fake_appdata
            os.environ["XDG_CONFIG_HOME"] = fake_appdata
            ops = organizer.build_plan(
                [self.record],
                self.root,
                template="{cn} ({year})",
                folder_template="{cn} ({year})",
                write_nfo=False,
                download_images=False,
            )
            report = apply_mod.execute(ops, root=self.root)
        self.assertEqual(report.residuals, [])


class CleanupTest(unittest.TestCase):
    """空目录清理：只在确实没有视频时下手。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_dir_is_candidate(self) -> None:
        empty = self.tmp / "空壳 (2019)"
        empty.mkdir()
        found = cleanup.scan(self.tmp)
        self.assertEqual([c.path.name for c in found], ["空壳 (2019)"])
        self.assertTrue(found[0].removable)
        self.assertEqual(found[0].file_count, 0)

    def test_dir_with_video_is_never_a_candidate(self) -> None:
        keeper = self.tmp / "正片 (2020)"
        keeper.mkdir()
        (keeper / "movie.mkv").write_bytes(b"x")
        found = cleanup.scan(self.tmp)
        self.assertEqual(found, [])

    def test_poster_only_dir_is_candidate(self) -> None:
        # 整理后常见的残留：片子走了，海报和旧 nfo 还在
        leftover = self.tmp / "只剩海报 (2021)"
        leftover.mkdir()
        (leftover / "poster.jpg").write_bytes(b"y" * 100)
        found = cleanup.scan(self.tmp)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].file_count, 1)
        self.assertTrue(found[0].removable)

    def test_tagged_dir_with_video_is_blocked(self) -> None:
        # 名字带 _空壳待删 但里面真有视频：宁可漏删也不能删片
        risky = self.tmp / "阿凡达 Collection_空壳待删"
        risky.mkdir()
        (risky / "avatar.mkv").write_bytes(b"x")
        found = cleanup.scan(self.tmp)
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0].removable)
        result = cleanup.remove(found)
        self.assertEqual(result.ok, 0)
        self.assertTrue((risky / "avatar.mkv").exists())

    def test_nested_empty_removed(self) -> None:
        keeper = self.tmp / "合集 Collection"
        (keeper / "第一部 (2001)").mkdir(parents=True)
        (keeper / "第二部 (2003)").mkdir(parents=True)
        (keeper / "第二部 (2003)" / "b.mkv").write_bytes(b"x")
        found = cleanup.scan(self.tmp)
        names = sorted(c.path.name for c in found)
        self.assertEqual(names, ["第一部 (2001)"])
        result = cleanup.remove(found)
        self.assertEqual(result.ok, 1)
        self.assertFalse((keeper / "第一部 (2001)").exists())
        self.assertTrue((keeper / "第二部 (2003)" / "b.mkv").exists())

    def test_remove_deletes_leftover_files(self) -> None:
        leftover = self.tmp / "空壳 Collection_空壳待删"
        leftover.mkdir()
        (leftover / "poster.jpg").write_bytes(b"y")
        found = cleanup.scan(self.tmp)
        result = cleanup.remove(found)
        self.assertEqual(result.ok, 1)
        self.assertFalse(leftover.exists())

    def test_missing_root_is_safe(self) -> None:
        self.assertEqual(cleanup.scan(self.tmp / "不存在"), [])


class EpisodeParseTest(unittest.TestCase):
    """剧集集号：四种命名格式 + 父目录季号。"""

    def test_leading_number(self) -> None:
        self.assertEqual(naming.parse_episode("01.flv"), (0, 1))
        self.assertEqual(naming.parse_episode("02.flv"), (0, 2))

    def test_bracket_number(self) -> None:
        self.assertEqual(
            naming.parse_episode("[DBD-Raws][Grand Blue][01][1080P][BDRip][HEVC-10bit].mkv"),
            (0, 1),
        )

    def test_chinese_counter(self) -> None:
        self.assertEqual(naming.parse_episode("第01话 那就是恋情的开端 4K.mp4"), (0, 1))

    def test_sxxexx(self) -> None:
        self.assertEqual(naming.parse_episode("胆大党S01E03.mp4"), (1, 3))

    def test_trailing_number(self) -> None:
        # 全职猎人重制版：集号在文件名末尾
        self.assertEqual(naming.parse_episode("HUNTER×HUNTER(2011) 001.mkv"), (0, 1))
        self.assertEqual(naming.parse_episode("HUNTER×HUNTER(2011) 062.mkv"), (0, 62))

    def test_sequel_number_is_not_episode(self) -> None:
        # 「阿凡达.2.mkv」的 2 是续集编号，不是第二集
        self.assertIsNone(naming.parse_episode("阿凡达.2.mkv"))

    def test_season_inside_filename(self) -> None:
        self.assertEqual(naming.parse_episode("[Feibanyama] Dan Da Dan S2 [07][2160p].mkv"), (2, 7))

    def test_movies_have_no_episode(self) -> None:
        for name in (
            "冰雪奇缘 (2013) 1080P.mkv",
            "2012 (2009).mkv",
            "The.Bad.Guys.2022.2160p.WEB-DL.x265.10bit.SDR.mkv",
        ):
            self.assertIsNone(naming.parse_episode(name), name)

    def test_season_from_folder(self) -> None:
        self.assertEqual(naming.parse_season("第一季"), 1)
        self.assertEqual(naming.parse_season("第二季 抢先版"), 2)
        self.assertEqual(naming.parse_season("胆大党S01"), 1)
        self.assertEqual(naming.parse_season("胆大党S02"), 2)
        self.assertEqual(naming.parse_season("Season 03"), 3)
        self.assertEqual(naming.parse_season("全职猎人1999"), 1)

    def test_series_query_from_parent(self) -> None:
        self.assertEqual(
            naming.guess_series_query("/a/全职猎人/全职猎人1999/01.flv")[0], "全职猎人"
        )
        self.assertEqual(naming.guess_series_query("/a/碧蓝之海/第一季/[x][01].mkv")[0], "碧蓝之海")
        self.assertEqual(
            naming.guess_series_query("/a/胆大党S01/日语特效字幕.B站版.4K/第01话x.mp4")[0],
            "胆大党",
        )


class LanguageOriginalTest(unittest.TestCase):
    """第二段名字按作品语言挑：日本用日文、英语地区用英文。"""

    def test_japanese_uses_original(self) -> None:
        meta = {"original_language": "ja", "original": "ぐらんぶる", "en": "Grand Blue"}
        self.assertEqual(naming.language_original(meta, "碧蓝之海"), "ぐらんぶる")

    def test_english_uses_original(self) -> None:
        meta = {"original_language": "en", "original": "Zootopia", "en": "Zootopia"}
        self.assertEqual(naming.language_original(meta, "疯狂动物城"), "Zootopia")

    def test_chinese_falls_back_to_english(self) -> None:
        meta = {
            "original_language": "zh",
            "original": "流浪地球",
            "en": "The Wandering Earth",
        }
        self.assertEqual(naming.language_original(meta, "流浪地球"), "The Wandering Earth")

    def test_same_as_cn_is_dropped(self) -> None:
        meta = {"original_language": "ja", "original": "罗小黑战记", "en": "The Legend of Hei"}
        self.assertEqual(naming.language_original(meta, "罗小黑战记"), "The Legend of Hei")


class TvPlanTest(unittest.TestCase):
    """剧集计划：Season 子目录 + SxxExx + tvshow.nfo。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.ep_dir = self.tmp / "碧蓝之海" / "第一季"
        self.ep_dir.mkdir(parents=True)
        self.video = self.ep_dir / "[DBD-Raws][Grand Blue][01].mkv"
        self.video.write_bytes(b"x" * 1024)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _record(self, path: str, kind: str, season: int, episode: int) -> dict:
        return {
            "path": path,
            "probe": ProbeResult(path=path, size=1024),
            "meta": {
                "cn": "碧蓝之海",
                "orig": "ぐらんぶる",
                "en": "Grand Blue",
                "original": "ぐらんぶる",
                "year": "2018",
                "tmdbid": 79166,
                "ep_title": "深蓝",
            },
            "kind": kind,
            "season": season,
            "episode": episode,
            "checked": True,
            "source": "BDRip",
            "group": "",
            "edition": "",
        }

    def test_episode_goes_to_season_folder(self) -> None:
        record = self._record(str(self.video), "tv", 1, 1)
        ops = organizer.build_plan(
            [record],
            self.tmp,
            template="{cn}.{orig}.S{season}E{episode}.{res}.{codec}",
            folder_template="{cn} ({year})",
            ep_template="{cn}.{orig}.S{season}E{episode}.{res}.{codec}",
        )
        moves = [op for op in ops if op.kind == "move"]
        self.assertEqual(len(moves), 1)
        target = Path(moves[0].dst)
        self.assertEqual(target.parent.name, "Season 01")
        self.assertEqual(target.parent.parent.name, "碧蓝之海 (2018)")
        self.assertIn("S01E01", target.name)
        self.assertIn("ぐらんぶる", target.name)

    def test_episode_writes_tvshow_and_episode_nfo(self) -> None:
        record = self._record(str(self.video), "tv", 1, 1)
        ops = organizer.build_plan(
            [record],
            self.tmp,
            template="{cn}.{orig}.S{season}E{episode}",
            folder_template="{cn} ({year})",
            ep_template="{cn}.{orig}.S{season}E{episode}",
        )
        nfo_paths = [Path(op.dst).name for op in ops if op.kind == "write_nfo"]
        self.assertIn("tvshow.nfo", nfo_paths)
        self.assertIn("S01E01.nfo", nfo_paths)

    def test_movie_still_uses_movie_nfo(self) -> None:
        record = self._record(str(self.video), "movie", 0, 0)
        record["meta"] = {"cn": "碧蓝之海", "year": "2018", "tmdbid": 1}
        ops = organizer.build_plan(
            [record],
            self.tmp,
            template="{cn}.{orig}.{year}",
            folder_template="{cn} ({year})",
        )
        nfo_paths = [Path(op.dst).name for op in ops if op.kind == "write_nfo"]
        self.assertEqual(nfo_paths, ["movie.nfo"])


class ClassifyTest(unittest.TestCase):
    """电影/剧集判定：同目录 >=2 个不同集号才算剧集。"""

    def test_two_episodes_become_tv(self) -> None:
        files = [
            Path("/a/碧蓝之海/第一季/01.mkv"),
            Path("/a/碧蓝之海/第一季/02.mkv"),
        ]
        kinds = classify_media(files)
        self.assertTrue(all(kinds[str(f)]["kind"] == "tv" for f in files))
        self.assertEqual(kinds[str(files[0])]["episode"], 1)
        self.assertEqual(kinds[str(files[1])]["episode"], 2)

    def test_single_file_stays_movie(self) -> None:
        # 《101忠狗》这类片名里带数字，单文件不能判成剧集
        files = [Path("/a/101忠狗/101忠狗.mkv")]
        kinds = classify_media(files)
        self.assertEqual(kinds[str(files[0])]["kind"], "movie")

    def test_season_from_parent(self) -> None:
        files = [
            Path("/a/碧蓝之海/第二季 抢先版/[S2][01].mkv"),
            Path("/a/碧蓝之海/第二季 抢先版/[S2][02].mkv"),
        ]
        kinds = classify_media(files)
        self.assertEqual(kinds[str(files[0])]["season"], 2)


class MatchQueryTest(unittest.TestCase):
    """译名对不上时的补救：后缀剥离 + 原名段补搜词。"""

    def test_variants_pick_original_title_segment(self) -> None:
        # 《长发公主》TMDB 官方译名是《魔发奇缘》，中文查询词搜不到；
        # 文件名里的 Tangled 才是命中它的钥匙
        path = (
            "N:/动漫/长发公主 (2010) 1080P/"
            "长发公主.Tangled.2010.BluRay.1080p.x265.10bit.3Audio.MNHD-FRDS.mkv"
        )
        self.assertEqual(naming.query_variants(path, "长发公主"), ["Tangled"])

    def test_variants_join_latin_run(self) -> None:
        path = "N:/电影/The.Matrix.1999.BluRay.mkv"
        # 主查询词已经是 The Matrix，补充词去重后不该重复
        self.assertEqual(naming.query_variants(path, "The Matrix"), [])

    def test_variants_skip_noise_segments(self) -> None:
        path = "N:/x/碧蓝之海真人版.HDRip.1080p.x264.AAC.2.0.mp4"
        self.assertEqual(naming.query_variants(path, "碧蓝之海真人版"), [])

    def test_variants_skip_episode_tokens(self) -> None:
        # S01E01 是拉丁文，但不是标题，不能当补充查询词
        path = "N:/x/碧蓝之海 (2018)/Season 01/碧蓝之海.ぐらんぶる.S01E01.1080p.x265.10bit.mkv"
        self.assertEqual(naming.query_variants(path, "碧蓝之海"), [])

    def test_series_query_skips_season_folder(self) -> None:
        # 整理过的库：剧名 (年)/Season 01/xx.mkv —— Season 层是容器不是剧名，
        # 拿「Season」当查询词会搜出毫不相干的片
        path = "N:/x/碧蓝之海 (2018)/Season 01/碧蓝之海.ぐらんぶる.S01E01.1080p.x265.10bit.mkv"
        self.assertEqual(naming.guess_series_query(path), ("碧蓝之海", "2018"))

    def test_series_name_ending_with_s_is_kept(self) -> None:
        # 「KIDS」这种以 S 结尾的剧名不能被季号尾巴啃掉
        path = "N:/x/KIDS/KIDS - 01.mkv"
        self.assertEqual(naming.guess_series_query(path), ("KIDS", ""))

    def test_strip_edition_suffix(self) -> None:
        self.assertEqual(naming.strip_edition_suffix("碧蓝之海真人版"), "碧蓝之海")
        self.assertEqual(naming.strip_edition_suffix("Grand Blue Live Action"), "Grand Blue")
        # 没有后缀就原样返回
        self.assertEqual(naming.strip_edition_suffix("碧蓝之海"), "碧蓝之海")

    def test_empty_year_folder_has_no_parens(self) -> None:
        record = {"path": "N:/动漫/碧蓝之海真人版 ()/碧蓝之海真人版.HDRip.mp4", "meta": {}}
        self.assertEqual(
            naming.render_folder(record, cfg.DEFAULT_FOLDER_TEMPLATE), "碧蓝之海真人版"
        )

    def test_empty_year_filename_has_no_parens(self) -> None:
        record = {"path": "N:/x/碧蓝之海真人版.mkv", "meta": {}}
        name = naming.render_filename(record, "{cn} ({year}).{source}")
        self.assertEqual(name, "碧蓝之海真人版.mkv")


if __name__ == "__main__":
    unittest.main()

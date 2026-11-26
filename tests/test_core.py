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


if __name__ == "__main__":
    unittest.main()

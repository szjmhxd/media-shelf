"""核心逻辑自检：不依赖 PySide6，可直接 `python -m unittest` 跑。"""

from __future__ import annotations

import csv
import os
import tempfile
import unittest
from pathlib import Path

from mediashelf import apply as apply_mod
from mediashelf import config as cfg
from mediashelf import naming, nfo, organizer
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

    def test_unchecked_skipped(self) -> None:
        self.record["checked"] = False
        ops = organizer.build_plan(
            [self.record], self.root, template="{cn}", folder_template="{cn}", write_nfo=False
        )
        self.assertEqual(ops, [])

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


if __name__ == "__main__":
    unittest.main()

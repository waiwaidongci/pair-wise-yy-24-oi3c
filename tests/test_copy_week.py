import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import DomainError, RadioDB


class CopyWeekTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = RadioDB(self.path)
        # 来源周 2026-09-28(周一) ~ 2026-10-04(周日)
        self.news = self.db.add_program("城市早报", "talk", 30, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        self.ad = self.db.add_program("青柠广告", "ad", 5, "2026-01-01", "2026-12-31", "青柠", 0, ["华东"])
        self.db.add_sponsor_policy("青柠", 90)
        # 授权在来源周结束、下周已失效的节目
        self.short = self.db.add_program("临时版权", "music", 20, "2026-09-01", "2026-10-04", None, 0, ["华东"])

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def test_preview_and_copy_map_weekdays_and_keep_normal_flow(self):
        self.db.schedule_slot("2026-09-28", "09:00", self.news, "华东")
        self.db.schedule_slot("2026-09-30", "10:00", self.ad, "华东")
        # 周中任意一天都能定位到同一个来源周
        preview = self.db.preview_copy_week("2026-10-02", "华东")
        self.assertTrue(preview["ok"])
        self.assertEqual("2026-10-05", preview["week_start"])
        self.assertEqual(
            [("2026-10-05", "09:00", self.news), ("2026-10-07", "10:00", self.ad)],
            [(s["air_date"], s["start_time"], s["program_id"]) for s in preview["slots"]],
        )
        # 预览不写入
        self.assertEqual(0, self.db.conn.execute(
            "SELECT COUNT(*) FROM slots WHERE air_date >= '2026-10-05'").fetchone()[0])

        saved = self.db.copy_week("2026-09-28", "华东")
        self.assertEqual(["planned", "planned"], [s["status"] for s in saved])
        self.assertEqual(2, self.db.conn.execute(
            "SELECT COUNT(*) FROM slots WHERE air_date >= '2026-10-05'").fetchone()[0])
        # 复制后的排期仍可按普通排期替换、登记实播和对账
        new_id = saved[0]["id"]
        replaced = self.db.replace_slot(new_id, self.ad)
        self.assertEqual("replaced", replaced["status"])
        self.db.record_playout(new_id, "09:00", 5, self.news)
        exceptions = self.db.reconcile_date("2026-10-05")
        self.assertIn("wrong_program", {e["kind"] for e in exceptions})

    def test_conflicts_are_all_listed_and_nothing_is_written(self):
        self.db.schedule_slot("2026-09-28", "09:00", self.news, "华东")
        self.db.schedule_slot("2026-09-28", "11:00", self.ad, "华东")
        self.db.schedule_slot("2026-09-29", "14:00", self.short, "华东")
        # 目标周已有节目，会与复制下来的早报重叠；另有赞助间隔不足的节目
        clash = self.db.schedule_slot("2026-10-05", "09:15", self.news, "华东")
        self.db.schedule_slot("2026-10-05", "11:30", self.ad, "华东")

        preview = self.db.preview_copy_week("2026-09-28", "华东")
        self.assertFalse(preview["ok"])
        conflicts = {(c["air_date"], c["start_time"], c["title"]): c["errors"] for c in preview["conflicts"]}
        self.assertEqual(
            {"2026-10-05 09:00 城市早报", "2026-10-05 11:00 青柠广告", "2026-10-06 14:00 临时版权"},
            set(f"{d} {t} {title}" for (d, t, title) in conflicts),
        )
        self.assertIn(f"与排期 #{clash} 时间重叠", conflicts[("2026-10-05", "09:00", "城市早报")])
        self.assertTrue(any("赞助商" in e for e in conflicts[("2026-10-05", "11:00", "青柠广告")]))
        self.assertIn("播出日期超出授权窗口", conflicts[("2026-10-06", "14:00", "临时版权")])

        with self.assertRaisesRegex(DomainError, "未写入任何排期"):
            self.db.copy_week("2026-09-28", "华东")
        self.assertEqual(2, self.db.conn.execute(
            "SELECT COUNT(*) FROM slots WHERE air_date >= '2026-10-05'").fetchone()[0])

    def test_cancelled_slots_are_skipped(self):
        self.db.schedule_slot("2026-09-28", "09:00", self.news, "华东")
        dropped = self.db.schedule_slot("2026-09-28", "10:00", self.news, "华东")
        self.db.conn.execute("UPDATE slots SET status='cancelled' WHERE id=?", (dropped,))
        self.db.conn.commit()
        saved = self.db.copy_week("2026-09-28", "华东")
        self.assertEqual(["2026-10-05"], [s["air_date"] for s in saved])
        self.assertEqual(1, len(saved))

    def test_empty_source_week_is_rejected(self):
        with self.assertRaisesRegex(DomainError, "没有可复制的排期"):
            self.db.preview_copy_week("2026-09-28", "华东")


if __name__ == "__main__":
    unittest.main()

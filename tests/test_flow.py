import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import CopyConflictError, DomainError, RadioDB


class RadioSchedulingFlowTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = RadioDB(self.path)
        self.p1 = self.db.add_program("早间新闻", "talk", 30, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        self.p2 = self.db.add_program("品牌广告", "ad", 5, "2026-01-01", "2026-12-31", "青柠", 0, ["华东"])

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def test_complete_replace_playout_and_reconcile_flow(self):
        first = self.db.schedule_slot("2026-09-28", "09:00", self.p1, "华东")
        second = self.db.schedule_slot("2026-09-28", "10:00", self.p2, "华东")
        self.assertEqual("planned", self.db.get_slot(first)["status"])
        replaced = self.db.replace_slot(first, self.p2)
        self.assertEqual("replaced", replaced["status"])
        self.assertEqual(self.p2, replaced["program_id"])
        self.db.record_playout(first, "09:00", 5, self.p1, "临时切回旧内容")
        self.db.record_playout(second, "10:00", 5, self.p2)
        exceptions = self.db.reconcile_date("2026-09-28")
        kinds = {(row["slot_id"], row["kind"]) for row in exceptions}
        self.assertIn((first, "wrong_program"), kinds)

    def test_rejects_overlap_and_unauthorized_region(self):
        self.db.schedule_slot("2026-09-28", "09:00", self.p1, "华东")
        with self.assertRaisesRegex(DomainError, "重叠"):
            self.db.schedule_slot("2026-09-28", "09:15", self.p1, "华东")
        with self.assertRaisesRegex(DomainError, "未授权"):
            self.db.schedule_slot("2026-09-28", "11:00", self.p1, "华北")

    def test_copy_week_maps_weekdays_to_next_week_and_saves_atomically(self):
        monday = self.db.schedule_slot("2026-09-28", "09:00", self.p1, "华东")
        tuesday = self.db.schedule_slot("2026-09-29", "09:00", self.p1, "华东")
        plan = self.db.build_copy_plan("2026-09-28", "华东")
        self.assertTrue(plan["ok"])
        self.assertEqual("2026-09-28", plan["source_week_start"])
        self.assertEqual("2026-10-05", plan["target_week_start"])
        mapped = {item["source_slot_id"]: item["air_date"] for item in plan["items"]}
        self.assertEqual("2026-10-05", mapped[monday])
        self.assertEqual("2026-10-06", mapped[tuesday])

        result = self.db.copy_week("2026-09-30", "华东")  # 传周三也应定位同一周
        self.assertEqual(2, len(result["created_slot_ids"]))
        copied = self.db.get_slot(result["created_slot_ids"][0])
        self.assertEqual("2026-10-05", copied["air_date"])
        self.assertEqual("planned", copied["status"])
        # 保存后仍是普通排期：可替换、登记实播、对账
        self.db.replace_slot(result["created_slot_ids"][0], self.p2)
        self.db.record_playout(result["created_slot_ids"][0], "09:00", 30, self.p1, "临时切回")
        exceptions = self.db.reconcile_date("2026-10-05")
        self.assertTrue(any(row["kind"] == "wrong_program" for row in exceptions))

    def test_copy_week_skips_cancelled_slots(self):
        kept = self.db.schedule_slot("2026-09-28", "09:00", self.p1, "华东")
        self.db.schedule_slot("2026-09-29", "10:00", self.p1, "华东")
        self.db.conn.execute("UPDATE slots SET status='cancelled' WHERE air_date='2026-09-29'")
        plan = self.db.build_copy_plan("2026-09-28", "华东")
        self.assertEqual([kept], [item["source_slot_id"] for item in plan["items"]])

    def test_copy_week_preview_collects_every_conflict_without_writing(self):
        # 目标周已存在排期 -> 重叠
        self.db.schedule_slot("2026-09-28", "09:00", self.p1, "华东")
        self.db.schedule_slot("2026-10-05", "09:10", self.p1, "华东")
        # 授权在目标周前到期
        expiring = self.db.add_program("秋季短授权", "music", 30, "2026-01-01", "2026-10-01", None, 0, ["华东"])
        self.db.schedule_slot("2026-09-30", "08:00", expiring, "华东")
        # 下周一 08:00 撞禁播（禁播窗按星期生效，故在源排期入库后再新增）
        blocked = self.db.add_program("晨间直播", "live", 30, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        self.db.schedule_slot("2026-09-28", "08:00", blocked, "华东")
        self.db.add_blocked_window("华东", 0, "07:30", "08:30", "下周一首例禁播")
        # 赞助间隔不足：源周广告 11:10 合规，但目标周已有同赞助商节目 11:20（间隔不足 90 分钟）
        self.db.add_sponsor_policy("青柠", 90)
        sponsored_other = self.db.add_program("青柠点歌台", "music", 30, "2026-01-01", "2026-12-31", "青柠", 0, ["华东"])
        self.db.schedule_slot("2026-09-28", "11:10", self.p2, "华东")
        self.db.schedule_slot("2026-10-05", "11:20", sponsored_other, "华东")

        plan = self.db.build_copy_plan("2026-09-28", "华东")
        self.assertFalse(plan["ok"])
        kinds = {c["kind"] for c in plan["conflicts"]}
        self.assertIn("overlap", kinds)
        self.assertIn("out_of_license", kinds)
        self.assertIn("blocked", kinds)
        self.assertIn("sponsor_gap", kinds)

        before = self.db.conn.execute("SELECT COUNT(*) FROM slots WHERE air_date>='2026-10-05' AND air_date<'2026-10-12'").fetchone()[0]
        with self.assertRaises(CopyConflictError):
            self.db.copy_week("2026-09-28", "华东")
        after = self.db.conn.execute("SELECT COUNT(*) FROM slots WHERE air_date>='2026-10-05' AND air_date<'2026-10-12'").fetchone()[0]
        self.assertEqual(before, after)  # 冲突时不写入任何排期

    def test_copy_week_rejects_empty_source_week_and_bad_region(self):
        with self.assertRaisesRegex(DomainError, "没有可复制"):
            self.db.build_copy_plan("2026-09-28", "华北")
        self.db.schedule_slot("2026-09-28", "09:00", self.p1, "华东")
        with self.assertRaisesRegex(DomainError, "地区不能为空"):
            self.db.build_copy_plan("2026-09-28", "  ")


if __name__ == "__main__":
    unittest.main()

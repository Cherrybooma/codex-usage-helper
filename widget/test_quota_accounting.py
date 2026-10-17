import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from widget.quota_accounting import quota_window_report


class QuotaAccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "history.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE observations(observed REAL,window_key TEXT,duration INTEGER,reset REAL,used REAL);
            CREATE TABLE observation_context(observed REAL,window_key TEXT,plan TEXT,limit_id TEXT);
            CREATE TABLE window_changes(window_key TEXT,duration INTEGER,reset REAL,observed_change REAL);
        """)

    def add(self, observed, used, reset=18000, duration=300, plan="plus", key="primary"):
        self.db.execute("INSERT INTO observations VALUES(?,?,?,?,?)", (observed,key,duration,reset,used))
        self.db.execute("INSERT INTO observation_context VALUES(?,?,?,?)", (observed,key,plan,"codex"))
        self.db.commit()

    def report(self, cutoff=600):
        return quota_window_report(self.path, datetime.fromtimestamp(cutoff, timezone.utc))

    def test_missing_history_does_not_create_database_or_zero(self):
        missing = self.path.with_name("missing.sqlite3")
        self.assertEqual(quota_window_report(missing,datetime.now(timezone.utc))["windows"], [])
        self.assertFalse(missing.exists())

    def test_fixed_cutoff_excludes_future_and_exact_boundary_samples(self):
        self.add(100,10); self.add(200,15); self.add(600,90); self.add(700,100)
        row = self.report()["windows"][0]
        self.assertEqual((row["observations"],row["used_percent"],row["observed_increase_percent"]),(2,15,5))
        self.assertEqual(self.report()["statistics_until"], datetime.fromtimestamp(600,timezone.utc).isoformat())

    def test_counter_correction_cannot_be_counted_as_new_use(self):
        self.add(100,80); self.add(200,5); self.add(300,15)
        row=self.report()["windows"][0]
        self.assertIsNone(row["observed_increase_percent"])
        self.assertEqual(row["status"],"incomplete")

    def test_changed_deadline_is_not_proven_bonus_or_unused_allowance(self):
        self.add(100,50); self.add(200,10,reset=18200)
        self.db.execute("INSERT INTO window_changes VALUES(?,?,?,?)", ("primary",300,18000,200)); self.db.commit()
        rows=self.report()["windows"]
        old=next(r for r in rows if r["used_percent"]==50)
        self.assertEqual((old["reset_kind"],old["state"]),("未确认","已结束"))
        self.assertEqual(old["remaining_percent"],50)
        self.assertNotIn("unused_percent",old)

    def test_plan_change_invalidates_delta(self):
        self.add(100,10); self.add(200,20,plan="pro")
        self.assertIsNone(self.report()["windows"][0]["observed_increase_percent"])

    def test_five_hour_and_weekly_windows_stay_separate(self):
        self.add(100,10); self.add(100,20,reset=604800,duration=10080,key="secondary")
        rows=self.report()["windows"]
        self.assertEqual({r["kind"] for r in rows},{"5小时","周额度"})
        self.assertFalse(any("total_tokens" in r for r in rows))

    def test_gaps_and_final_sample_remain_explicit(self):
        self.add(100,10); self.add(500,15)
        row=self.report(19000)["windows"][0]
        self.assertEqual(row["state"],"已结束")
        self.assertTrue(any("3分钟" in n for n in row["notes"]))
        self.assertTrue(any("最终" in n for n in row["notes"]))

    def test_future_change_cannot_rewrite_earlier_report(self):
        self.add(100,10)
        self.db.execute("INSERT INTO window_changes VALUES(?,?,?,?)", ("primary",300,18000,1000)); self.db.commit()
        self.assertEqual(self.report()["windows"][0]["reset_kind"],"常规")

    def test_naive_cutoff_rejected(self):
        with self.assertRaises(ValueError):
            quota_window_report(self.path,datetime(2026,1,1))

    def test_recent_history_limit_keeps_current_windows_and_local_history(self):
        for reset in range(18000, 18010):
            self.add(reset-100, 10, reset=reset)
        self.add(20000,20,reset=30000)
        now=datetime.fromtimestamp(21000,timezone.utc)
        report=quota_window_report(self.path,now,history_limit=2)
        self.assertEqual(len(report['windows']),3)
        self.assertEqual(report['windows_recorded_locally'],11)
        self.assertTrue(report['history_limited'])
        self.assertEqual(sum(r['state']=='进行中' for r in report['windows']),1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],11)

    def test_idle_zero_history_is_suppressed_without_deleting_raw_observations(self):
        self.add(100, 58, reset=604800, duration=10080, key="secondary")
        for offset in range(1000, 11000, 1000):
            self.add(offset, 0, reset=604800 + offset, duration=10080, key="secondary")
        # No window_changes table entries are needed: older zero deadlines may
        # still be in the future, but only the newest one is the current zero.
        before = self.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        report = self.report(11000)
        self.assertEqual(len(report["windows"]), 2)
        self.assertEqual(sorted(row["used_percent"] for row in report["windows"]), [0, 58])
        self.assertEqual(report["idle_zero_windows_omitted"], 9)
        self.assertEqual(report["windows_recorded_locally"], 11)
        self.assertFalse(report["history_limited"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], before)

    def test_all_zero_closed_history_is_not_an_unused_allowance_claim(self):
        self.add(100, 0, reset=18000)
        report = self.report(19000)
        self.assertEqual(report["windows"], [])
        self.assertEqual(report["idle_zero_windows_omitted"], 1)

    def test_fixed_cutoff_keeps_then_current_zero_before_future_usage(self):
        self.add(100, 0, reset=18000)
        self.add(700, 20, reset=18700)
        report = self.report(600)
        self.assertEqual(len(report["windows"]), 1)
        self.assertEqual(report["windows"][0]["used_percent"], 0)
        self.assertEqual(report["idle_zero_windows_omitted"], 0)


if __name__ == "__main__":
    unittest.main()

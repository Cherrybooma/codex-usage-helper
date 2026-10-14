import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from widget_backend import accounting_snapshot


class PeriodBackendTests(unittest.TestCase):
    NOW = datetime(2026, 10, 14, 2, tzinfo=timezone.utc)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.settings = self.data / 'app-settings.json'

    def fake_report(self, home, data, now, **kwargs):
        return {'generated_at': now.isoformat(), 'dataset_id': kwargs['registry'].dataset_id if kwargs['registry'] else 'local-test',
                'weekly': {'current': {'totals': {'total_tokens': 17}}},
                'monthly': {'status': 'configured' if kwargs.get('billing_anchor') else 'needs_anchor'}}

    def snapshot(self, now=None, force=False):
        return accounting_snapshot(self.data, self.data, self.data, now or self.NOW, force=force)

    def test_fresh_cache_reuses_tokens_but_advances_quota_cutoff(self):
        with patch('period_accounting.build_period_report', side_effect=self.fake_report) as replay, \
                patch('quota_accounting.quota_window_report', return_value={'windows': []}) as quota:
            first = self.snapshot()
            later = self.NOW + timedelta(seconds=100)
            second = self.snapshot(later)
            self.assertEqual(replay.call_count, 1)
            self.assertEqual(second['generated_at'], first['generated_at'])
            self.assertEqual(quota.call_args.args[1], later)

    def test_changed_confirmed_anchor_never_reuses_old_month(self):
        with patch('period_accounting.build_period_report', side_effect=self.fake_report) as replay:
            self.assertEqual(self.snapshot()['monthly']['status'], 'needs_anchor')
            self.settings.write_text(json.dumps({'billing_anchor': '2026-10-01T00:00:00+08:00', 'billing_anchor_precision': 'date'}))
            self.assertEqual(self.snapshot()['monthly']['status'], 'configured')
            self.assertEqual(replay.call_count, 2)
            self.assertEqual(replay.call_args.kwargs['billing_anchor_precision'], 'date')

    def test_fixed_earlier_cutoff_never_uses_future_cache(self):
        with patch('period_accounting.build_period_report', side_effect=self.fake_report) as replay:
            self.snapshot()
            earlier = self.NOW - timedelta(seconds=1)
            self.assertEqual(self.snapshot(earlier)['generated_at'], earlier.isoformat())
            self.assertEqual(replay.call_count, 2)

    def test_force_bypasses_cache_and_expired_cache_replays(self):
        with patch('period_accounting.build_period_report', side_effect=self.fake_report) as replay:
            self.snapshot(); self.snapshot(force=True)
            self.snapshot(self.NOW + timedelta(seconds=300))
            self.assertEqual(replay.call_count, 3)

    def test_malformed_cache_recovers_without_auth_or_cloud(self):
        (self.data / 'accounting.json').write_text('[]')
        with patch('period_accounting.build_period_report', side_effect=self.fake_report), \
                patch('notion_credentials.configured_token', side_effect=AssertionError('no credentials')):
            self.assertEqual(self.snapshot()['weekly']['current']['totals']['total_tokens'], 17)

    def test_naive_cutoff_rejected(self):
        with self.assertRaises(ValueError):
            self.snapshot(datetime(2026, 10, 14))

    def test_cleared_swift_anchor_and_precision_null_are_unconfigured(self):
        self.settings.write_text(json.dumps({'billing_anchor':None,'billing_anchor_precision':None}))
        with patch('period_accounting.build_period_report', side_effect=self.fake_report) as replay:
            self.assertEqual(self.snapshot()['monthly']['status'],'needs_anchor')
            self.assertEqual(replay.call_args.kwargs['billing_anchor_precision'],'datetime')


if __name__ == '__main__':
    unittest.main()

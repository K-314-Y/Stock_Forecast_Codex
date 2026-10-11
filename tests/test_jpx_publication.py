"""Local calendar and synthetic observations only; no browser/network requests."""
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('acquisition', ROOT / 'providers/jp/jpx_manual/acquisition.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        source = json.loads((ROOT / 'analysis/calendar_validation_2026-09-12.json').read_text(encoding='utf-8-sig'))
        self.calendar = dict(validated=True, timezone='Asia/Tokyo', sessions=source['jp_sessions'],
                             coverage_start=source['validation_range']['start'],
                             coverage_end=source['validation_range']['end'])

    def observation(self, day, available=False):
        return a.publication_observation('2026-09-18', self.calendar,
                                         datetime.fromisoformat(day + 'T18:00:00+09:00'), available)

    def test_next_business_day(self):
        self.assertEqual(a.publication_due('2026-09-18', self.calendar), '2026-09-24')

    def test_holidays_are_not_sessions(self):
        for d in ('2026-09-21', '2026-09-22', '2026-09-23'):
            self.assertNotIn(d, self.calendar['sessions'])

    def test_before_due_never_missing(self):
        for d in ('2026-09-19', '2026-09-20', '2026-09-21', '2026-09-22', '2026-09-23'):
            r = self.observation(d)
            self.assertEqual(r['publication_state'], 'NOT_YET_AVAILABLE')
            self.assertIsNone(r['missing_session'])
            self.assertFalse(r['backfill_enqueue'])

    def test_due_day_absent_is_pending(self):
        r = self.observation('2026-09-24')
        self.assertEqual(r['publication_state'], 'PENDING_PUBLICATION')
        self.assertEqual(r['one_shot_availability'], 'NOT_YET_AVAILABLE')

    def test_available(self):
        r = self.observation('2026-09-24', True)
        self.assertEqual(r['publication_state'], 'AVAILABLE')
        self.assertEqual(r['published_session'], '2026-09-18')
        self.assertEqual(r['published_exact_time'], 'UNKNOWN')

    def test_past_due_candidate_not_queue(self):
        r = self.observation('2026-09-25')
        self.assertEqual(r['publication_state'], 'MISSING_SOURCE_CANDIDATE')
        self.assertIsNone(r['missing_session'])
        self.assertFalse(r['backfill_enqueue'])

    def test_unobserved_not_assumed_absent(self):
        r = self.observation('2026-09-24', None)
        self.assertEqual(r['publication_state'], 'NOT_CHECKED')
        self.assertIsNone(r['available_at_check'])

    def test_jst_boundary(self):
        r = a.publication_observation('2026-09-18', self.calendar,
                                      datetime.fromisoformat('2026-09-23T15:00:00+00:00'), False)
        self.assertEqual(r['publication_state'], 'PENDING_PUBLICATION')

    def test_naive_time_rejected(self):
        with self.assertRaises(ValueError):
            a.publication_observation('2026-09-18', self.calendar, datetime(2026, 9, 24), False)

    def test_no_download_without_listing(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(a.plan('2026-09-24', self.calendar, [], ['2026-09-17'], Path(d)), [])
            self.assertEqual(a.plan('2026-09-24', self.calendar, [], [], Path(d),
                                    published_sessions=['2026-09-18']),
                             [dict(session_date='2026-09-18', acquisition_type='NORMAL')])

    def test_no_implicit_backfill(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(a.plan('2026-09-25', self.calendar, [], [], Path(d),
                                    published_sessions=['2026-09-18']), [])

    def test_calendar_end_fails_closed(self):
        with self.assertRaises(ValueError):
            a.publication_due(self.calendar['sessions'][-1], self.calendar)


if __name__ == '__main__':
    unittest.main(verbosity=2)

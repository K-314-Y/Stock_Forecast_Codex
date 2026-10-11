"""Synthetic temp files only. Never invoke the native OS recycle backend."""
import copy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'providers/jp/jpx_manual' / (name + '.py'))
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


c = module('cleanup')
a = module('acquisition')
NOW = datetime.fromisoformat('2000-02-03T08:00:00+09:00')


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.inbox = self.root / c.INBOX
        self.inbox.mkdir(parents=True)
        self.raw = self.inbox / 'synthetic.pdf'
        self.raw.write_bytes(b'%PDF-FAKE-TEST-NOT-MARKET-DATA')
        (self.root / 'config').mkdir()
        for name in ('minimal_forecast_schema.json', 'trial_score_schema.json'):
            shutil.copyfile(ROOT / 'config' / name, self.root / 'config' / name)
        ex = c.load(ROOT / 'analysis/provider_test_results/minimal_design_examples_2026-09-15.json')
        fs, ss, rows = [], [], []
        for ticker in sorted(c.CORE):
            f, s = copy.deepcopy(ex['forecast']), copy.deepcopy(ex['score'])
            fid = 'SYNTHETIC-' + ticker
            # Trial-shaped synthetic fixtures exist only in this temporary tree.
            f.update(ticker=ticker, forecast_id=fid, record_kind='TRIAL')
            s.update(ticker=ticker, forecast_id=fid, record_kind='TRIAL')
            fs.append(f)
            ss.append(s)
            rows.append(dict(ticker=ticker, session_date='2000-01-04', open=s['actual_open'], close=s['actual_close'],
                             high=max(s['actual_open'], s['actual_close']), low=min(s['actual_open'], s['actual_close']), volume=100))
        self.e = dict(filename=self.raw.name, session_date='2000-01-04', expected_session_date='2000-01-04',
                      sha256=c.sha(self.raw), downloaded_at='2000-01-04T08:00:00+09:00',
                      parsed_at='2000-01-04T16:00:00+09:00', scored_at='2000-01-04T17:00:00+09:00',
                      acquisition_type='NORMAL', retention_class='DAILY', trash_after='2000-02-03',
                      parser_status='PASS', core6_count=6, schema_validation='PASS', scoring_status='SCORED',
                      data_conflict=False, parser_error=False, corporate_action_flag='NONE_CONFIRMED',
                      manual_review=False, scoring_discrepancy=False, schema_migration=False, user_keep=False,
                      forecast_ids=[f['forecast_id'] for f in fs], trash_moved_at=None, eligible_for_trash=False)
        self.artifacts = {}
        for kind, data in (('normalized_output', dict(records=rows, source_file_hash=self.e['sha256'], normalized_schema_validation='PASS')),
                           ('forecast_record', dict(records=fs)), ('scoring_record', dict(records=ss))):
            p = self.root / (kind + '.json')
            p.write_text(json.dumps(data), encoding='utf-8')
            self.e[kind + '_path'] = p.name
            self.e[kind + '_sha256'] = c.sha(p)
            self.artifacts[p] = c.sha(p)
        self.manifest = self.inbox / 'retention_manifest.json'
        self.manifest.write_text(json.dumps({'entries': [self.e]}), encoding='utf-8')
        self.policy = {'cleanup_enabled': True, 'windows_trash_backend_validated': True}

    def tearDown(self):
        self.tmp.cleanup()

    def test_30_calendar_days(self):
        self.assertEqual(c.trash_date(self.e['downloaded_at']), '2000-02-03')
        self.assertFalse(c.eligible(self.root, self.e, datetime.fromisoformat('2000-02-02T23:59:00+09:00'))[0])
        self.assertTrue(c.eligible(self.root, self.e, NOW)[0])

    def test_jst_boundary(self):
        self.assertEqual(c.trash_date('2000-01-04T23:59:00-05:00'), '2000-02-04')

    def test_protected_classes(self):
        for kind in c.load(ROOT / 'config/raw_data_retention_policy.json')['protected_classes']:
            with self.subTest(kind=kind):
                e = dict(self.e, retention_class=kind)
                self.assertFalse(c.eligible(self.root, e, NOW)[0])

    def test_protected_flags(self):
        for flag in ('data_conflict', 'parser_error', 'manual_review', 'scoring_discrepancy', 'schema_migration', 'user_keep'):
            with self.subTest(flag=flag):
                self.assertFalse(c.eligible(self.root, dict(self.e, **{flag: True}), NOW)[0])

    def test_unknown_ca_and_parser_failure(self):
        for update in ({'corporate_action_flag': 'UNKNOWN'}, {'parser_status': 'FAIL'}, {'core6_count': 5},
                       {'downloaded_at': None}, {'scoring_status': 'PENDING_DATA'}, {'forecast_ids': []}):
            with self.subTest(update=update):
                self.assertFalse(c.eligible(self.root, dict(self.e, **update), NOW)[0])

    def test_raw_hash_tamper(self):
        self.raw.write_bytes(b'CORRUPTED-SYNTHETIC')
        self.assertFalse(c.eligible(self.root, self.e, NOW)[0])

    def test_normalized_tamper(self):
        (self.root / self.e['normalized_output_path']).write_text('{}', encoding='utf-8')
        self.assertFalse(c.eligible(self.root, self.e, NOW)[0])

    def test_outside_path(self):
        with self.assertRaises(ValueError):
            c.bounded(self.root, '../outside.pdf')

    def test_move_preserves_evidence(self):
        trash = self.root / 'FAKE_TRASH'
        trash.mkdir()
        def fake_backend(path):
            shutil.move(str(path), str(trash / path.name))
        c.apply_one(self.root, self.manifest, self.raw.name, self.policy, NOW, fake_backend)
        self.assertEqual(c.sha(trash / self.raw.name), self.e['sha256'])
        for path, checksum in self.artifacts.items():
            self.assertEqual(c.sha(path), checksum)
        entry = c.load(self.manifest)['entries'][0]
        self.assertEqual(entry['forecast_ids'], self.e['forecast_ids'])
        self.assertEqual(entry['trash_moved_at'], NOW.isoformat())

    def test_failure_no_delete_fallback(self):
        def fail(path):
            raise OSError('simulated recycle failure')
        with self.assertRaises(OSError):
            c.apply_one(self.root, self.manifest, self.raw.name, self.policy, NOW, fail)
        self.assertTrue(self.raw.exists())
        self.assertEqual(c.sha(self.raw), self.e['sha256'])
        with self.assertRaisesRegex(ValueError, 'unfinished'):
            c.apply_one(self.root, self.manifest, self.raw.name, self.policy, NOW, fail)

    def test_disabled(self):
        with self.assertRaisesRegex(ValueError, 'DISABLED'):
            c.apply_one(self.root, self.manifest, self.raw.name, {}, NOW)

    def test_no_permanent_delete_code(self):
        import ast
        source = (ROOT / 'providers/jp/jpx_manual/cleanup.py').read_text(encoding='utf-8')
        banned = {'unlink', 'remove', 'rmdir', 'rmtree', 'DeleteFileW', 'SHFileOperationW'}
        calls = [node.func.attr for node in ast.walk(ast.parse(source))
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        self.assertFalse(banned.intersection(calls))


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.day = '2000-01-10'
        self.now = datetime.fromisoformat(self.day + 'T08:00:00+09:00')
        self.normal = dict(session_date='2000-01-07', acquisition_type='NORMAL')
        self.old = dict(session_date='2000-01-04', acquisition_type='BACKFILL')
        self.ledger = dict(day=self.day, approved_plan=[self.normal, self.old], attempts=[], stopped=False)
        self.calendar = dict(validated=True, timezone='Asia/Tokyo', coverage_start='2000-01-04', coverage_end=self.day,
                             sessions=['2000-01-04', '2000-01-05', '2000-01-06', '2000-01-07', self.day])

    def test_one_normal_plus_oldest_backfill(self):
        self.assertEqual(a.plan(self.day, self.calendar, [], ['2000-01-05', '2000-01-04'], Path('.'),
                                published_sessions=['2000-01-07', '2000-01-04', '2000-01-05']),
                         [self.normal, self.old])

    def test_duplicate_zero_web(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = root / 'fake.pdf'
            p.write_bytes(b'FAKE')
            entries = [dict(filename=p.name, session_date='2000-01-07', sha256=a.digest(p))]
            self.assertEqual(a.plan(self.day, self.calendar, entries, [], root), [])

    def test_unregistered_pdf_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'unknown.pdf').write_bytes(b'FAKE')
            with self.assertRaisesRegex(ValueError, 'unregistered'):
                a.plan(self.day, self.calendar, [], [], Path(tmp))

    def test_ledger_is_append_version_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'ledger-v1.json'
            a.persist_ledger_new(p, self.ledger)
            with self.assertRaises(FileExistsError):
                a.persist_ledger_new(p, self.ledger)

    def test_two_successes_third_blocked(self):
        for target in (self.normal, self.old):
            a.finish_attempt(self.ledger, a.begin_attempt(self.ledger, target, self.now), 'SUCCESS')
        with self.assertRaisesRegex(ValueError, 'DAILY_CAP'):
            a.begin_attempt(self.ledger, self.normal, self.now)

    def test_duplicate_attempt_blocked(self):
        a.finish_attempt(self.ledger, a.begin_attempt(self.ledger, self.normal, self.now), 'SUCCESS')
        with self.assertRaisesRegex(ValueError, 'DUPLICATE'):
            a.begin_attempt(self.ledger, self.normal, self.now)

    def test_retry_max_one(self):
        for _ in range(2):
            a.finish_attempt(self.ledger, a.begin_attempt(self.ledger, self.normal, self.now), 'TRANSIENT_FAILURE')
        with self.assertRaisesRegex(ValueError, 'STOP_FOR_THE_DAY'):
            a.begin_attempt(self.ledger, self.normal, self.now)

    def test_restrictions_zero_retry(self):
        for result in a.RESTRICTIONS | a.HARD_FAILURES:
            with self.subTest(result=result):
                ledger = dict(day=self.day, approved_plan=[self.normal], attempts=[], stopped=False)
                a.finish_attempt(ledger, a.begin_attempt(ledger, self.normal, self.now), result)
                with self.assertRaisesRegex(ValueError, 'STOP_FOR_THE_DAY'):
                    a.begin_attempt(ledger, self.normal, self.now)

    def test_restart_incomplete_attempt_stops(self):
        a.begin_attempt(self.ledger, self.normal, self.now)
        with self.assertRaises(ValueError):
            a.begin_attempt(self.ledger, self.old, self.now)

    def test_calendar_required(self):
        with self.assertRaises(ValueError):
            a.plan(self.day, dict(self.calendar, validated=False), [], [], Path('.'))
        self.assertEqual(a.plan('2000-01-08', self.calendar, [], [], Path('.')), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)

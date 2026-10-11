"""Local historical registration contracts; no browser/acquisition/forecast."""
import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engine import minimal_trial as core
from engine import historical_close_bridge as history
from engine import one_session_dry_run as probe
from engine import fresh_bridge

PROJECT = core.ROOT
DAY = '2026-09-14'
TARGET = '2026-10-09'
REGISTERED = datetime.fromisoformat('2026-10-08T14:00:00+09:00')
CREATED = datetime.fromisoformat(TARGET + 'T08:25:00+09:00')
CUTOFF = datetime.fromisoformat(TARGET + 'T08:45:00+09:00')


class HistoricalCloseBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for part in ('config', 'analysis', 'data/outcome_input/manual_jpx', 'data/outcome_parsed/jpx'):
            (self.root / part).mkdir(parents=True)
        for name in ('official_gap_policy.json', 'normalized_market_schema.json', 'fresh_feature_bridge.json'):
            (self.root / 'config' / name).write_bytes((PROJECT / 'config' / name).read_bytes())
        calendar = 'analysis/calendar_validation_2026-09-12.json'
        (self.root / calendar).write_bytes((PROJECT / calendar).read_bytes())
        manifest = json.loads((PROJECT / 'data/outcome_input/manual_jpx/retention_manifest.json').read_bytes())
        entry = next(e for e in manifest['entries'] if e['session_date'] == DAY)
        self.manifest_path = self.root / 'data/outcome_input/manual_jpx/retention_manifest.json'
        self.manifest = dict(entries=[entry])
        self.manifest_path.write_bytes(core.canonical_bytes(self.manifest))
        self.raw = self.root / 'data/outcome_input/manual_jpx' / entry['filename']
        self.parsed = self.root / entry['normalized_output_path']
        for path in (self.raw, self.parsed):
            path.write_bytes((PROJECT / path.relative_to(self.root)).read_bytes())
        root = patch.object(core, 'ROOT', self.root)
        root.start()
        self.addCleanup(root.stop)
        clock = patch.object(history, '_now', return_value=REGISTERED)
        self.clock = clock.start()
        self.addCleanup(clock.stop)

    def register(self):
        return history.register_existing(DAY, TARGET)

    def verify(self, evidence, created=CREATED, cutoff=CUTOFF):
        return history.verify(evidence, '6857', created, cutoff, '2026-10-08')

    def rewrite(self, evidence, field, value):
        path = Path(evidence['path'])
        record = core.read_frozen(path)
        record[field] = value
        path.write_bytes(core.canonical_bytes(record))
        path.with_suffix('.sha256').write_text(core.sha256(path))
        evidence['sha256'] = core.sha256(path)

    def test_registered_close_only_derivative_preserves_source_hashes_and_real_registration_time(self):
        before = {p: p.read_bytes() for p in (self.raw, self.parsed, self.manifest_path)}
        evidence = self.register()
        rows, hashes = self.verify(evidence)
        record = core.read_frozen(evidence['path'])
        self.assertEqual(len(record['records']), 6)
        self.assertEqual(record['registered_at'], REGISTERED.isoformat())
        self.assertEqual(record['source_role'], 'FORECAST_FEATURE')
        self.assertEqual(record['upstream_source_role'], 'OUTCOME_ONLY')
        self.assertEqual(record['eligibility'], 'NON_FORMAL')
        self.assertIsNone(record['original_downloaded_at'])
        self.assertNotEqual(record['original_retrieved_at'], record['registered_at'])
        self.assertTrue(all(set(r) == {'ticker', 'session_date', 'close'} for r in record['records']))
        self.assertEqual(rows[0][0], DAY)
        self.assertEqual({p:p.read_bytes() for p in before}, before)
        self.assertIn(str(self.raw), hashes)
        self.assertIn(str(self.parsed), hashes)

    def test_repeated_registration_is_idempotent_without_overwrite_or_retiming(self):
        evidence = self.register()
        before = Path(evidence['path']).read_bytes()
        self.clock.return_value = datetime.fromisoformat('2026-10-08T15:00:00+09:00')
        self.assertEqual(self.register(), evidence)
        self.assertEqual(Path(evidence['path']).read_bytes(), before)
        with self.assertRaises(FileExistsError):
            probe._commit_record(Path(evidence['path']).parent, {'replacement': True})

    def test_caller_cannot_set_registration_time(self):
        with self.assertRaises(TypeError):
            history.register_existing(DAY, TARGET, registered_at='2026-09-14T16:00:00+09:00')
        evidence = self.register()
        evidence['retrieved_at'] = '2026-09-14T16:00:00+09:00'
        with self.assertRaisesRegex(ValueError, 'CALLER_RECEIPT_TIME_MISMATCH'):
            self.verify(evidence)

    def test_record_time_and_public_hash_rewriting_cannot_backdate(self):
        evidence = self.register()
        self.rewrite(evidence, 'registered_at', '2026-09-14T16:00:00+09:00')
        with self.assertRaisesRegex(ValueError, 'HISTORICAL_RECEIPT_AUTHENTICATION_FAILED'):
            self.verify(evidence)

    def test_close_and_public_hash_rewriting_rejected(self):
        evidence = self.register()
        records = core.read_frozen(evidence['path'])['records']
        records[0]['close'] += 1
        self.rewrite(evidence, 'records', records)
        with self.assertRaisesRegex(ValueError, 'HISTORICAL_RECEIPT_AUTHENTICATION_FAILED'):
            self.verify(evidence)

    def test_raw_modification_rejected(self):
        evidence = self.register()
        self.raw.write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'RAW_SOURCE_HASH_MISMATCH'):
            self.verify(evidence)

    def test_parsed_modification_rejected(self):
        evidence = self.register()
        parsed = json.loads(self.parsed.read_bytes())
        parsed['records'][0]['close'] += 1
        self.parsed.write_bytes(core.canonical_bytes(parsed))
        with self.assertRaisesRegex(ValueError, 'JPX_MANIFEST_OR_VALIDATION_MISMATCH'):
            self.verify(evidence)

    def test_unrelated_manifest_addition_does_not_destroy_old_registration(self):
        evidence = self.register()
        self.manifest['entries'].append(dict(session_date='2026-10-06', filename='other.pdf', sha256='f'*64))
        self.manifest_path.write_bytes(core.canonical_bytes(self.manifest))
        self.verify(evidence)

    def test_same_session_manifest_change_rejected(self):
        evidence = self.register()
        self.manifest['entries'][0]['manual_review'] = False
        self.manifest_path.write_bytes(core.canonical_bytes(self.manifest))
        with self.assertRaisesRegex(ValueError, 'HISTORICAL_MANIFEST_ENTRY_CHANGED'):
            self.verify(evidence)

    def test_latest_session_and_unapproved_dates_cannot_enter_historical_role(self):
        for day in ('2026-10-08', '2026-10-09', '2026-09-11', '2026-09-19'):
            with self.subTest(day=day), self.assertRaisesRegex(ValueError, 'HISTORICAL_SESSION_NOT_APPROVED'):
                history.register_existing(day, TARGET)
        self.assertFalse(history._root().exists())

    def test_original_jpx_and_outcome_only_role_cannot_be_used_directly(self):
        evidence = self.register()
        with self.assertRaisesRegex(ValueError, 'UNTRUSTED_HISTORICAL_BRIDGE_EVIDENCE'):
            self.verify(dict(evidence, path=str(self.parsed), sha256=core.sha256(self.parsed)))
        with self.assertRaisesRegex(ValueError, 'UNTRUSTED_HISTORICAL_BRIDGE_EVIDENCE'):
            self.verify(dict(evidence, source_role='OUTCOME_ONLY'))

    def test_registration_at_or_after_cutoff_rejected(self):
        self.clock.return_value = CUTOFF
        with self.assertRaisesRegex(ValueError, 'HISTORICAL_REGISTRATION_AFTER_CUTOFF'):
            self.register()
        self.assertFalse(history._root().exists())

    def test_registered_data_cannot_be_used_for_an_earlier_forecast(self):
        evidence = self.register()
        early = datetime.fromisoformat('2026-10-07T08:25:00+09:00')
        with self.assertRaisesRegex(ValueError, 'POST_GENERATION_INPUT_EVIDENCE'):
            self.verify(evidence, created=early)

    def test_unapproved_adjustment_basis_rejected_without_registration(self):
        path = self.root / 'config/official_gap_policy.json'
        policy = json.loads(path.read_bytes())
        policy['ADJUSTMENT_BASIS_COMPATIBLE_FOR_MINIMAL_TRIAL'] = False
        path.write_bytes(core.canonical_bytes(policy))
        with self.assertRaisesRegex(ValueError, 'HISTORICAL_BASIS_NOT_APPROVED'):
            self.register()

    def test_historical_and_fresh_are_separate_but_share_dry_run_input_contract(self):
        historical = self.register()
        stocks = {}
        for ticker in core.CORE:
            response = dict(structuredContent=dict(company_overview=dict(
                isin=fresh_bridge.CORE_ISINS[ticker], timestamp='2026-10-08T15:30:00+09:00',
                price=100, currency='JPY', exchange='JPX', source=dict(provider_id='FMP'))))
            # Synthetic future close, only in a temporary test store.
            with patch.object(fresh_bridge, '_receipt_now', return_value=datetime.fromisoformat('2026-10-08T16:00:00+09:00')):
                fresh = fresh_bridge.capture_fresh_close(ticker, '2026-10-08', lambda:response)
            stocks[ticker] = dict(provenance=dict(evidence=[historical, fresh]))
        hashes, prices = probe._evidence(stocks, '2026-10-08', CREATED, CUTOFF)
        self.assertEqual(len(prices), 6)
        self.assertIn(historical['path'], hashes)
        self.assertNotEqual(history._key(), fresh_bridge._receipt_key())
        for rows in prices.values():
            self.assertEqual([r['session_date'] for r in rows], [DAY, '2026-10-08'])


if __name__ == '__main__':
    unittest.main()

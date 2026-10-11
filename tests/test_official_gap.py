import copy
import json
import tempfile
import unittest
from pathlib import Path

from engine import official_gap as gap
from engine import fresh_bridge as bridge
from engine import minimal_trial as base
from engine import historical_quality as quality


ROOT = base.ROOT


def fixtures():
    calendar = json.loads((ROOT / 'analysis/calendar_validation_2026-09-12.json').read_text())
    manifest = json.loads((ROOT / 'data/outcome_input/manual_jpx/retention_manifest.json').read_text())
    inbox = ROOT / 'data/outcome_input/manual_jpx'
    return calendar, manifest, inbox


class OfficialGapTests(unittest.TestCase):
    def test_exact_calendar_and_dynamic_previous(self):
        calendar, _, _ = fixtures()
        self.assertEqual(gap.expected_gap_sessions(calendar),
                         ['2026-09-14', '2026-09-15', '2026-09-16',
                          '2026-09-17', '2026-09-18', '2026-09-24'])
        self.assertEqual(bridge.expected_previous(calendar, '2026-09-28'), '2026-09-25')
        wrong = copy.deepcopy(calendar)
        wrong['jp_sessions'].remove('2026-09-24')
        with self.assertRaisesRegex(ValueError, 'JPX_CALENDAR_GAP_CONFLICT'):
            gap.expected_gap_sessions(wrong)

    def test_allowlist_duplicate_daily_caps_and_stop(self):
        calendar, manifest, inbox = fixtures()
        manifest['entries'] = [e for e in manifest['entries'] if e['session_date'] in gap.EXISTING_SESSIONS]
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        inbox = Path(temp.name)
        published = ('2026-09-14',)
        self.assertEqual(gap.plan_one_shot_backfill('2026-09-14', calendar, manifest, inbox,
                                                   published_sessions=published)['acquisition_type'], 'BACKFILL')
        with self.assertRaisesRegex(ValueError, 'SESSION_NOT_VISIBLY_PUBLISHED'):
            gap.plan_one_shot_backfill('2026-09-14', calendar, manifest, inbox)
        for session in ('2026-09-10', '2026-09-25', '2026-09-15'):
            with self.assertRaisesRegex(ValueError, 'SESSION_NOT_IN_EXACT_ALLOWLIST'):
                gap.plan_one_shot_backfill(session, calendar, manifest, inbox, published_sessions=published)
        with self.assertRaisesRegex(ValueError, 'OLDEST_MISSING_FIRST'):
            gap.plan_one_shot_backfill('2026-09-16', calendar, manifest, inbox,
                                       published_sessions=('2026-09-16',))
        with self.assertRaisesRegex(ValueError, 'DAILY_OR_BACKFILL_CAP'):
            gap.plan_one_shot_backfill('2026-09-14', calendar, manifest, inbox,
                                       successes_today=2, published_sessions=published)
        with self.assertRaisesRegex(ValueError, 'DAILY_OR_BACKFILL_CAP'):
            gap.plan_one_shot_backfill('2026-09-14', calendar, manifest, inbox,
                                       backfills_today=1, published_sessions=published)
        with self.assertRaisesRegex(ValueError, 'STOP_FOR_THE_DAY'):
            gap.plan_one_shot_backfill('2026-09-14', calendar, manifest, inbox,
                                       stopped=True, published_sessions=published)
        duplicate = copy.deepcopy(manifest)
        duplicate['entries'].append({'session_date': '2026-09-14'})
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_DOWNLOAD_BLOCKED'):
            gap.plan_one_shot_backfill('2026-09-14', calendar, duplicate, inbox,
                                       published_sessions=published)

    def test_existing_layers_provenance_and_raw_unchanged(self):
        calendar, manifest, inbox = fixtures()
        cases = [('2026-09-15', '2026-09-15_core6_validation_2026-09-18.json'),
                 ('2026-09-18', '2026-09-18_core6_one_shot_r2.json')]
        with tempfile.TemporaryDirectory() as temp:
            layers = []
            for day, name in cases:
                entry = next(e for e in manifest['entries'] if e['session_date'] == day)
                raw = inbox / entry['filename']
                before = base.sha256(raw)
                dest = Path(temp) / (day + '.json')
                layer = gap.stage_existing_pdf(ROOT / 'data/outcome_parsed/jpx' / name,
                                               manifest, calendar, inbox, dest)
                self.assertEqual(layer['core6_count'], 6)
                self.assertEqual(layer['source_hash'], before)
                self.assertEqual(base.sha256(raw), before)
                self.assertTrue(all(r['source'] == 'JPX_DAILY_REPORT' for r in layer['records']))
                with self.assertRaises(FileExistsError):
                    gap.stage_existing_pdf(ROOT / 'data/outcome_parsed/jpx' / name,
                                           manifest, calendar, inbox, dest)
                layers.append(layer)
            status = gap.continuity(calendar, layers)
            self.assertEqual(status['status'], 'GAP_REMAINS')
            self.assertFalse(status['interpolation'])
            self.assertEqual(status['missing_sessions'], list(gap.MISSING_ALLOWLIST))
            synthetic_complete = layers + [dict(session_date=day, source='JPX_DAILY_REPORT',
                                                core6_count=6, source_hash='synthetic',
                                                records=[dict(row, session_date=day, source_hash='synthetic')
                                                         for row in layers[0]['records']])
                                           for day in gap.MISSING_ALLOWLIST]
            self.assertEqual(gap.continuity(calendar, synthetic_complete)['status'], 'CONTINUOUS')
            history = quality.audit('6857')['records']
            self.assertEqual(gap.merge_price_series(history, layers, calendar, '6857')['status'], 'GAP_REMAINS')
            self.assertEqual(gap.merge_price_series(history, synthetic_complete, calendar, '6857')['status'],
                             'SOURCE_ADJUSTMENT_BASIS_UNVERIFIED')
            merged = gap.merge_price_series(history, synthetic_complete, calendar, '6857',
                                            basis_compatible=True)
            self.assertEqual(merged['status'], 'CONTINUOUS')
            self.assertEqual(merged['rows'][-1]['session_date'], '2026-09-24')
            self.assertEqual(merged['rows'][-1]['source'], 'JPX_DAILY_REPORT')

    def test_technical_terms_separate(self):
        config = json.loads((ROOT / 'config/official_gap_policy.json').read_text())
        self.assertEqual(config['TECHNICAL_STATUS'], 'CONTINUOUS_CANDIDATE_ADJUSTMENT_BASIS_UNVERIFIED')
        self.assertEqual(config['TERMS_STATUS'], 'UNRESOLVED')
        self.assertTrue(config['OFFICIAL_GAP_SERIES_READY'])
        self.assertTrue(config['HISTORICAL_TO_OFFICIAL_BRIDGE_READY'])
        self.assertTrue(config['BIGDATA_FRESH_BRIDGE_TECHNICALLY_VALIDATED'])
        self.assertTrue(config['LIVE_FEATURE_SNAPSHOT_TECHNICALLY_READY'])
        self.assertTrue(config['FEATURE_SERIES_OPERATIONALLY_COMPATIBLE_FOR_MINIMAL_TRIAL'])
        self.assertTrue(config['ADJUSTMENT_BASIS_COMPATIBLE_FOR_MINIMAL_TRIAL'])
        self.assertFalse(config['RECALCULATION_REQUIRED'])
        self.assertEqual(config['READY_FOR_LIVE_DRY_RUN'], 'YES')
        self.assertEqual(config['READY_FOR_ONE_SESSION_LIVE_DRY_RUN'], 'YES')
        self.assertFalse(config['FREE_FORWARD_TRIAL_ENABLED'])

    def test_real_bootstrap_continuity_and_candidate_features(self):
        calendar, manifest, inbox = fixtures()
        parsed_dir = ROOT / 'data/outcome_parsed/jpx'
        layer_dir = ROOT / 'data/fresh_bridge/jp/official_gap'
        layers = []
        for day in gap.expected_gap_sessions(calendar):
            entry = next(e for e in manifest['entries'] if e['session_date'] == day)
            source = inbox / entry['filename']
            before = base.sha256(source)
            self.assertEqual(before, entry['sha256'])
            if day in gap.MISSING_ALLOWLIST:
                parsed = parsed_dir / f'{day}_core6_manual_bootstrap.json'
            else:
                parsed = parsed_dir / ({'2026-09-15': '2026-09-15_core6_validation_2026-09-18.json',
                                        '2026-09-18': '2026-09-18_core6_one_shot_r2.json'}[day])
            self.assertEqual(json.loads(parsed.read_text(encoding='utf-8'))['session_date'], day)
            layer = json.loads((layer_dir / f'{day}_core6.json').read_text(encoding='utf-8'))
            self.assertEqual(layer['source_hash'], before)
            self.assertEqual(layer['parsed_output_hash'], base.sha256(parsed))
            self.assertEqual(layer['core6_count'], 6)
            self.assertEqual(base.sha256(source), before)
            layers.append(layer)
        state = gap.continuity(calendar, layers)
        self.assertEqual(state['status'], 'CONTINUOUS')
        self.assertEqual(state['missing_sessions'], [])
        self.assertFalse(state['interpolation'])
        limits = json.loads((ROOT / 'config/trial_thresholds_candidate.json').read_text())['thresholds']
        for ticker in base.CORE:
            history = quality.audit(ticker)['records']
            candidate = gap.candidate_features(history, layers, calendar, ticker, limits[ticker])
            self.assertEqual(candidate['as_of_session'], '2026-09-24')
            self.assertEqual(len(candidate['features']), 9)
            self.assertNotIn('ATR14_pct', candidate['features'])
            self.assertNotIn('volume_ratio_20d', candidate['features'])
            self.assertFalse(candidate['forecast_eligible'])
            self.assertEqual(candidate['official_source_hashes'], state['provenance'])
        with self.assertRaisesRegex(ValueError, 'GAP_REMAINS'):
            gap.candidate_features(quality.audit('6857')['records'], layers[:-1], calendar,
                                   '6857', limits['6857'])

    def test_wrong_pdf_session_rejected(self):
        from providers.jp.jpx_manual.parser import source_date
        with self.assertRaisesRegex(ValueError, 'DATA_CONFLICT'):
            source_date('2026年9月14日', '2026-09-16')


if __name__ == '__main__':
    unittest.main()

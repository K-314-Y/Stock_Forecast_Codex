import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from engine import jev_news as news
from engine.parallel_data import validate_numeric
from parallel_fixture import snapshot, SOURCE_REGISTRY


class NewsTests(unittest.TestCase):
    def setUp(self):
        self.event = dict(kind='news', event_id='synthetic-event-1', headline='Synthetic event',
                          body='INVENTED_NEWS_BODY_DO_NOT_PERSIST', source='SYNTHETIC_NEWS',
                          published_at='2025-06-30T20:00:00+09:00', available_at='2025-06-30T20:10:00+09:00',
                          target_mapping=['6857'])
        self.evaluator = Mock(version='jev synthetic-fixture')
        self.evaluator.evaluate.return_value = dict(scores={n: 3 for n in news.SCORES},
                                                    category='direct_company', model='synthetic-jev')

    def filter(self, events=None):
        return news.filter_news([self.event] if events is None else events, '2025-07-01',
                                '2025-07-01T08:00:00+09:00', self.evaluator, {'SYNTHETIC_NEWS'},
                                clock=lambda: news.stamp('2025-07-01T08:00:00+09:00'))

    def test_keep_rule_and_version_metadata(self):
        r = self.filter()[0]
        self.assertEqual(r['decision'], 'KEEP')
        self.assertEqual(r['jev_rule_version'], 'jev-news-filter-v1')
        self.assertEqual(r['jev_cli_version'], 'jev synthetic-fixture')
        self.assertEqual(r['jev_model_version'], 'synthetic-jev')
        self.assertEqual(r['processed_at'], '2025-07-01T08:00:00+09:00')

    def test_drop_threshold(self):
        self.evaluator.evaluate.return_value['scores']['materiality'] = 1
        r = self.filter()[0]
        self.assertEqual(r['decision'], 'DROP')
        self.assertIn('materiality', r['drop_reason'])

    def test_drop_irrelevant(self):
        self.evaluator.evaluate.return_value['category'] = 'irrelevant'
        self.assertEqual(self.filter()[0]['decision'], 'DROP')

    def test_duplicate_headline_removed_before_jev(self):
        second = dict(self.event, event_id='synthetic-2', headline='  SYNTHETIC   EVENT  ')
        result = self.filter([second, self.event])
        self.assertEqual([r['decision'] for r in result], ['KEEP', 'DROP'])
        self.assertEqual(result[1]['drop_reason'], 'DUPLICATE')
        self.assertEqual(self.evaluator.evaluate.call_count, 1)

    def test_duplicate_event_removed(self):
        result = self.filter([self.event, dict(self.event, headline='Other invented title')])
        self.assertEqual(sum(r['decision'] == 'KEEP' for r in result), 1)
        self.assertEqual(self.evaluator.evaluate.call_count, 1)

    def test_pit_drop_without_call(self):
        self.event['available_at'] = '2025-07-01T09:00:00+09:00'
        self.assertEqual(self.filter()[0]['drop_reason'], 'PIT_OR_PROCESSING_CUTOFF')
        self.evaluator.evaluate.assert_not_called()

    def test_unknown_source_drop(self):
        self.event['source'] = 'UNAPPROVED'
        self.assertEqual(self.filter()[0]['drop_reason'], 'SOURCE_NOT_APPROVED')
        self.evaluator.evaluate.assert_not_called()

    def test_call_finishing_after_cutoff_cannot_keep(self):
        result = news.filter_news([self.event], '2025-07-01', '2025-07-01T08:44:00+09:00',
                                  self.evaluator, {'SYNTHETIC_NEWS'},
                                  clock=lambda: news.stamp('2025-07-01T08:46:00+09:00'))
        self.assertEqual(result[0]['decision'], 'DROP')
        self.assertEqual(result[0]['processed_at'], '2025-07-01T08:46:00+09:00')

    def test_no_raw_persistence_in_keep_or_drop(self):
        result = self.filter([self.event, dict(self.event, event_id='2')])
        saved = json.dumps(result)
        self.assertNotIn(self.event['body'], saved)
        self.assertNotIn(self.event['headline'], saved)
        self.assertTrue(all('body' not in r and 'headline' not in r for r in result))

    def test_invalid_response_fail_closed(self):
        self.evaluator.evaluate.return_value['scores']['relevance'] = float('nan')
        self.assertEqual(self.filter()[0]['drop_reason'], 'JEV_UNAVAILABLE_OR_INVALID')

    def test_unavailable_missing_coverage_not_zero(self):
        self.evaluator.evaluate.side_effect = RuntimeError('test')
        result = news.news_aggregates(self.filter(), '6857', '2025-07-01', coverage_complete=True)
        self.assertTrue(all(v is None for v in result['values'].values()))

    def test_candidate_aggregate_only(self):
        result = news.news_aggregates(self.filter(), '6857', '2025-07-01', coverage_complete=True)
        self.assertEqual(result['values']['material_news_count'], 1)
        self.assertEqual(result['values']['has_direct_company_event'], 1)
        self.assertFalse(result['model_input'])

    def test_structured_numerical_bypasses_jev(self):
        snap = snapshot('2025-07-01')
        item = snap['stocks']['6857']['feature_metadata']['ret_1d']
        self.assertEqual(validate_numeric(item, 'ret_1d', snap['feature_end'], snap['cutoff'],
                                          snap['created_at'], SOURCE_REGISTRY), 'PASS')
        self.evaluator.evaluate.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'STRUCTURED_DATA_BYPASSES'):
            self.filter([dict(kind='numeric', value=123)])

    @patch('engine.jev_news.subprocess.run')
    def test_cli_stdin_json_contract_no_file_or_raw_diagnostics(self, run):
        answers = {n: dict(type='score', score=2.5) for n in news.SCORES}
        answers['category'] = dict(type='choice', choice='sector')
        run.side_effect = [subprocess.CompletedProcess([], 0, 'jev 2026.919.0'),
                           subprocess.CompletedProcess([], 0, json.dumps(dict(model='fixture', answers=answers)))]
        cli = news.JevCLI()
        result = cli.evaluate(self.event, '2025-07-01')
        self.assertEqual(result['category'], 'sector')
        call = run.call_args
        self.assertEqual(call.args[0], ['jev', 'ask', '-'])
        request = json.loads(call.kwargs['input'])
        self.assertEqual(set(request['questions']), set(news.SCORES) | {'category'})
        self.assertNotIn('capture-diagnostics', str(call))

    def test_installed_cli_request_schema_offline(self):
        import shutil
        if not shutil.which('jev'):
            self.skipTest('Optional Jev CLI absent; contract mock still tested')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'news-rule.json'
            path.write_text(json.dumps(news.request_for(self.event, '2025-07-01')))
            r = subprocess.run(['jev', 'presets', 'validate', str(path)], capture_output=True, text=True, timeout=10)
            self.assertEqual(r.returncode, 0, 'Installed Jev rejected offline request schema')

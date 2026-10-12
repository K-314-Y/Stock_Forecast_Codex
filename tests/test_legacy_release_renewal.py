"""Offline renewal chains. Evidence and Git histories are temporary fixtures."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from engine import minimal_trial as core
from providers.jp.jpx_manual import historical_policy_v2 as policy
from providers.jp.jpx_manual import legacy_stop_review as review
from tests import test_legacy_stop_review as fixture


class RenewalTests(unittest.TestCase):
    def setUp(self):
        fixture.LegacyReviewTests.setUp(self)
        self.generation1 = fixture.LegacyReviewTests.release(self)
        self.original_receipt = Path(self.generation1['path']).read_bytes()
        self.original_sidecar = Path(self.generation1['path']).with_suffix('.sha256').read_bytes()
        (self.root / '.gitignore').write_text('data/\nanalysis/\n')
        self.git('init', '-q')
        self.git('add', '.')
        self.commit('fixture base')
        self.base = self.git('rev-parse', 'HEAD').strip()

    def git(self, *args):
        result = subprocess.run(['git', '-C', str(self.root), *args], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def commit(self, message):
        self.git('-c', 'user.name=Offline Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-qm', message)

    def change_code(self):
        path = self.root / review.FIX_PATHS[0]
        path.write_bytes(path.read_bytes() + b'\n# temporary offline follow-up fixture\n')
        self.git('add', '-u')
        self.commit('fixture follow-up')

    def arguments_for_renewal(self):
        index = json.loads((self.store / 'current_stops_v2.json').read_bytes())['index']
        previous_ref = index['reviewed_legacy_releases'][-1]['receipt_reference']
        previous = core.read_frozen(previous_ref['path'])
        generation = len(index['reviewed_legacy_releases']) + 1
        fixes = [policy.reference(self.root / name) for name in review.FIX_PATHS]
        current = self.git('rev-parse', 'HEAD').strip()
        base = previous.get('current_code_commit', self.base)
        report = dict(schema_version=review.RENEWAL_VALIDATION_SCHEMA, status='PASS',
            generation=generation, previous_release_reference=previous_ref,
            previous_stop_reference=self.stop_ref, previous_fix_refs=previous['fix_refs'],
            fix_refs=fixes, review_evidence=self.evidence, discovery_inputs=self.inputs,
            base_code_commit=base, current_code_commit=current, live_jpx_requests=0,
            checks={key: 'PASS' for key in review.CHECKS}, suites={name: dict(
                status='PASS', tests_run=1, failures=0, errors=0) for name in review.RENEWAL_SUITES})
        # PASS assertions here are synthetic validator inputs, never real reports.
        path = self.store / ('renewal-pass-' + str(generation) + '.json')
        policy.seal(path, report)
        return dict(expected_stop_sha256=self.stop_ref['sha256'],
            expected_previous_release_sha256=previous_ref['sha256'], review_decision='APPROVED',
            renewal_reason='Follow-up fixture changes require explicit review.',
            root_cause='A reviewed root failure was mistaken for an interrupted new attempt.',
            resolution='Only the exact reviewed legacy failure can start one fresh attempt.',
            fix_refs=fixes, validation_refs=[policy.reference(path)],
            base_code_commit=base, current_code_commit=current)

    def renew(self, **changes):
        return review.manual_review_renewal(self.root, **{**self.arguments_for_renewal(), **changes})

    def valid_generation2(self):
        self.change_code()
        return self.renew()

    def rewrite_latest(self, change):
        index = json.loads((self.store / 'current_stops_v2.json').read_bytes())['index']
        entry = index['reviewed_legacy_releases'][-1]
        path = Path(entry['receipt_reference']['path'])
        receipt = core.read_frozen(path)
        receipt.update(change)
        path.write_bytes(core.canonical_bytes(receipt))
        path.with_suffix('.sha256').write_text(core.sha256(path) + '\n')
        entry['receipt_reference'] = policy.reference(path)
        policy.write_current(self.store / 'current_stops_v2.json', index)

    def assert_blocked(self):
        with self.assertRaisesRegex(ValueError, 'REVIEWED_LEGACY_RELEASE_INVALID'):
            policy.Stops(self.store)

    def test_b1_generation1_unchanged_gate_passes(self):
        self.assertIsNone(policy.Stops(self.store).gate(fixture.SESSION))

    def test_b2_stale_generation1_without_renewal_fails(self):
        self.change_code()
        self.assert_blocked()
        bootstrap = policy.Stops.__new__(policy.Stops)
        bootstrap._load_evidence(self.store)
        with self.assertRaisesRegex(ValueError, 'REVIEWED_LEGACY_RELEASE_INVALID'):
            bootstrap.gate(fixture.SESSION)

    def test_b3_generation2_bootstraps_stale_previous_source(self):
        ref = self.valid_generation2()
        receipt = core.read_frozen(ref['path'])
        self.assertEqual(receipt['generation'], 2)
        self.assertEqual(receipt['previous_release_reference'], self.generation1)
        self.assertEqual(receipt['previous_fix_refs'], self.fixes)
        self.assertIsNone(policy.Stops(self.store).gate(fixture.SESSION))
        self.guard.assert_not_called()

    def test_b4_generation1_and_stop_remain_unchanged(self):
        self.valid_generation2()
        self.assertEqual(Path(self.generation1['path']).read_bytes(), self.original_receipt)
        self.assertEqual(Path(self.generation1['path']).with_suffix('.sha256').read_bytes(), self.original_sidecar)
        self.assertEqual((self.store / 'STOP.json').read_bytes(), self.before)

    def test_b5_generation2_current_code_changed_fails(self):
        self.valid_generation2()
        self.change_code()
        self.assert_blocked()

    def test_b6_corrupt_generation2_fails(self):
        ref = self.valid_generation2()
        path = Path(ref['path'])
        path.write_bytes(path.read_bytes() + b' ')
        self.assert_blocked()

    def test_b7_missing_generation2_sidecar_fails(self):
        ref = self.valid_generation2()
        Path(ref['path']).with_suffix('.sha256').unlink()
        self.assert_blocked()

    def test_b8_orphan_generation2_fails(self):
        self.valid_generation2()
        index = json.loads((self.store / 'current_stops_v2.json').read_bytes())['index']
        index['reviewed_legacy_releases'].pop()
        policy.write_current(self.store / 'current_stops_v2.json', index)
        self.assert_blocked()

    def test_b9_duplicate_and_fork_generation_fail(self):
        ref = self.valid_generation2()
        index = json.loads((self.store / 'current_stops_v2.json').read_bytes())['index']
        index['reviewed_legacy_releases'].append(dict(index['reviewed_legacy_releases'][-1]))
        policy.write_current(self.store / 'current_stops_v2.json', index)
        self.assert_blocked()
        index['reviewed_legacy_releases'].pop()
        policy.write_current(self.store / 'current_stops_v2.json', index)
        policy.seal(self.store / review.FOLDER / 'sibling-generation2.json', core.read_frozen(ref['path']))
        self.assert_blocked()

    def test_missing_previous_lineage_cannot_bootstrap_renewal(self):
        self.change_code()
        args = self.arguments_for_renewal()
        index = json.loads((self.store / 'current_stops_v2.json').read_bytes())['index']
        index.pop('reviewed_legacy_releases')
        policy.write_current(self.store / 'current_stops_v2.json', index)
        with self.assertRaisesRegex(ValueError, 'REVIEWED_LEGACY_RELEASE_INVALID'):
            review.manual_review_renewal(self.root, **args)

    def test_damaged_stop_or_previous_receipt_cannot_bootstrap(self):
        self.change_code()
        args = self.arguments_for_renewal()
        for path in (self.store / 'STOP.json', self.store / 'STOP.sha256',
                     Path(self.generation1['path']), Path(self.generation1['path']).with_suffix('.sha256')):
            with self.subTest(path=path.name):
                before = path.read_bytes()
                path.write_bytes(before + b'fixture damage')
                with self.assertRaises(ValueError):
                    review.manual_review_renewal(self.root, **args)
                path.write_bytes(before)

    def test_wrong_previous_reference_generation_or_previous_fixes_fail(self):
        ref = self.valid_generation2()
        original = core.read_frozen(ref['path'])
        for change in (dict(previous_release_reference={**self.generation1, 'sha256': '0' * 64}),
                       dict(generation=3), dict(previous_fix_refs=[])):
            with self.subTest(change=change):
                self.rewrite_latest({**original, **change})
                self.assert_blocked()
    def test_b10_wrong_previous_receipt_sha_fails(self):
        self.valid_generation2()
        self.rewrite_latest(dict(previous_release_sha256='0' * 64))
        self.assert_blocked()

    def test_b11_wrong_stop_lineage_fails(self):
        self.valid_generation2()
        self.rewrite_latest(dict(previous_stop_reference={**self.stop_ref, 'sha256': '0' * 64}))
        self.assert_blocked()

    def test_b12_fresh_process_revalidates_chain(self):
        self.valid_generation2()
        code = ('from providers.jp.jpx_manual.historical_policy_v2 import Stops; '
                'import sys; assert Stops(sys.argv[1]).gate("2026-10-05") is None')
        result = subprocess.run([sys.executable, '-c', code, str(self.store)], cwd=fixture.PROJECT,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_generation3_is_linear_and_only_latest_binds_current(self):
        second = self.valid_generation2()
        second_bytes = Path(second['path']).read_bytes()
        self.change_code()
        third = self.renew()
        self.assertEqual(core.read_frozen(third['path'])['generation'], 3)
        self.assertIsNone(policy.Stops(self.store).gate(fixture.SESSION))
        self.assertEqual(Path(second['path']).read_bytes(), second_bytes)

    def test_old_evidence_stays_required_after_renewal(self):
        self.valid_generation2()
        for path in (Path(self.generation1['path']), self.report_path,
                     Path(self.evidence['path']), Path(self.inputs['monthly']['path'])):
            with self.subTest(path=path.name):
                before = path.read_bytes()
                path.write_bytes(before + b' ')
                self.assert_blocked()
                path.write_bytes(before)

    def test_missing_latest_receipt_is_not_approval(self):
        ref = self.valid_generation2()
        Path(ref['path']).unlink()
        self.assert_blocked()

    def test_explicit_approval_exact_stop_and_previous_sha_required(self):
        self.change_code()
        args = self.arguments_for_renewal()
        for change in (dict(review_decision='PENDING'), dict(expected_stop_sha256='0' * 64),
                       dict(expected_previous_release_sha256='0' * 64)):
            with self.subTest(change=change), self.assertRaises(ValueError):
                review.manual_review_renewal(self.root, **{**args, **change})

    def test_dirty_tree_or_wrong_commit_cannot_renew(self):
        self.change_code()
        args = self.arguments_for_renewal()
        with self.assertRaises(ValueError):
            review.manual_review_renewal(self.root, **{**args, 'current_code_commit': self.base})
        (self.root / 'untracked.py').write_text('# fixture\n')
        with self.assertRaises(ValueError):
            review.manual_review_renewal(self.root, **args)

    def test_other_active_stops_cannot_be_renewed_away(self):
        self.change_code()
        args = self.arguments_for_renewal()
        stops = policy.Stops.__new__(policy.Stops)
        stops._load_evidence(self.store)
        for reason in ('HTTP_403', 'CAPTCHA', 'SECURITY_CHALLENGE', 'BOT_CHALLENGE',
                       'ACCESS_DENIED', 'HASH_CONFLICT', 'PROVENANCE_CONFLICT',
                       'OHLCV_INVALID', 'SESSION_CONFLICT'):
            with self.subTest(reason=reason):
                stops.stop(reason, fixture.SESSION, evidence_refs=[], last_success=None,
                           run_id='fixture', retry_count=0)
                with self.assertRaisesRegex(ValueError, 'LEGACY_RENEWAL_OTHER_STOP_ACTIVE'):
                    review.manual_review_renewal(self.root, **args)

    def test_validation_pass_and_lineage_are_required(self):
        self.change_code()
        args = self.arguments_for_renewal()
        path = Path(args['validation_refs'][0]['path'])
        original = core.read_frozen(path)
        for change in (dict(status='FAIL'), dict(suites={}), dict(previous_fix_refs=[]),
                       dict(generation=3), dict(live_jpx_requests=1)):
            with self.subTest(change=change):
                path.write_bytes(core.canonical_bytes({**original, **change}))
                path.with_suffix('.sha256').write_text(core.sha256(path) + '\n')
                with self.assertRaises(ValueError):
                    review.manual_review_renewal(self.root, **{**args, 'validation_refs': [policy.reference(path)]})

    def test_crash_after_receipt_before_index_fails_closed(self):
        self.change_code()
        with patch.object(policy.Stops, 'save', side_effect=KeyboardInterrupt('fixture index crash')):
            with self.assertRaises(KeyboardInterrupt):
                self.renew()
        self.assert_blocked()
        self.assertEqual(Path(self.generation1['path']).read_bytes(), self.original_receipt)

    def test_crash_between_receipt_and_sidecar_fails_closed(self):
        self.change_code()
        link = policy.os.link
        def interrupt(source, target):
            if Path(target).suffix == '.sha256':
                raise KeyboardInterrupt('fixture sidecar crash')
            return link(source, target)
        args = self.arguments_for_renewal()
        with patch.object(policy.os, 'link', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                review.manual_review_renewal(self.root, **args)
        self.assert_blocked()


if __name__ == '__main__':
    unittest.main()

"""Exact pre-v2 root recognition; fresh requests are always mocked offline."""
from datetime import datetime, timezone
from pathlib import Path
import json
import unittest
from unittest.mock import patch

from engine import minimal_trial as core
from providers.jp.jpx_manual import historical_direct as direct
from providers.jp.jpx_manual import historical_policy_v2 as policy
from providers.jp.jpx_manual import legacy_stop_review as review
from tests import test_legacy_stop_review as fixture
from tests.test_jpx_historical_direct import listing, response, MockTransport


class LegacyAttemptTests(unittest.TestCase):
    def setUp(self):
        fixture.LegacyReviewTests.setUp(self)
        self.root_attempt = self.store / fixture.SESSION
        self.root_attempt.mkdir()
        policy.seal(self.root_attempt / 'start.json', dict(session=fixture.SESSION,
            acquisition_method=direct.METHOD, started_at='2026-10-09T12:01:24.172121+00:00',
            policy_sha256='1' * 64))
        policy.seal(self.root_attempt / 'failure.json', self.old)
        self.old_files = {p.name: p.read_bytes() for p in self.root_attempt.iterdir()}
        config = json.loads((fixture.PROJECT / 'config/jpx_daily_acquisition_config.json').read_bytes())
        (self.root / 'config/jpx_daily_acquisition_config.json').write_text(json.dumps(config))
        # Synthetic calendar; no local/private fixture dependency in public trees.
        calendar = dict(jp_sessions=['2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08',
                                     '2026-10-09', '2026-10-13'],
                        validation_range=dict(start='2026-10-01', end='2026-10-31'))
        (self.root / 'analysis/calendar_validation_2026-09-12.json').write_text(json.dumps(calendar))
        config['historical_direct']['calendar_sha256'] = core.sha256(self.root / config['historical_direct']['calendar'])
        (self.root / 'config/jpx_daily_acquisition_config.json').write_text(json.dumps(config))
        self.transport = MockTransport([response(listing(session='2026-10-06'))])
        self.clock = patch.object(direct, '_now', return_value=datetime(2026, 10, 12, 6, tzinfo=timezone.utc))
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def release(self):
        return fixture.LegacyReviewTests.release(self)

    def acquire(self):
        return direct.acquire_one(self.root, fixture.SESSION, transport=self.transport)

    def recognized(self):
        return review.reviewed_legacy_root_attempt(policy.Stops(self.store), fixture.SESSION, self.root_attempt)

    def assert_interrupted(self):
        with self.assertRaisesRegex(FileExistsError, 'INTERRUPTED_ATTEMPT_REVIEW_REQUIRED'):
            self.acquire()
        self.assertEqual(self.transport.calls, [])

    def test_a1_exact_reviewed_failure_starts_first_fresh_attempt(self):
        self.release()
        self.assertTrue(self.recognized())
        result = self.acquire()
        self.assertEqual(result['status'], 'NOT_YET_AVAILABLE')
        self.assertEqual(self.transport.calls, [direct.LISTING])
        self.assertTrue((self.root_attempt / 'attempts/000001/start.json').is_file())
        self.assertTrue((self.root_attempt / 'attempts/000001/result.json').is_file())
        self.assertFalse(self.recognized())
        self.guard.assert_not_called()

    def test_a2_unreviewed_attempt_is_blocked(self):
        with self.assertRaisesRegex(ValueError, 'HARD_STOP_ACTIVE'):
            self.acquire()
        self.assertFalse(self.recognized())
        self.assertEqual(self.transport.calls, [])

    def test_a3_wrong_failure_contents_are_blocked(self):
        self.release()
        path = self.root_attempt / 'failure.json'
        path.write_bytes(core.canonical_bytes({**self.old, 'reason': 'OTHER_UNKNOWN'}))
        path.with_suffix('.sha256').write_text(core.sha256(path) + '\n')
        self.assertFalse(self.recognized())
        self.assert_interrupted()

    def test_a4_wrong_hash_sidecar_or_equivalent_reencoded_bytes_are_blocked(self):
        self.release()
        path = self.root_attempt / 'failure.json'
        for body, sidecar in ((self.old_files['failure.json'], b'0' * 64),
                              (self.old_files['failure.json'] + b' ', None)):
            with self.subTest(body=body[-1:]):
                path.write_bytes(body)
                path.with_suffix('.sha256').write_bytes(sidecar or (core.sha256(path) + '\n').encode())
                self.assertFalse(self.recognized())
                self.assert_interrupted()

    def test_a5_extra_artifacts_are_blocked(self):
        self.release()
        for name in ('listing.html', 'acquisition.json', 'request-000001.json', 'raw.pdf', 'parsed.json'):
            with self.subTest(name=name):
                path = self.root_attempt / name
                path.write_text('temporary fixture')
                self.assertFalse(self.recognized())
                self.assert_interrupted()
                path.unlink()

    def test_a6_attempts_directory_even_empty_blocks_exception(self):
        self.release()
        (self.root_attempt / 'attempts').mkdir()
        self.assertFalse(self.recognized())
        self.assert_interrupted()

    def test_a7_crashed_fresh_attempt_cannot_start_again(self):
        self.release()
        with patch.object(direct, '_request', side_effect=KeyboardInterrupt('fresh fixture crash')) as request:
            with self.assertRaises(KeyboardInterrupt):
                self.acquire()
        self.assertEqual(request.call_count, 1)
        fresh = self.root_attempt / 'attempts/000001'
        self.assertTrue((fresh / 'start.json').is_file())
        self.assertFalse((fresh / 'result.json').exists())
        self.assert_interrupted()
        self.assertFalse((self.root_attempt / 'attempts/000002').exists())

    def test_a8_old_start_failure_and_sidecars_remain_unchanged(self):
        self.release()
        self.acquire()
        for name, contents in self.old_files.items():
            self.assertEqual((self.root_attempt / name).read_bytes(), contents)
        self.assertEqual((self.store / 'STOP.json').read_bytes(), self.before)

    def test_wrong_start_session_method_or_sidecar_blocks(self):
        self.release()
        path = self.root_attempt / 'start.json'
        start = core.read_frozen(path)
        for change in (dict(session='2026-10-06'), dict(acquisition_method='HUMAN_MANUAL_DOWNLOAD')):
            with self.subTest(change=change):
                path.write_bytes(core.canonical_bytes({**start, **change}))
                path.with_suffix('.sha256').write_text(core.sha256(path) + '\n')
                self.assert_interrupted()
        path.write_bytes(self.old_files['start.json'])
        path.with_suffix('.sha256').unlink()
        self.assert_interrupted()

    def test_unrelated_interrupted_root_is_not_recognized(self):
        self.release()
        other = self.store / '2026-10-06'
        other.mkdir()
        for name, contents in self.old_files.items():
            (other / name).write_bytes(contents)
        self.assertFalse(review.reviewed_legacy_root_attempt(policy.Stops(self.store), '2026-10-06', other))
        with self.assertRaisesRegex(FileExistsError, 'INTERRUPTED_ATTEMPT_REVIEW_REQUIRED'):
            direct.acquire_one(self.root, '2026-10-06', transport=self.transport)
        self.assertEqual(self.transport.calls, [])

    def test_symlink_or_missing_root_file_blocks(self):
        self.release()
        path = self.root_attempt / 'start.json'
        contents = path.read_bytes()
        path.unlink()
        self.assert_interrupted()
        outside = self.store / 'outside-start.json'
        outside.write_bytes(contents)
        path.symlink_to(outside)
        self.assert_interrupted()


if __name__ == '__main__':
    unittest.main()

"""General completed-session rules, independent of old target and Forecast ledger."""
from datetime import datetime, timedelta, timezone
import json
from unittest import TestCase
from unittest.mock import patch

from engine import minimal_trial as core
from providers.jp.jpx_manual import historical_direct as direct
from tests import test_jpx_policy_v2 as fixture


class GeneralAdmissionTests(TestCase):
    def setUp(self):
        fixture.HistoricalPolicyTests.setUp(self)
        self.days = ['2026-11-02', '2027-01-05', '2027-03-02']
        path = self.root / 'analysis/calendar_validation_2026-09-12.json'
        path.write_text(json.dumps(dict(jp_sessions=self.days,
            validation_range=dict(start=self.days[0], end='2027-03-31'))))
        self.config_path = self.root / 'config/jpx_daily_acquisition_config.json'
        self.config = json.loads(self.config_path.read_bytes())
        self.config['historical_direct']['calendar_sha256'] = core.sha256(path)
        self.save_policy()

    def save_policy(self):
        self.config_path.write_text(json.dumps(self.config))

    def now(self, day):
        return datetime.fromisoformat(day + 'T08:00:00+09:00') + timedelta(days=1)

    def transport(self, day):
        return fixture.Transport([fixture.response(fixture.html(day)),
                                  fixture.response(self.pdf, fixture.PDF_URL, kind='application/pdf')])

    def test_valid_completed_sessions_beyond_old_target_accepted(self):
        for day in self.days:
            with self.subTest(day=day), patch.object(direct, '_now', return_value=self.now(day)):
                result = direct.acquire_one(self.root, day, transport=self.transport(day))
                self.assertEqual(result['status'], 'VALIDATED_PENDING_EXPLICIT_REGISTRATION')
                self.assertFalse(result['automatic_registration'])

    def test_future_and_observation_day_sessions_rejected_without_request(self):
        transport = fixture.Transport([])
        for now in (self.now(self.days[0]) - timedelta(days=2), self.now(self.days[0]) - timedelta(days=1)):
            with patch.object(direct, '_now', return_value=now), self.assertRaisesRegex(ValueError, 'NOT_APPROVED'):
                direct.acquire_one(self.root, self.days[0], transport=transport)
        self.assertEqual(transport.calls, [])

    def test_unknown_calendar_day_rejected(self):
        transport = fixture.Transport([])
        with patch.object(direct, '_now', return_value=self.now(self.days[1])):
            with self.assertRaisesRegex(ValueError, 'NOT_APPROVED'):
                direct.acquire_one(self.root, '2027-01-04', transport=transport)
        self.assertEqual(transport.calls, [])

    def test_unvalidated_calendar_rejected(self):
        path = self.root / self.config['historical_direct']['calendar']
        path.write_bytes(path.read_bytes() + b' ')
        with self.assertRaisesRegex(ValueError, 'CALENDAR_HASH_MISMATCH'):
            direct.acquire_one(self.root, self.days[0], transport=fixture.Transport([]))

    def test_guessed_pdf_without_approved_discovery_never_requested(self):
        transport = fixture.Transport([fixture.response(fixture.html(self.days[1]))])
        with patch.object(direct, '_now', return_value=self.now(self.days[0])):
            result = direct.acquire_one(self.root, self.days[0], transport=transport)
        self.assertEqual(result['status'], 'NOT_YET_AVAILABLE')
        self.assertEqual(transport.calls, [direct.LISTING])

    def test_wrong_source_rejected_before_pdf_request(self):
        body = fixture.html(self.days[0]).replace('株式相場表'.encode(), '債券価格表'.encode())
        transport = fixture.Transport([fixture.response(body)])
        with patch.object(direct, '_now', return_value=self.now(self.days[0])):
            with self.assertRaises(ValueError):
                direct.acquire_one(self.root, self.days[0], transport=transport)
        self.assertEqual(transport.calls, [direct.LISTING])

    def test_unapproved_host_rejected(self):
        day = self.days[0]
        body = fixture.html(day).replace(b'https://www.jpx.co.jp/published/', b'https://evil.test/')
        transport = fixture.Transport([fixture.response(body)])
        with patch.object(direct, '_now', return_value=self.now(day)), self.assertRaises(ValueError):
            direct.acquire_one(self.root, day, transport=transport)
        self.assertEqual(transport.calls, [direct.LISTING])

    def test_security_challenge_rejected(self):
        day = self.days[0]
        transport = fixture.Transport([fixture.response(b'<html><title>Security challenge</title></html>')])
        with patch.object(direct, '_now', return_value=self.now(day)), self.assertRaisesRegex(ValueError, 'SECURITY_CHALLENGE'):
            direct.acquire_one(self.root, day, transport=transport)
        self.assertEqual(transport.calls, [direct.LISTING])

    def test_invalid_observation_timestamp_rejected_without_request(self):
        transport = fixture.Transport([])
        with patch.object(direct, '_now', return_value=datetime(2027, 1, 6)):
            with self.assertRaisesRegex(ValueError, 'CLOCK_INVALID'):
                direct.acquire_one(self.root, self.days[1], transport=transport)
        self.assertEqual(transport.calls, [])

    def test_policy_mismatch_rejected_without_request(self):
        self.config['historical_direct']['session_admission'] = 'ALLOW_ANY_SESSION'
        self.save_policy()
        with self.assertRaisesRegex(ValueError, 'POLICY'):
            direct.acquire_one(self.root, self.days[0], transport=fixture.Transport([]))

    def test_stale_discovery_timestamp_rejected(self):
        day = self.days[0]
        original = direct._request
        def stale(*args, **kwargs):
            response, chain = original(*args, **kwargs)
            if args[1] == direct.LISTING:
                chain = tuple(dict(row, completed_at=(self.now(day) - timedelta(days=1)).isoformat()) for row in chain)
            return response, chain
        with patch.object(direct, '_now', return_value=self.now(day)), patch.object(direct, '_request', side_effect=stale):
            with self.assertRaisesRegex(ValueError, 'ACQUISITION_CLOCK_INVALID'):
                direct.acquire_one(self.root, day, transport=self.transport(day))

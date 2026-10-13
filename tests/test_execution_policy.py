"""Scope and next validated session rules, without provider access."""
import unittest
from unittest.mock import patch
from engine import execution_policy as p
from tests import test_fresh_ephemeral as fixtures
TARGET = fixtures.TARGET


class ExecutionPolicyTests(unittest.TestCase):
    setUp = fixtures.EphemeralFreshTests.setUp
    write = fixtures.EphemeralFreshTests.write

    def test_personal_noncommercial_gate_open_for_next_target_only(self):
        self.assertTrue(p.execution_gates(p.SCOPE, TARGET)['forecast_allowed'])
        self.assertEqual(p.next_target_session(), TARGET)

    def test_all_other_scopes_closed(self):
        for scope in p.load_policy()['prohibited_scopes'] + ['', 'PERSONAL', None]:
            self.assertFalse(p.execution_gates(scope, TARGET)['forecast_allowed'])
            with self.assertRaisesRegex(ValueError, 'GATE_CLOSED'):
                p.require_execution_scope(scope, TARGET)

    def test_permanently_missed_session_and_future_target_closed(self):
        self.assertFalse(p.execution_gates(p.SCOPE, '2026-10-13')['forecast_allowed'])
        self.assertFalse(p.execution_gates(p.SCOPE, '2026-10-15')['forecast_allowed'])

    def test_schedules_closed_even_with_personal_scope(self):
        self.assertFalse(p.execution_gates(p.SCOPE, TARGET, scheduled=True)['forecast_allowed'])

    def test_formal_closed_even_with_personal_scope(self):
        self.assertFalse(p.execution_gates(p.SCOPE, TARGET, formal=True)['forecast_allowed'])

    def test_calendar_modification_rejected(self):
        (self.root / p.load_policy()['calendar']).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'CALENDAR_HASH'):
            p.next_target_session()


if __name__ == '__main__': unittest.main()

"""Public-tree scope checks without acquired inputs or live calls."""
import unittest

from engine import execution_policy as p


class ExecutionPolicyTests(unittest.TestCase):
    def test_personal_noncommercial_gate_open_for_target_only(self):
        gates = p.execution_gates(p.SCOPE, '2026-10-13')
        for name in ('forecast_execution_enabled', 'forward_trial_enabled', 'forecast_allowed', 'trial_enabled'):
            self.assertTrue(gates[name])
        self.assertEqual(gates['terms_status'], 'CONDITIONAL_PERSONAL_NONCOMMERCIAL')

    def test_all_other_scopes_closed(self):
        for scope in p.load_policy()['prohibited_scopes'] + ['', 'PERSONAL', None]:
            with self.subTest(scope=scope):
                self.assertFalse(p.execution_gates(scope, '2026-10-13')['forecast_allowed'])
                with self.assertRaisesRegex(ValueError, 'GATE_CLOSED'):
                    p.require_execution_scope(scope, '2026-10-13')

    def test_other_target_closed(self):
        self.assertFalse(p.execution_gates(p.SCOPE, '2026-10-14')['forecast_allowed'])

    def test_schedules_closed_even_with_personal_scope(self):
        gates = p.execution_gates(p.SCOPE, '2026-10-13', scheduled=True)
        self.assertFalse(gates['forecast_allowed'])
        self.assertFalse(gates['schedules_enabled'])

    def test_formal_closed_even_with_personal_scope(self):
        gates = p.execution_gates(p.SCOPE, '2026-10-13', formal=True)
        self.assertFalse(gates['forecast_allowed'])
        self.assertFalse(gates['formal_forecast_enabled'])


if __name__ == '__main__':
    unittest.main()

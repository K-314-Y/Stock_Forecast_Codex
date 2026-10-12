"""JST observation-day regressions; synthetic responses and temporary state only."""
from datetime import datetime, timezone
import multiprocessing
import unittest
from unittest.mock import patch

import test_jpx_challenge_ledger_regression as fixtures
from test_jpx_policy_v2 import Transport, response, html, SESSION, OTHER
from providers.jp.jpx_manual import historical_direct as d
from providers.jp.jpx_manual import historical_policy_v2 as policy


class JSTObservationDayTests(unittest.TestCase):
    fixture = fixtures.FinalAdversarialTests.fixture

    def observe(self, fixture, when, run_id):
        transport = Transport([response(html(OTHER))])
        with patch.object(d, '_now', return_value=when):
            result = d.acquire_one(fixture.root, SESSION, transport=transport,
                                   run_state=d.RunState(run_id))
        self.assertEqual(transport.calls, [d.LISTING])
        self.assertEqual(result['status'], 'NOT_YET_AVAILABLE')
        self.assertEqual(result['completed_at'], when.isoformat())  # UTC evidence stays UTC.
        stops = policy.Stops(fixture.store)
        budget = stops.index['retry_targets'][SESSION]
        d.RequestContext(d.RunState('verify'), SESSION, stops=stops).validate_budget(budget)
        return result, budget

    def assert_completed(self, budget, expected_day):
        self.assertEqual(budget['day'], expected_day)
        self.assertEqual(budget['retry_count'], 0)
        self.assertFalse(budget['in_flight'])
        self.assertFalse(budget['exhausted'])
        self.assertEqual(len(budget['requests']), 1)
        operation = next(iter(budget['requests'].values()))
        self.assertEqual(operation['state'], 'COMPLETED')
        self.assertEqual(operation['attempts_started'], 1)
        receipt = policy.read_reference(operation['completion_reference'])
        self.assertEqual(receipt['observation_day'], expected_day)

    def test_jst_midnight_allows_one_fresh_observation(self):
        before = datetime(2026, 10, 11, 14, 59, tzinfo=timezone.utc)  # 23:59 JST.
        after = datetime(2026, 10, 11, 15, 1, tzinfo=timezone.utc)  # Next day 00:01 JST.
        with self.fixture() as fixture:
            _, old = self.observe(fixture, before, 'before-midnight')
            self.assert_completed(old, '2026-10-11')
            result, new = self.observe(fixture, after, 'after-midnight')
            self.assertEqual(result['web_requests'], 1)
            self.assert_completed(new, '2026-10-12')

    def test_utc_midnight_within_same_jst_day_reuses_observation(self):
        before = datetime(2026, 10, 11, 15, 1, tzinfo=timezone.utc)  # 00:01 JST.
        after = datetime(2026, 10, 12, 0, 1, tzinfo=timezone.utc)  # Same day 09:01 JST.
        with self.fixture() as fixture:
            first, old = self.observe(fixture, before, 'before-utc-midnight')
            self.assert_completed(old, '2026-10-12')
            transport = Transport([response(html(OTHER))])
            with patch.object(d, '_now', return_value=after):
                result = d.acquire_one(fixture.root, SESSION, transport=transport,
                                       run_state=d.RunState('after-utc-midnight'))
            self.assertEqual(transport.calls, [])
            self.assertEqual(result, dict(first, web_requests=0))
            new = policy.Stops(fixture.store).index['retry_targets'][SESSION]
            self.assertEqual(new, old)
            self.assert_completed(new, '2026-10-12')

    def test_two_process_jst_rollover_has_one_fresh_observation(self):
        before = datetime(2026, 10, 11, 14, 59, tzinfo=timezone.utc)
        after = datetime(2026, 10, 11, 15, 1, tzinfo=timezone.utc)
        with self.fixture() as fixture:
            self.observe(fixture, before, 'before-midnight')
            ctx = multiprocessing.get_context('fork')
            ready, count, results = ctx.Event(), ctx.Value('i', 0), ctx.Queue()
            def worker(number):
                class Counted(Transport):
                    def get(self, url, *, limit):
                        with count.get_lock():
                            count.value += 1
                        return super().get(url, limit=limit)
                ready.wait(10)
                try:
                    with patch.object(d, '_now', return_value=after):
                        result = d.acquire_one(fixture.root, SESSION,
                            transport=Counted([response(html(OTHER))]),
                            run_state=d.RunState('worker-' + str(number)))
                    results.put((result['status'], result['web_requests']))
                except Exception as error:
                    results.put(str(error))
            processes = [ctx.Process(target=worker, args=(number,)) for number in (1, 2)]
            try:
                for process in processes:
                    process.start()
                ready.set()
                outcomes = [results.get(timeout=20) for _ in processes]
                for process in processes:
                    process.join(20)
                    self.assertEqual(process.exitcode, 0)
                self.assertCountEqual(outcomes, [('NOT_YET_AVAILABLE', 1), ('NOT_YET_AVAILABLE', 0)])
                self.assertEqual(count.value, 1)
                self.assert_completed(policy.Stops(fixture.store).index['retry_targets'][SESSION], '2026-10-12')
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                        process.join(5)
                results.close()

    def test_0030_jst_uses_jst_calendar_date(self):
        when = datetime(2026, 10, 11, 15, 30, tzinfo=timezone.utc)  # 00:30 JST on Oct 12.
        self.assertEqual(d._jpx_observation_day(when), '2026-10-12')
        with patch.object(d, '_now', return_value=when):
            self.assertEqual(d._jpx_observation_day(), '2026-10-12')
        with self.fixture() as fixture:
            result, budget = self.observe(fixture, when, '0030-jst')
            self.assert_completed(budget, '2026-10-12')
            self.assertEqual(next(iter(result['completed_requests'].values()))['observation_day'], '2026-10-12')

    def test_existing_valid_bundle_skips_across_jst_midnight(self):
        before = datetime(2026, 10, 11, 14, 59, tzinfo=timezone.utc)
        after = datetime(2026, 10, 11, 15, 1, tzinfo=timezone.utc)
        with self.fixture() as fixture:
            with patch.object(d, '_now', return_value=before):
                result = d.acquire_one(fixture.root, SESSION, transport=fixture.acquisition_transport())
            self.assertEqual(result['state'], 'PASS')
            old = policy.Stops(fixture.store).index['retry_targets'][SESSION]
            transport = Transport([])
            with patch.object(d, '_now', return_value=after):
                result = d.acquire_one(fixture.root, SESSION, transport=transport,
                                       run_state=d.RunState('valid-next-day'))
            self.assertEqual(result['status'], 'SKIP_EXISTING_LOCAL')
            self.assertEqual(result['local_validation'], 'PASS')
            self.assertEqual(result['web_requests'], 0)
            self.assertEqual(transport.calls, [])
            self.assertEqual(policy.Stops(fixture.store).index['retry_targets'][SESSION], old)


if __name__ == '__main__':
    unittest.main()

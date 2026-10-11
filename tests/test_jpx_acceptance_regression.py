"""A1--A3 acceptance regressions through offline production entry points."""
from copy import deepcopy
from datetime import timedelta
import json
import multiprocessing
from pathlib import Path
import unittest
from unittest.mock import patch

import test_jpx_policy_v2 as fixtures
from test_jpx_policy_v2 import Transport, response, html, SESSION, OTHER, NOW, PDF_URL
import test_jpx_challenge_ledger_regression as previous
from providers.jp.jpx_manual import historical_direct as d
from providers.jp.jpx_manual import historical_policy_v2 as policy


class AcceptanceRegressionTests(unittest.TestCase):
    fixture = previous.FinalAdversarialTests.fixture
    assert_normal = previous.FinalAdversarialTests.assert_normal
    assert_challenge = previous.FinalAdversarialTests.assert_challenge

    def complete(self, fixture, session=SESSION, *, values=None):
        transport = Transport(values or [response(html(OTHER if session == SESSION else SESSION))])
        result = d.acquire_one(fixture.root, session, transport=transport)
        self.assertIn(result['state'], ('PASS', 'WARN', 'SKIP'))
        return policy.Stops(fixture.store).index['retry_targets'][session]

    def change(self, fixture, edit):
        with fixtures.frozen._locked(fixture.store):
            stops = policy.Stops(fixture.store)
            edit(stops.index['retry_targets'][SESSION])
            stops.save()
        return deepcopy(policy.Stops(fixture.store).index['retry_targets'][SESSION])

    def assert_closed(self, fixture, before):
        transport = Transport([response()])
        with patch.object(d, '_now', return_value=NOW + timedelta(days=1)):
            with self.assertRaisesRegex(ValueError, '^RETRY_INTERRUPTED_REVIEW_REQUIRED$'):
                d.discover_json_one(fixture.root, SESSION, transport=transport,
                    evidence_folder=fixture.root / 'restart', run_state=d.RunState('restart'))
        self.assertEqual(transport.calls, [])
        self.assertEqual(policy.Stops(fixture.store).index['retry_targets'][SESSION], before)
        self.assertEqual(policy.Stops(fixture.store).index['active'], [])

    def test_a1_forged_response_log_reference_rejects_rollover(self):
        with self.fixture() as fixture:
            self.complete(fixture)
            log = fixture.store / SESSION / 'request-0001.json'
            self.assertEqual(policy.read_reference(policy.reference(log))['status'], 200)
            before = self.change(fixture, lambda b: next(iter(b['requests'].values())).update(
                result_reference=policy.reference(log)))
            self.assert_closed(fixture, before)

    def test_a1_cross_session_result_reference_rejects_rollover(self):
        with self.fixture() as fixture:
            self.complete(fixture)
            other = self.complete(fixture, OTHER)
            other_ref = next(iter(other['requests'].values()))['result_reference']
            self.assertEqual(policy.read_reference(other_ref)['session'], OTHER)
            before = self.change(fixture, lambda b: next(iter(b['requests'].values())).update(
                result_reference=other_ref))
            self.assert_closed(fixture, before)

    def test_a1_cross_role_completion_rejects_rollover(self):
        with self.fixture() as fixture:
            budget = self.complete(fixture, values=[response(), response(fixture.pdf, PDF_URL, kind='application/pdf')])
            pdf = next(op for op in budget['requests'].values() if op['role'] == 'pdf')
            def edit(b):
                listing = next(op for op in b['requests'].values() if op['role'] == 'listing')
                listing['completion_reference'] = pdf.get('completion_reference', pdf['result_reference'])
            self.assert_closed(fixture, self.change(fixture, edit))

    def test_a1_cross_request_key_completion_rejects_rollover(self):
        with self.fixture() as fixture:
            observed = d.LISTING + 'observed-index/'
            budget = self.complete(fixture, values=[response(b'', status=302, Location=observed),
                response(html(OTHER), observed)])
            operations = list(budget['requests'].values())
            self.assertEqual(len(operations), 2)
            def edit(b):
                first, second = b['requests'].values()
                first['completion_reference'] = second.get('completion_reference', second['result_reference'])
            self.assert_closed(fixture, self.change(fixture, edit))

    def test_a1_unrelated_frozen_artifact_rejects_rollover(self):
        with self.fixture() as fixture:
            self.complete(fixture)
            path = fixture.root / 'unrelated.json'
            policy.seal(path, dict(state='PASS', session=SESSION, completed_at=NOW.isoformat()))
            before = self.change(fixture, lambda b: next(iter(b['requests'].values())).update(
                result_reference=policy.reference(path)))
            self.assert_closed(fixture, before)

    def test_a1_valid_completion_allows_next_day_fresh_observation(self):
        with self.fixture() as fixture:
            old = self.complete(fixture)
            with patch.object(d, '_now', return_value=NOW + timedelta(days=1)):
                transport = Transport([response(html(OTHER))])
                result = d.acquire_one(fixture.root, SESSION, transport=transport, run_state=d.RunState('later'))
            self.assertEqual(result['status'], 'NOT_YET_AVAILABLE')
            self.assertEqual(transport.calls, [d.LISTING])
            new = policy.Stops(fixture.store).index['retry_targets'][SESSION]
            self.assertNotEqual(new['day'], old['day'])
            self.assertEqual(new['retry_count'], 0)
            self.assertEqual(next(iter(new['requests'].values()))['attempts_started'], 1)

    def forge_receipt(self, fixture, budget, *, result_ref=None, changes=None):
        key, operation = next(iter(budget['requests'].items()))
        receipt = dict(schema_version='jpx-request-completion-v1', policy_version=policy.VERSION,
            state='COMPLETED', session=SESSION, request_key=key, canonical_url=operation['url'],
            role=operation['role'], observation_day=budget['day'], attempts_started=operation['attempts_started'],
            result_reference=result_ref or operation['result_reference'],
            completed_at=policy.read_reference(operation['result_reference'])['completed_at'])
        receipt.update(changes or {})
        path = fixture.root / 'forged-receipt.json'
        policy.seal(path, receipt)
        def edit(b):
            b['requests'][key].update(completion_reference=policy.reference(path),
                                     result_reference=receipt['result_reference'])
        return self.change(fixture, edit)

    def test_a1_checksum_valid_receipt_cannot_authorize_unrelated_underlying_result(self):
        for source in ('response-log', 'cross-session', 'unrelated', 'missing-type', 'wrong-state', 'wrong-operation'):
            with self.subTest(source=source), self.fixture() as fixture:
                budget = self.complete(fixture)
                if source == 'response-log':
                    ref = policy.reference(fixture.store / SESSION / 'request-0001.json')
                elif source == 'cross-session':
                    other = self.complete(fixture, OTHER)
                    ref = next(iter(other['requests'].values()))['result_reference']
                else:
                    result = deepcopy(policy.read_reference(next(iter(budget['requests'].values()))['result_reference']))
                    if source == 'unrelated':
                        result = dict(state='PASS', session=SESSION)
                    elif source == 'missing-type':
                        result.pop('schema_version', None)
                    elif source == 'wrong-state':
                        result['state'] = 'RETRY'
                    else:
                        result['completed_requests'] = {'0' * 64: dict(session=SESSION, url=PDF_URL, role='pdf')}
                    path = fixture.root / 'forged-result.json'
                    policy.seal(path, result)
                    ref = policy.reference(path)
                self.assert_closed(fixture, self.forge_receipt(fixture, budget, result_ref=ref))

    def test_a1_receipt_schema_and_ownership_matrix_rejects_rollover(self):
        for changes in (dict(schema_version='unknown'), dict(policy_version='unknown'), dict(state='PASS'),
                        dict(session=OTHER), dict(request_key='0' * 64), dict(role='pdf'),
                        dict(canonical_url=PDF_URL), dict(attempts_started=True),
                        dict(observation_day='2026-10-10'), dict(completed_at='2026-10-11T05:00:00')):
            with self.subTest(changes=changes), self.fixture() as fixture:
                budget = self.complete(fixture)
                self.assert_closed(fixture, self.forge_receipt(fixture, budget, changes=changes))

    def test_a1_receipts_are_create_only_and_precede_completed_index(self):
        with self.fixture() as fixture:
            seal = policy.seal
            records = []
            def inspect(path, record):
                if record.get('schema_version') == 'jpx-request-completion-v1':
                    budget = policy.Stops(fixture.store).index['retry_targets'][SESSION]
                    self.assertEqual(budget['requests'][record['request_key']]['state'], 'RESPONSE_RECEIVED')
                    records.append((path, deepcopy(record)))
                return seal(path, record)
            with patch.object(policy, 'seal', side_effect=inspect):
                budget = self.complete(fixture)
            self.assertEqual(len(records), 1)
            path, receipt = records[0]
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                seal(path, receipt)
            self.assertEqual(path.read_bytes(), before)
            operation = next(iter(budget['requests'].values()))
            self.assertEqual(policy.read_reference(operation['completion_reference']), receipt)
            self.assertEqual(receipt['result_reference'], operation['result_reference'])

    def test_a1_invalid_receipt_hash_cannot_rollover(self):
        with self.fixture() as fixture:
            budget = self.complete(fixture)
            operation = next(iter(budget['requests'].values()))
            reference = operation.get('completion_reference', operation['result_reference'])
            Path(reference['path']).write_bytes(b'corrupt completion receipt')
            self.assert_closed(fixture, budget)

    def test_a1_concurrent_valid_rollover_has_one_fresh_observation(self):
        with self.fixture() as fixture:
            self.complete(fixture)
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
                    with patch.object(d, '_now', return_value=NOW + timedelta(days=1)):
                        result = d.acquire_one(fixture.root, SESSION, transport=Counted([response(html(OTHER))]),
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
                budget = policy.Stops(fixture.store).index['retry_targets'][SESSION]
                self.assertEqual(next(iter(budget['requests'].values()))['attempts_started'], 1)
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                        process.join(5)

    def test_a1_completed_json_discovery_can_rollover_with_its_own_roles(self):
        markup = html().replace(b'</html>', b'<script>'
            b"const BASE_PATH = '/published/index/'; const BUCKNUMBER_JSON = BASE_PATH + 'months.json';"
            b'const jsonUrl = `${BASE_PATH}listed_${month}.json`; '
            b"item.Month; data.TableDatas; row.TradeDate; 'Stocks'; row[key];</script></html>")
        list_url = 'https://www.jpx.co.jp/published/index/months.json'
        month_url = 'https://www.jpx.co.jp/published/index/listed_202610.json'
        def transport():
            return Transport([response(markup), response(b'{"TableDatas":[{"Month":"202610"}]}', list_url,
                kind='application/json'), response(json.dumps(dict(TableDatas=[dict(TradeDate='20261005',
                Stocks=PDF_URL)])).encode(), month_url, kind='application/json')])
        with self.fixture() as fixture:
            first = transport()
            result = d.discover_json_one(fixture.root, SESSION, transport=first,
                evidence_folder=fixture.root / 'first', run_state=d.RunState('first'))
            self.assertEqual(result['status'], 'HREF_DISCOVERED')
            with patch.object(d, '_now', return_value=NOW + timedelta(days=1)):
                later = transport()
                d.discover_json_one(fixture.root, SESSION, transport=later,
                    evidence_folder=fixture.root / 'later', run_state=d.RunState('later'))
            self.assertEqual(later.calls, [d.LISTING, list_url, month_url])
            budget = policy.Stops(fixture.store).index['retry_targets'][SESSION]
            self.assertEqual({op['role'] for op in budget['requests'].values()}, {'listing', 'monthly_list', 'monthly'})
            self.assertTrue(all(op['attempts_started'] == 1 and op['state'] == 'COMPLETED'
                                for op in budget['requests'].values()))

    def test_a2_invalid_attempt_counters_before_rollover(self):
        for value in ('missing', None, -1, 4, '1', True):
            with self.subTest(value=value), self.fixture() as fixture:
                self.complete(fixture)
                def edit(b):
                    operation = next(iter(b['requests'].values()))
                    if value == 'missing':
                        operation.pop('attempts_started')
                    else:
                        operation['attempts_started'] = value
                before = self.change(fixture, edit)
                with patch.object(policy, 'verify_reference', wraps=policy.verify_reference) as verify:
                    self.assert_closed(fixture, before)
                verify.assert_not_called()  # Entire old schema precedes completion evidence reads.

    def test_a2_invalid_top_level_budget_before_rollover(self):
        for field, value in (('retry_count', 99), ('retry_count', None), ('retry_count', True),
                             ('in_flight', 'false'), ('in_flight', None), ('exhausted', 0),
                             ('day', '2026-99-99'), ('day', None), ('requests', []),
                             ('requests', None), ('requests', {}), ('requests', {'bad': None})):
            with self.subTest(field=field, value=value), self.fixture() as fixture:
                self.complete(fixture)
                def edit(b):
                    if value is None:
                        b.pop(field)
                    else:
                        b[field] = value
                before = self.change(fixture, edit)
                with patch.object(policy, 'verify_reference', wraps=policy.verify_reference) as verify:
                    self.assert_closed(fixture, before)
                verify.assert_not_called()

    def test_a2_identity_integrity_before_rollover(self):
        for field, value in (('key', '0' * 64), ('session', OTHER), ('session', None),
                             ('role', 'pdf'), ('role', None), ('url', PDF_URL), ('url', None),
                             ('state', 'UNKNOWN'), ('state', None)):
            with self.subTest(field=field, value=value), self.fixture() as fixture:
                self.complete(fixture)
                def edit(b):
                    key, operation = next(iter(b['requests'].items()))
                    if field == 'key':
                        b['requests'][value] = b['requests'].pop(key)
                    elif value is None:
                        operation.pop(field)
                    else:
                        operation[field] = value
                self.assert_closed(fixture, self.change(fixture, edit))

    def test_a2_entire_ledger_is_checked_before_any_completion_evidence(self):
        with self.fixture() as fixture:
            self.complete(fixture, values=[response(), response(fixture.pdf, PDF_URL, kind='application/pdf')])
            def edit(b):
                next(op for op in b['requests'].values() if op['role'] == 'pdf')['attempts_started'] = -1
            before = self.change(fixture, edit)
            with patch.object(policy, 'verify_reference', wraps=policy.verify_reference) as verify:
                self.assert_closed(fixture, before)
            verify.assert_not_called()

    def test_a2_inconsistent_states_cannot_reset(self):
        for edit in (lambda b: b.update(in_flight=True),
                     lambda b: b.update(exhausted=True),
                     lambda b: next(iter(b['requests'].values())).update(state='PENDING'),
                     lambda b: next(iter(b['requests'].values())).update(state='RETRY_EXHAUSTED')):
            with self.subTest(edit=edit), self.fixture() as fixture:
                self.complete(fixture)
                self.assert_closed(fixture, self.change(fixture, edit))

    def test_a3_generic_identifiers_in_documentation_are_normal(self):
        for fragment in (
            b'<h1 id="security-challenge">Security challenge documentation</h1>',
            b'<h2 id="security-challenge">Security challenge documentation</h2>',
            b'<section class="human-verification">Human verification explanation</section>',
            b'<div id="browser-verification">Browser verification guide</div>',
            b'<article id="challenge">Challenge documentation</article>',
            b'<h2 class="verification">Verification guide</h2>',
            b'<h2 id="challenge-platform">Challenge platform documentation</h2>',
            b'<div class="bot-challenge">Bot challenge guide</div>',
            b'<button id="challenge">Continue reading</button>',
            b'<input name="verification" type="submit" value="Continue reading">',
            b'<div src="/security-challenge">Security challenge documentation</div>',
            b'<h2 id="security-challenge">Security challenge documentation</h2><button>Continue</button>',
            b'<form id="security-challenge">Security challenge documentation</form>',
            b'<form class="human-verification">Human verification guide</form>',
            b'<form id="security-challenge" action="/docs/security-guide">Security challenge documentation'
            b'<input type="submit" value="Continue reading"></form>',
        ):
            with self.subTest(fragment=fragment):
                self.assert_normal(fragment)

    def test_a3_real_challenge_control_matrix_stops(self):
        for fragment in (
            b'<form action="/security-challenge"><input type="submit" value="Continue"></form>',
            b'<iframe src="/security-challenge"></iframe>',
            b'<script src="/challenge-platform/control.js"></script>',
            b'<form id="security-challenge"><button>Verify</button></form>',
            b'<form class="human-verification"><input type="submit" value="Verify human"></form>',
            b'<section class="human-verification"><button>Verify</button></section>',
            b'<button id="security-challenge">Verify</button>',
            b'<div class="g-recaptcha"></div>', b'<div class="h-captcha"></div>',
            b'<div class="cf-turnstile"></div>',
            b'<div role="alert">Access<br>restricted</div><button>Verify</button>',
        ):
            with self.subTest(fragment=fragment):
                self.assert_challenge(fragment)


if __name__ == '__main__':
    unittest.main()

"""Offline regressions for final publication checks and calendar-selected windows."""
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
import json
import tempfile
import unittest

from scripts import forecast_not_run_ledger as ledger
from scripts import install_jpx_publication_monitor as installer
from scripts import monitor_jpx_publication as m
from tests import test_jpx_publication_monitor as fixture

response = fixture.response


class FinalCheckTests(unittest.TestCase):
    setUp = fixture.MonitorTests.setUp
    run_check = fixture.MonitorTests.run_check

    def test_final_check_closes_hourly_deadline_hole(self):
        self.run_check(now=m.DEADLINE - timedelta(minutes=46))
        result = self.run_check(response([{'TradeDate':m.SESSION.replace('-',''),
                                         'Stocks':'/observed/final.pdf'}]),
                                now=m.DEADLINE - timedelta(minutes=10), final_check=True)
        self.assertEqual(result['result_state'],m.DETECTED)
        self.assertTrue(result['final_publication_check'])
        self.assertEqual(len(self.calls),2)
        self.assertEqual(result['source_identity']['publication_metadata']['resolved_href'],
                         'https://www.jpx.co.jp/observed/final.pdf')

    def test_final_check_once_even_when_absent(self):
        now=m.DEADLINE-timedelta(minutes=10)
        self.run_check(now=now,final_check=True)
        before=(self.folder/'latest.json').read_bytes()
        self.run_check(now=now+timedelta(minutes=1),final_check=True)
        self.assertEqual(len(self.calls),1)
        self.assertEqual((self.folder/'latest.json').read_bytes(),before)

    def test_concurrent_final_calls_make_one_get(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.run_check(now=m.DEADLINE-timedelta(minutes=10),
                                                         final_check=True), range(2)))
        self.assertEqual(len(self.calls),1)
        self.assertEqual(results[0],results[1])

    def test_final_check_before_0835_is_zero_network_stop(self):
        result=self.run_check(now=m.DEADLINE-timedelta(minutes=10,microseconds=1),final_check=True)
        self.assertEqual(result['stop_reason'],'FINAL_CHECK_OUTSIDE_APPROVED_WINDOW')
        self.assertFalse(self.calls)

    def test_final_check_after_0840_is_zero_network_stop(self):
        result=self.run_check(now=m.DEADLINE-timedelta(minutes=5)+timedelta(microseconds=1),final_check=True)
        self.assertEqual(result['stop_reason'],'FINAL_CHECK_OUTSIDE_APPROVED_WINDOW')
        self.assertFalse(self.calls)

    def test_final_absence_deadline_writes_ledger_zero_http(self):
        self.run_check(now=m.DEADLINE-timedelta(minutes=10),final_check=True)
        result=self.run_check(now=m.DEADLINE,final_check=True)
        self.assertEqual(result['result_state'],m.EXPIRED)
        self.assertEqual(len(self.calls),1)
        self.assertIn(m.TARGET,ledger.sessions(self.root,self.policy))
        self.assertEqual(json.loads((self.root/'config/forward_trial_execution_policy.json').read_bytes()),
                         self.policy)

    def test_late_final_check_is_bookkeeping_only(self):
        result=self.run_check(now=m.DEADLINE+timedelta(seconds=1),final_check=True)
        self.assertEqual(result['result_state'],m.EXPIRED)
        self.assertFalse(self.calls)

    def test_security_stop_prevents_final_check(self):
        self.run_check(response(status=403),now=m.DEADLINE-timedelta(minutes=46))
        result=self.run_check(now=m.DEADLINE-timedelta(minutes=10),final_check=True)
        self.assertEqual(result['result_state'],m.STOP)
        self.assertEqual(len(self.calls),1)

    def test_final_redirect_is_never_followed(self):
        result=self.run_check(response(status=302,location='https://external.test/x'),
                              now=m.DEADLINE-timedelta(minutes=10),final_check=True)
        self.assertEqual(result['stop_reason'],'UNEXPECTED_REDIRECT')
        self.assertEqual(len(self.calls),1)

    def test_interrupted_final_reservation_stops_without_retry(self):
        now=m.DEADLINE-timedelta(minutes=10)
        self.folder.mkdir(parents=True)
        m.write_state(self.folder/'final_check_reserved.json',dict(target_session=m.TARGET,
                      deadline=m.DEADLINE.isoformat(),reserved_at=now.isoformat()))
        m.write_state(self.folder/'latest.json',dict(target_session=m.SESSION,
                      checked_at_jst=now.isoformat(),result_state=m.PENDING))
        result=self.run_check(now=now+timedelta(seconds=1),final_check=True)
        self.assertEqual(result['stop_reason'],'INTERRUPTED_CHECK_REVIEW_REQUIRED')
        self.assertFalse(self.calls)

    def test_corrupt_final_reservation_is_fail_closed(self):
        now=m.DEADLINE-timedelta(minutes=10)
        self.run_check(now=now,final_check=True)
        (self.folder/'final_check_reserved.json').write_text('{}')
        result=self.run_check(now=now+timedelta(seconds=1),final_check=True)
        self.assertEqual(result['result_state'],m.STOP)
        self.assertEqual(len(self.calls),1)

    def test_missing_final_reservation_does_not_reset_one_use(self):
        now=m.DEADLINE-timedelta(minutes=10)
        self.run_check(now=now,final_check=True)
        (self.folder/'final_check_reserved.json').unlink()
        result=self.run_check(now=now+timedelta(seconds=1),final_check=True)
        self.assertEqual(result['stop_reason'],'FINAL_CHECK_RESERVATION_MISSING')
        self.assertEqual(len(self.calls),1)

    def test_units_pin_final_and_deadline_to_current_window(self):
        units=installer.units(self.root,'/usr/bin/python3',pinned=True)
        final=units[m.UNIT+'-final.timer']
        self.assertIn((m.DEADLINE-m.FINAL_CHECK_LEAD).strftime('OnCalendar=%Y-%m-%d %H:%M:%S'),final)
        self.assertIn('Persistent=true',final)
        self.assertIn('--final-check',units[m.UNIT+'-final.service'])
        self.assertIn('--target-session '+m.TARGET,units[m.UNIT+'-final.service'])
        self.assertIn('--finalize',units[m.UNIT+'-deadline.service'])
        self.assertNotIn('--final-check',units[m.UNIT+'-deadline.service'])
        self.assertIn(m.UNIT+'-final.timer',units[m.UNIT+'-disarm.service'])


class CalendarWindowTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name); (self.root/'config').mkdir()
        # Deliberately omit an ordinary weekday: selection must follow the calendar.
        self.days=['2030-01-04','2030-01-07','2030-01-09','2030-01-10']
        calendar=self.root/'calendar.json'; calendar.write_bytes(m.canonical({'jp_sessions':self.days}))
        self.rules=dict(calendar='calendar.json',calendar_sha256=m.digest(calendar),
                        permanently_not_run_sessions=['2030-01-07'])
        (self.root/'config/forward_trial_execution_policy.json').write_bytes(m.canonical(self.rules))
        self.now=datetime.fromisoformat('2030-01-07T15:00:00+09:00')

    def test_next_target_and_previous_use_calendar_not_weekdays(self):
        self.assertEqual(m.calendar_window(self.root,now=self.now),dict(target_session='2030-01-09',
                          previous_session='2030-01-07',deadline='2030-01-09T08:45:00+09:00'))

    def test_runtime_exclusion_affects_target_selection(self):
        ledger.record(self.root,'2030-01-09',reason='PERMANENTLY_NOT_RUN',source='synthetic',
                      recorded_at=self.now.isoformat())
        self.assertEqual(m.calendar_window(self.root,now=self.now)['target_session'],'2030-01-10')

    def test_cutoff_advances_window_but_explicit_pin_does_not(self):
        now=datetime.fromisoformat('2030-01-09T08:45:00+09:00')
        self.assertEqual(m.calendar_window(self.root,now=now)['target_session'],'2030-01-10')
        self.assertEqual(m.calendar_window(self.root,'2030-01-09',now=now)['target_session'],'2030-01-09')

    def test_calendar_hash_mismatch_and_invalid_target_rejected(self):
        with self.assertRaisesRegex(ValueError,'TARGET_INVALID'):
            m.calendar_window(self.root,'2030-01-08')
        (self.root/'calendar.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'HASH_MISMATCH'):
            m.calendar_window(self.root,now=self.now)

    def test_not_run_migration_survives_static_restore(self):
        for day in ('2026-10-13','2026-10-14'):
            ledger.record(self.root,day,reason='PERMANENTLY_NOT_RUN',source='synthetic migration',
                          recorded_at=self.now.isoformat())
        self.assertEqual(ledger.sessions(self.root,{'permanently_not_run_sessions':[]}),
                         {'2026-10-13','2026-10-14'})


class RetainedDiscoveryTests(unittest.TestCase):
    def setUp(self):
        from engine import forward_storage as storage
        from providers.jp.jpx_manual import historical_direct as direct
        from providers.jp.jpx_manual import historical_policy_v2 as lifecycle
        from tests import test_legacy_stop_review as source_fixture
        self.direct=direct
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name); (self.root/'config').mkdir()
        self.folder=self.root/'state/jpx_publication_monitor'/m.SESSION
        self.discovery=self.root/'data/retained'; self.discovery.mkdir(parents=True)
        self.reference='data/retained/discovery.json'
        listing=source_fixture.response(source_fixture.HTML)
        program=direct.json_endpoints(listing)
        monthlist=source_fixture.jr([dict(Month=m.SESSION[:7].replace('-',''))],
                                   program['monthly_list_endpoint'])
        url=direct.monthly_json_url(monthlist,program,m.SESSION)
        monthly=source_fixture.jr([],url)
        hashes={}; chains={}
        for label,value in dict(listing=listing,monthly_list=monthlist,monthly=monthly).items():
            path=self.discovery/(label+'.body'); path.write_bytes(value.body)
            hashes[label]=m.digest(path)
            chains[label]=[dict(url=value.url,response_url=value.url,status=200,headers=value.headers,
                                 response_sha256=hashes[label])]
            storage.seal(self.discovery/(label+'.response.json'),dict(url=value.url,chain=chains[label],
                                                                     sha256=hashes[label]))
        self.receipt=dict(schema_version='jpx-json-discovery-v1',acquisition_method=direct.METHOD,
                          session=m.SESSION[:8]+'09',pdf_requests=0,discovery=program,
                          listing_sha256=hashes['listing'],monthly_list_sha256=hashes['monthly_list'],
                          monthly_json_sha256=hashes['monthly'],monthly_json_reference_url=url,
                          redirect_chains=chains)
        storage.seal(self.discovery/'discovery.json',self.receipt)
        calendar=self.root/'calendar.json'; calendar.write_bytes(m.canonical({'jp_sessions':[m.SESSION,m.TARGET]}))
        rules=dict(calendar='calendar.json',calendar_sha256=m.digest(calendar),
                   permanently_not_run_sessions=[m.SESSION])
        (self.root/'config/forward_trial_execution_policy.json').write_bytes(m.canonical(rules))
        for relative in ('config/jpx_daily_acquisition_config.json','config/primary_forward_trial_v1.json',
                         'data/jpx_historical_direct/STOP.json','data/jpx_historical_direct/current_stops_v2.json'):
            path=self.root/relative; path.parent.mkdir(parents=True,exist_ok=True); path.write_text('{}')
        (self.root/'config/primary_forward_trial_v1.json').write_bytes(m.canonical({'forecast_store':'forecasts'}))
        for relative in m.CODE_PATHS:
            path=self.root/relative; path.parent.mkdir(parents=True,exist_ok=True); path.write_text('# fixture')
        guard=patch.object(lifecycle,'Stops'); self.stops=guard.start(); self.addCleanup(guard.stop)

    def test_same_month_official_discovery_replays_with_no_network_and_create_only(self):
        import socket
        with patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden')):
            context=installer.create_context(self.root,self.reference)
        self.assertEqual(context['monthly_url'],self.receipt['monthly_json_reference_url'])
        self.stops.return_value.gate.assert_called_once_with(self.receipt['session'])
        m.write_state(self.folder/'armed.json',dict(target_session=m.SESSION,deadline=m.DEADLINE.isoformat(),
                      code_commit='a'*40,code_hashes={p:m.digest(self.root/p) for p in m.CODE_PATHS}))
        with patch.object(m.subprocess,'check_output',return_value=m.BRANCH+'\n'):
            self.assertEqual(m.verify_context(self.root,self.folder),(context['monthly_url'],'a'*40))
        with self.assertRaisesRegex(ValueError,'CREATE_ONLY'):
            installer.create_context(self.root,self.reference)

    def test_changed_body_cannot_become_new_context(self):
        (self.discovery/'monthly.body').write_text('{}')
        with self.assertRaisesRegex(ValueError,'BODY_HASH_CONFLICT'):
            installer.create_context(self.root,self.reference)
        self.assertFalse(self.folder.exists())

    def test_existing_security_gate_blocks_context(self):
        self.stops.return_value.gate.side_effect=ValueError('HARD_STOP_ACTIVE')
        with self.assertRaisesRegex(ValueError,'HARD_STOP_ACTIVE'):
            installer.create_context(self.root,self.reference)
        self.assertFalse(self.folder.exists())

    def test_context_rejects_other_month_even_with_valid_seal(self):
        from engine import forward_storage as storage
        for suffix in ('.json','.sha256'):
            (self.discovery/('discovery'+suffix)).unlink()
        storage.seal(self.discovery/'discovery.json',dict(self.receipt,session='2026-09-30'))
        with self.assertRaisesRegex(ValueError,'DISCOVERY_IDENTITY_FAILURE'):
            installer.create_context(self.root,self.reference)
        self.assertFalse(self.folder.exists())

    def test_deadline_does_not_exclude_an_existing_forecast(self):
        installer.create_context(self.root,self.reference)
        bundle=self.root/'forecasts/frozen'; bundle.mkdir(parents=True)
        (bundle/'receipt.json').write_bytes(m.canonical({'target_session':m.TARGET}))
        with self.assertRaisesRegex(ValueError,'FROZEN_FORECAST_PRESENT'):
            m.record_not_run(self.root,m.DEADLINE.isoformat())
        self.assertNotIn(m.TARGET,ledger.sessions(self.root,{'permanently_not_run_sessions':[]}))


if __name__=='__main__':
    unittest.main()

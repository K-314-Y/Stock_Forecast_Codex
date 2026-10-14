"""Public-tree, synthetic, offline efficiency and fail-closed regressions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import ast
import hashlib
import fcntl
import hmac
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from engine import execution_policy, forward_artifacts, forward_storage
from engine import forward_trial_inputs as inputs
from engine import historical_close_bridge as historical
from engine import minimal_trial as core
from engine import prospective_pit
from engine.parallel_data import digest
from providers.jp.jpx_manual import acquisition_binding as binding
from providers.jp.jpx_manual import historical_policy_v2 as stops_policy
from providers.jp.jpx_manual import legacy_stop_review as review
from scripts import forecast_not_run_ledger as ledger
from scripts import install_jpx_publication_monitor as installer
from scripts import monitor_jpx_publication as monitor
from tests import test_legacy_release_renewal as renewal_fixture
from tests import test_legacy_stop_review as release_fixture
from tests import test_jpx_publication_monitor as monitor_fixture


class ComponentBindingTests(unittest.TestCase):
    setUp = renewal_fixture.RenewalTests.setUp
    git = renewal_fixture.RenewalTests.git
    commit = renewal_fixture.RenewalTests.commit
    change_code = renewal_fixture.RenewalTests.change_code
    arguments_for_renewal = renewal_fixture.RenewalTests.arguments_for_renewal
    renew = renewal_fixture.RenewalTests.renew

    def test_unrelated_committed_and_dirty_changes_do_not_require_renewal(self):
        self.change_code()
        ref = self.renew()
        before = Path(ref['path']).read_bytes()
        for relative in ('scripts/monitor_jpx_publication.py', 'README.md', 'engine/unrelated.py'):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# unrelated synthetic change\n')
        self.git('add', '.')
        self.commit('unrelated fixture commit')
        self.assertIsNone(stops_policy.Stops(self.store).gate('2026-10-05'))
        (self.root / 'README.md').write_text('# dirty unrelated change\n')
        self.assertIsNone(stops_policy.Stops(self.store).gate('2026-10-05'))
        self.assertEqual(Path(ref['path']).read_bytes(), before)
        self.guard.assert_not_called()

    def test_each_acquisition_component_modification_closes_gate(self):
        self.change_code()
        self.renew()
        for relative in (*binding.FILES, 'engine/forward_artifacts.py',
                         'engine/forward_storage.py', binding.BINDER, binding.COMPATIBILITY):
            path = self.root / relative
            before = path.read_bytes()
            changed = before + b'\n# changed component\n'
            if relative == 'engine/forward_artifacts.py':
                changed = before.replace(b'seal as _seal', b'commit as _seal')
            if relative == 'engine/forward_storage.py':
                changed = before.replace(b'def seal(path, record):', b'def seal(path, record):\n    pass')
            with self.subTest(relative=relative):
                path.write_bytes(changed)
                with self.assertRaisesRegex(ValueError, 'REVIEWED_LEGACY_RELEASE_INVALID'):
                    stops_policy.Stops(self.store).gate('2026-10-05')
                path.write_bytes(before)

    def test_package_initializer_addition_and_body_change_close_gate(self):
        self.change_code()
        self.renew()
        for relative in binding.PACKAGE_INITIALIZERS:
            path = self.root / relative
            with self.subTest(relative=relative):
                path.write_bytes(b'# new package initializer\n')
                with self.assertRaises(ValueError):
                    stops_policy.Stops(self.store)
                first = binding.components(self.root)['package:' + relative]
                path.write_bytes(b'# changed package initializer\n')
                self.assertNotEqual(first, binding.components(self.root)['package:' + relative])
                with self.assertRaises(ValueError):
                    stops_policy.Stops(self.store)
                path.unlink()
                self.assertIsNone(stops_policy.Stops(self.store).gate('2026-10-05'))

    def test_dangling_initializer_symlink_fails_closed(self):
        self.change_code()
        self.renew()
        for relative in binding.PACKAGE_INITIALIZERS:
            path = self.root / relative
            with self.subTest(relative=relative):
                path.symlink_to(self.root / 'missing-initializer')
                with self.assertRaisesRegex(ValueError, 'ACQUISITION_COMPONENT_PATH_INVALID'):
                    binding.components(self.root)
                path.unlink()

    def test_relevant_engine_helper_changes_close_but_unrelated_functions_do_not(self):
        self.change_code()
        self.renew()
        path = self.root / 'engine/minimal_trial.py'
        before = path.read_bytes()
        path.write_bytes(before.replace(b'def sha256(path):', b'def sha256(path):\n    pass'))
        with self.assertRaises(ValueError):
            stops_policy.Stops(self.store)
        path.write_bytes(before.replace(b'def features(rows, frozen_threshold):',
                                       b'def features(rows, frozen_threshold):\n    pass'))
        self.assertIsNone(stops_policy.Stops(self.store).gate('2026-10-05'))

    def test_top_level_rebindings_are_bound(self):
        self.change_code()
        self.renew()
        path = self.root / 'engine/minimal_trial.py'
        before = path.read_bytes()
        for mutation in (b'CORE: tuple = ("EVIL",)\n',
                         b'if True:\n    CORE = ("EVIL",)\n',
                         b'globals()["CORE"] = ("EVIL",)\n'):
            with self.subTest(mutation=mutation):
                path.write_bytes(before + b'\n' + mutation)
                with self.assertRaises(ValueError):
                    stops_policy.Stops(self.store)
        path.write_bytes(before)

    def test_compatibility_parses_the_exact_checkpointed_bytes(self):
        root=self.root
        base='f60fbba965ee858ca6e7a3afefbdb4e09655a4f0'
        value=dict(schema_version=binding.VERSION,base_commit=base,before={'a':'b'},after={'safe':'map'})
        original=json.dumps(value).encode()
        changed=json.dumps({**value,'after':{'unsafe':'replacement'}}).encode()
        reads=[]
        def read(root,path):
            reads.append(path)
            if path == binding.COMPATIBILITY:
                return original if reads.count(path)==1 else changed
            return b'binder'
        def git(root,*args):
            if args[0]=='log':
                return b'checkpoint\n'
            if args[0]=='show':
                return original if args[1].endswith(binding.COMPATIBILITY) else b'binder'
            return b''
        with patch.object(binding,'_bytes',side_effect=read), patch.object(binding,'git',side_effect=git), \
             patch.object(binding,'components',return_value={'a':'b'}):
            self.assertEqual(binding._compatibility(root),value)
        self.assertEqual(reads.count(binding.COMPATIBILITY),1)

    def test_symlink_component_and_missing_source_fail_closed(self):
        self.change_code()
        self.renew()
        path = self.root / 'providers/jp/jpx_manual/parser.py'
        body = path.read_bytes()
        path.unlink()
        with self.assertRaises(ValueError):
            stops_policy.Stops(self.store)
        elsewhere = self.root / 'same-parser.py'
        elsewhere.write_bytes(body)
        path.symlink_to(elsewhere)
        with self.assertRaises(ValueError):
            stops_policy.Stops(self.store)

    def test_old_renewal_without_fingerprint_uses_commit_component_snapshot(self):
        self.change_code()
        ref = self.renew()
        value = core.read_frozen(ref['path'])
        value.pop('component_fingerprint')
        Path(ref['path']).write_bytes(core.canonical_bytes(value))
        Path(ref['path']).with_suffix('.sha256').write_text(core.sha256(ref['path']))
        index = ledger.strict_json((self.store / 'current_stops_v2.json').read_bytes())['index']
        index['reviewed_legacy_releases'][-1]['receipt_reference'] = stops_policy.reference(ref['path'])
        stops_policy.write_current(self.store / 'current_stops_v2.json', index)
        (self.root / 'README.md').write_text('# unrelated old receipt fixture\n')
        self.git('add', '.')
        self.commit('unrelated old receipt fixture')
        self.assertIsNone(stops_policy.Stops(self.store).gate('2026-10-05'))
        parser = self.root / 'providers/jp/jpx_manual/parser.py'
        parser.write_bytes(parser.read_bytes() + b'\n# parser changed\n')
        with self.assertRaises(ValueError):
            stops_policy.Stops(self.store)


class LegacyAuthoringTests(unittest.TestCase):
    setUp = release_fixture.LegacyReviewTests.setUp
    release = release_fixture.LegacyReviewTests.release

    def test_initial_release_cannot_approve_stale_validation_refs(self):
        path = self.root / review.FIX_PATHS[0]
        path.write_bytes(path.read_bytes() + b'\n# changed after PASS report\n')
        with self.assertRaises(ValueError):
            self.release()
        self.assertFalse((self.store / review.FOLDER).exists())


class RuntimeNotRunTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'config').mkdir()
        project=Path(__file__).resolve().parents[1]
        self.rules = ledger.strict_json((project/'config/forward_trial_execution_policy.json').read_bytes())
        (self.root/'config/minimal_forward_trial_config.json').write_bytes(
            (project/'config/minimal_forward_trial_config.json').read_bytes())
        (self.root / 'config/forward_trial_execution_policy.json').write_bytes(ledger.canonical(self.rules))
        self.at = monitor.DEADLINE.isoformat()
        self.kwargs = dict(reason='FORECAST_DEADLINE_EXPIRED', source='synthetic-test', recorded_at=self.at)

    @property
    def path(self):
        return self.root / 'state/forecast_not_run_sessions/2026-10-14.json'

    def record(self, **changes):
        return ledger.record(self.root, monitor.TARGET, **{**self.kwargs, **changes})

    def test_runtime_not_run_blocks_target_and_prospective_consumer(self):
        self.record()
        self.assertEqual(ledger.sessions(self.root, self.rules), {'2026-10-13', monitor.TARGET})
        with patch.object(core, 'ROOT', self.root):
            self.assertIn(monitor.TARGET, execution_policy.not_run_sessions())
            with patch.object(execution_policy, 'validated_calendar', return_value={'jp_sessions':[monitor.TARGET]}):
                with self.assertRaisesRegex(ValueError, 'EXHAUSTED'):
                    execution_policy.next_target_session(monitor.DEADLINE - timedelta(minutes=1))
            value = dict(schema_version='prospective-pre-session-v1', target_session=monitor.TARGET,
                         rows=[{}], ticker_statuses={ticker:{} for ticker in core.CORE})
            value['ledger_sha256'] = digest(value)
            with self.assertRaisesRegex(ValueError, 'PERMANENTLY_NOT_RUN'):
                prospective_pit.validate_pre_session(value)

    def test_corrupt_hash_json_duplicate_keys_and_aware_time_fail_closed(self):
        self.record()
        original = self.path.read_bytes()
        value = ledger.strict_json(original)
        changes = [b'{', original + b'bad', ledger.canonical({**value, 'sha256':'0'*64}),
                   original.replace(b'"sha256":', b'"sha256":"duplicate","sha256":')]
        record = dict(value['record'], recorded_at='2026-10-14T08:45:00')
        changes.append(ledger.canonical(dict(record=record, sha256=hashlib.sha256(ledger.canonical(record)).hexdigest())))
        for body in changes:
            with self.subTest(body=body[:30]):
                self.path.write_bytes(body)
                with self.assertRaisesRegex(ValueError, 'LEDGER_INVALID'):
                    ledger.sessions(self.root, self.rules)
                with patch.object(core, 'ROOT', self.root):
                    self.assertFalse(execution_policy.execution_gates(execution_policy.SCOPE, monitor.TARGET)['forecast_allowed'])
        self.path.write_bytes(original)

    def test_duplicate_is_idempotent_and_conflicts_are_rejected(self):
        self.record()
        before = self.path.read_bytes()
        self.record(recorded_at=(monitor.DEADLINE + timedelta(hours=1)).isoformat())
        self.assertEqual(self.path.read_bytes(), before)
        for changes in ({'reason':'OTHER'}, {'source':'OTHER'}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'CONFLICTING_DUPLICATE'):
                self.record(**changes)
        with self.assertRaisesRegex(ValueError, 'CLOCK_ROLLBACK'):
            self.record(recorded_at=(monitor.DEADLINE - timedelta(seconds=1)).isoformat())
        self.assertEqual(self.path.read_bytes(), before)

    def test_concurrent_writers_commit_one_idempotent_record(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            records = list(pool.map(lambda _: self.record(), range(6)))
        self.assertTrue(all(record == records[0] for record in records))
        self.assertEqual(ledger.sessions(self.root, self.rules), {'2026-10-13', monitor.TARGET})

    def test_initial_witness_is_complete_and_locked_before_atomic_publication(self):
        actual=ledger.os.link
        observations=[]
        def link(source,target,**kwargs):
            if target == ledger.WITNESS:
                fd=os.open(source,os.O_RDWR,dir_fd=kwargs['src_dir_fd'])
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    value=ledger.strict_json(os.read(fd,1024))
                    self.assertEqual(value['record']['schema_version'],ledger.SCHEMA)
                    observations.append(target)
                finally:
                    os.close(fd)
            return actual(source,target,**kwargs)
        with patch.object(ledger.os,'link',side_effect=link):
            self.record()
        self.assertEqual(observations,[ledger.WITNESS])

    def test_transient_two_link_witness_waits_before_link_count_validation(self):
        self.record()
        actual=ledger.os.fstat
        observations=[]
        def info(fd):
            value=actual(fd)
            if not observations:
                observations.append(fd)
                return Mock(st_mode=value.st_mode,st_nlink=2)
            return value
        with patch.object(ledger.os,'fstat',side_effect=info):
            self.record()
        self.assertEqual(len(observations),1)

    def test_missing_published_record_or_inventory_fails_closed(self):
        self.record()
        original = self.path.read_bytes()
        self.path.unlink()
        with self.assertRaises(ValueError):
            ledger.sessions(self.root, self.rules)
        self.record()  # Exact durable inventory repairs a missing publication.
        self.assertEqual(self.path.read_bytes(), original)
        inventory = self.path.parent / '.inventory.json'
        inventory.unlink()
        with self.assertRaises(ValueError):
            ledger.sessions(self.root, self.rules)
        with self.assertRaises(ValueError):
            self.record()

    def test_loss_of_entire_ledger_or_state_directory_fails_closed(self):
        self.record()
        self.assertTrue((self.root/ledger.WITNESS).exists())
        shutil.rmtree(self.path.parent)
        with self.assertRaises(ValueError):
            ledger.sessions(self.root,self.rules)
        with self.assertRaises(ValueError):
            self.record()
        self.path.parent.mkdir(parents=True)
        with self.assertRaises(ValueError):
            self.record()
        shutil.rmtree(self.root/'state')
        with self.assertRaises(ValueError):
            ledger.sessions(self.root,self.rules)

    def test_symlink_directory_entry_lock_and_path_escape_fail_closed(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.root / 'state').symlink_to(outside)
        with self.assertRaises(ValueError):
            self.record()
        self.assertEqual(list(outside.iterdir()), [])
        (self.root / 'state').unlink()
        self.record()
        for name in ('2026-10-14.json', '.lock'):
            path = self.path.parent / name
            body = path.read_bytes()
            path.unlink()
            path.symlink_to(outside / 'missing')
            with self.assertRaises(ValueError):
                ledger.sessions(self.root, self.rules)
            with self.assertRaises(ValueError):
                self.record()
            path.unlink()
            path.write_bytes(body)
        with self.assertRaises(ValueError):
            ledger.record(self.root, '../escape', **self.kwargs)
        with self.assertRaises(ValueError):
            ledger.record(self.root, '/absolute', **self.kwargs)

    def test_crash_before_publish_has_no_partial_final_and_after_publish_is_durable(self):
        with patch.object(ledger.os, 'link', side_effect=KeyboardInterrupt('before publication')):
            with self.assertRaises(KeyboardInterrupt):
                self.record()
        self.assertFalse(self.path.exists())
        with self.assertRaises(ValueError):
            ledger.sessions(self.root, self.rules)
        self.record()  # Reserved intent is recovered with exact original bytes.
        self.path.unlink()
        actual_fsync = ledger.os.fsync
        def sync(fd):
            if self.path.exists():
                raise KeyboardInterrupt('after publication')
            return actual_fsync(fd)
        with patch.object(ledger.os, 'fsync', side_effect=sync):
            with self.assertRaises(KeyboardInterrupt):
                self.record()
        self.assertIn(monitor.TARGET, ledger.sessions(self.root, self.rules))
        self.record()


class MonitorBindingRetryTests(unittest.TestCase):
    setUp = monitor_fixture.MonitorTests.setUp
    run_check = monitor_fixture.MonitorTests.run_check

    def context(self):
        source = self.root / 'discovery.json'
        source.write_bytes(monitor.canonical(dict(session=monitor.SESSION, pdf_requests=0,
            schema_version='jpx-json-discovery-v1', monthly_json_reference_url=monitor_fixture.URL)))
        calendar = self.root / 'calendar.json'
        calendar.write_bytes(monitor.canonical({'jp_sessions':[monitor.SESSION,monitor.TARGET]}))
        self.policy.update(calendar='calendar.json',calendar_sha256=monitor.digest(calendar))
        policy = self.root / 'config/forward_trial_execution_policy.json'
        policy.write_bytes(monitor.canonical(self.policy))
        monitor.write_state(self.folder / 'context.json', dict(target_session=monitor.SESSION,
            deadline=monitor.DEADLINE.isoformat(), monthly_url=monitor_fixture.URL,
            discovery_reference='discovery.json', protected_references={
                'discovery.json':monitor.digest(source),
                'config/forward_trial_execution_policy.json':monitor.digest(policy)}))
        for relative in monitor.CODE_PATHS:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# synthetic code\n')
        monitor.write_state(self.folder / 'armed.json', dict(code_commit='a'*40,
            target_session=monitor.SESSION, deadline=monitor.DEADLINE.isoformat(),
            code_hashes={relative:monitor.digest(self.root / relative) for relative in monitor.CODE_PATHS}))
        def git(command, **kwargs):
            self.assertNotIn('rev-parse', command)  # Execution never reads repository HEAD.
            return monitor.BRANCH + '\n'
        return patch.object(monitor.subprocess, 'check_output', side_effect=git)

    def test_unrelated_commit_no_longer_stops_monitor(self):
        self.context()  # Prepare identities; use real local Git instead of its stub.
        def git(*args):
            return subprocess.check_output(['git','-C',str(self.root),*args],text=True)
        git('init','-q','-b',monitor.BRANCH)
        git('add','.')
        git('-c','user.name=Offline Fixture','-c','user.email=fixture@example.invalid',
            'commit','-qm','armed fixture')
        (self.root / 'README.md').write_text('unrelated commit\n')
        git('add','README.md')
        git('-c','user.name=Offline Fixture','-c','user.email=fixture@example.invalid',
            'commit','-qm','unrelated fixture commit')
        self.assertNotEqual(git('rev-parse','HEAD').strip(),'a'*40)
        result = monitor.check_once(self.root, now=monitor_fixture.NOW,
            fetch=lambda *_: monitor_fixture.response())
        self.assertEqual(result['result_state'], monitor.ABSENT)
        self.assertEqual(result['code_commit'], 'a'*40)

    def test_changed_protected_source_code_policy_calendar_discovery_close_gate(self):
        with self.context():
            paths = [self.root / relative for relative in monitor.CODE_PATHS]
            paths += [self.root/'config/forward_trial_execution_policy.json',
                      self.root/'calendar.json', self.root/'discovery.json']
            for path in paths:
                original = path.read_bytes()
                with self.subTest(path=path):
                    path.write_bytes(original + b' ')
                    fetch = Mock(side_effect=AssertionError('HTTP forbidden'))
                    result = monitor.check_once(self.root, now=monitor_fixture.NOW, fetch=fetch)
                    self.assertEqual(result['result_state'], monitor.STOP)
                    fetch.assert_not_called()
                    path.write_bytes(original)
                    (self.folder/'latest.json').unlink(missing_ok=True)
                    (self.folder/'STOP.json').unlink(missing_ok=True)

    def test_deadline_expiry_leaves_tracked_tree_clean(self):
        (self.root / '.gitignore').write_bytes((Path(__file__).resolve().parents[1]/'.gitignore').read_bytes())
        def git(*args):
            return subprocess.check_output(['git','-C',str(self.root),*args], text=True)
        git('init','-q');git('add','.')
        git('-c','user.name=Offline Fixture','-c','user.email=fixture@example.invalid',
            'commit','-qm','fixture')
        self.run_check(now=monitor.DEADLINE)
        self.assertEqual(git('status','--porcelain'), '')
        self.assertIn(monitor.TARGET, ledger.sessions(self.root, self.policy))
        self.assertEqual(self.calls, [])

    def test_deadline_failure_retry_is_bounded_across_restarts_and_zero_http(self):
        (self.root / 'config/forward_trial_execution_policy.json').write_text('{')
        results = [self.run_check(now=monitor.DEADLINE + timedelta(hours=i)) for i in range(6)]
        self.assertEqual([r['finalization_attempts'] for r in results], [1,2,3,3,3,3])
        self.assertTrue(results[2]['deadline_expired'])
        self.assertTrue(results[2]['finalization_exhausted'])
        self.assertTrue((self.folder/'STOP.json').exists())
        self.assertEqual(self.calls, [])
        self.assertEqual(monitor.read_state(self.folder/'deadline_retry.json')['attempts'], 3)

    def test_finalize_clock_rollback_cannot_reenter_http(self):
        fetch = Mock(side_effect=AssertionError('HTTP forbidden'))
        result = monitor.check_once(self.root, now=monitor.DEADLINE-timedelta(seconds=1),
                                    fetch=fetch, finalize_only=True)
        self.assertEqual(result['result_state'], monitor.STOP)
        self.assertTrue(result['deadline_expired'])
        fetch.assert_not_called()

    def test_restart_and_parallel_call_cannot_consume_retry_before_one_hour(self):
        (self.root / 'config/forward_trial_execution_policy.json').write_text('{')
        first = self.run_check(now=monitor.DEADLINE)
        for seconds in (1, 10, 3599):
            result = self.run_check(now=monitor.DEADLINE + timedelta(seconds=seconds))
            self.assertEqual(result['finalization_attempts'],1)
            self.assertFalse(result['deadline_expired'])
        result = self.run_check(now=monitor.DEADLINE + timedelta(hours=1))
        self.assertEqual(result['finalization_attempts'],2)
        self.assertEqual(self.calls,[])

    def test_completed_finalization_recovers_after_crash_before_latest_or_stop(self):
        for interrupted in ('latest.json','STOP.json'):
            with self.subTest(interrupted=interrupted), tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                (root/'config').mkdir()
                (root/'config/forward_trial_execution_policy.json').write_bytes(monitor.canonical(self.policy))
                folder=root/'state/jpx_publication_monitor'/monitor.SESSION
                monitor.write_state(folder/'STOP.json',dict(target_session=monitor.SESSION,
                    checked_at_jst=monitor_fixture.NOW.isoformat(),result_state=monitor.STOP,
                    deadline_expired=False,stop_reason='earlier STOP'))
                actual=monitor.write_state
                def write(path,value):
                    if Path(path).name == interrupted:
                        raise KeyboardInterrupt('after durable completion')
                    return actual(path,value)
                with patch.object(monitor,'write_state',side_effect=write):
                    with self.assertRaises(KeyboardInterrupt):
                        monitor.check_once(root,now=monitor.DEADLINE,fetch=Mock(side_effect=AssertionError('HTTP')))
                result=monitor.check_once(root,now=monitor.DEADLINE+timedelta(seconds=1),
                    fetch=Mock(side_effect=AssertionError('HTTP')))
                self.assertEqual(result['result_state'],monitor.EXPIRED)
                self.assertEqual(result['finalization_attempts'],1)
                self.assertEqual(monitor.read_state(folder/'STOP.json'),result)

    def test_state_and_retry_symlinks_cannot_reset_attempts_or_escape(self):
        self.folder.mkdir(parents=True)
        outside=self.root/'outside.json'
        outside.write_text('untrusted')
        path=self.folder/'deadline_retry.json'
        path.symlink_to(self.root/'missing')
        result=self.run_check(now=monitor.DEADLINE)
        self.assertTrue(result['finalization_exhausted'])
        self.assertEqual(self.calls,[])
        for name in ('state.json','.lock'):
            path=self.folder/name
            if path.exists():
                path.unlink()
            path.symlink_to(outside)
            with self.assertRaises((ValueError,OSError)):
                if name == '.lock':
                    with monitor.locked(self.folder):
                        pass
                else:
                    monitor.read_state(path)
            with self.assertRaises((ValueError,OSError)):
                monitor.write_state(path,{'bad':'replacement'})
            self.assertEqual(outside.read_text(),'untrusted')

    def test_corrupt_retry_ledger_is_terminal_without_network(self):
        self.folder.mkdir(parents=True)
        (self.folder/'deadline_retry.json').write_text('{')
        result = self.run_check(now=monitor.DEADLINE)
        self.assertTrue(result['finalization_exhausted'])
        self.assertEqual(result['result_state'], monitor.STOP)
        self.assertEqual(self.calls, [])

    def test_missing_retry_after_failed_reservation_cannot_reset_budget(self):
        (self.root/'config/forward_trial_execution_policy.json').write_text('{')
        first=self.run_check(now=monitor.DEADLINE)
        self.assertEqual(first['finalization_attempts'],1)
        (self.folder/'deadline_retry.json').unlink()
        result=self.run_check(now=monitor.DEADLINE+timedelta(hours=1))
        self.assertTrue(result['finalization_exhausted'])
        self.assertIn('LEDGER_MISSING',result['stop_reason'])
        self.assertEqual(self.calls,[])

    def test_crash_before_results_and_retry_ledger_loss_cannot_reset_budget(self):
        with patch.object(monitor,'record_not_run',side_effect=KeyboardInterrupt('before results')):
            with self.assertRaises(KeyboardInterrupt):
                self.run_check(now=monitor.DEADLINE)
        self.assertFalse((self.folder/'latest.json').exists())
        self.assertFalse((self.folder/'STOP.json').exists())
        (self.folder/'deadline_retry.json').unlink()
        result=self.run_check(now=monitor.DEADLINE+timedelta(seconds=1))
        self.assertTrue(result['finalization_exhausted'])
        self.assertIn('LEDGER_MISSING',result['stop_reason'])
        self.assertEqual(self.calls,[])

    def test_driver_and_generated_units_have_no_unbounded_restart(self):
        states = [dict(deadline_expired=False),dict(deadline_expired=False),dict(deadline_expired=True)]
        with patch.object(monitor, 'check_once', side_effect=states) as check:
            sleep = Mock()
            self.assertEqual(monitor.run_deadline_finalization(self.root, sleep=sleep), states[-1])
            self.assertEqual(check.call_count, 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertTrue(all(call.kwargs['finalize_only'] for call in check.call_args_list))
        definitions = installer.units(self.root, '/usr/bin/python3')
        service = definitions[monitor.UNIT+'-deadline.service']
        self.assertIn('Restart=no', service)
        self.assertNotIn('Restart=on-failure',service)
        self.assertIn('OnFailure='+monitor.UNIT+'-deadline-disarm.service',service)
        self.assertIn('--finalization-failed',definitions[monitor.UNIT+'-deadline-disarm.service'])
        self.assertIn(monitor.UNIT+'-deadline.timer', definitions[monitor.UNIT+'-deadline-disarm.service'])


class HistoricalDayEfficiencyTests(unittest.TestCase):
    def test_common_heavy_verification_once_per_day_and_no_cross_execution_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            day = '2026-10-09'
            started = datetime.fromisoformat('2026-10-14T08:20:00+09:00')
            bridge_path = root/'data/historical_close_bridge/jp'/day/'record.json'
            bridge_path.parent.mkdir(parents=True)
            parsed_path=root/'parsed.json'; parsed_path.write_text('synthetic')
            raw=root/'raw.pdf';raw.write_bytes(b'synthetic-not-a-real-pdf')
            manifest_path=root/'data/outcome_input/manual_jpx/retention_manifest.json'
            manifest_path.parent.mkdir(parents=True)
            entry={'session_date':day}
            manifest_path.write_bytes(core.canonical_bytes({'entries':[entry]}))
            policy_path=root/'policy.json';policy_path.write_text('{}')
            rows=[dict(ticker=ticker,session_date=day,close=100.0) for ticker in sorted(core.CORE)]
            record=dict(source_role='FORECAST_FEATURE',source_kind='HISTORICAL_CLOSE_BRIDGE',
                eligibility='NON_FORMAL',upstream_source_role='OUTCOME_ONLY',session_date=day,
                registered_at='2026-10-13T20:00:00+09:00',records=rows,
                policy_path=str(policy_path),policy_sha256=core.sha256(policy_path),
                parsed_path=str(parsed_path),parsed_sha256=core.sha256(parsed_path),
                raw_path=str(raw),raw_sha256=core.sha256(raw),
                manifest_entry_sha256=digest(entry),source_data_sha256=digest(rows))
            key=b'k'*32
            record['hmac_sha256']=hmac.new(key,core.canonical_bytes(record),hashlib.sha256).hexdigest()
            forward_storage.seal(bridge_path,record)
            parsed=dict(records=rows)
            provenance=dict(raw_sha256=core.sha256(raw),parsed_sha256=core.sha256(parsed_path),
                            manifest_sha256=core.sha256(manifest_path))
            with patch.object(core,'ROOT',root), \
                 patch.object(execution_policy,'load_policy',return_value={'historical_through':'2026-10-08'}), \
                 patch.object(inputs,'load_validated_history',return_value=({t:[dict(session_date='2026-10-08',close=99.0)] for t in core.CORE},{})), \
                 patch.object(historical,'_key',return_value=key) as authenticate, \
                 patch.object(historical,'_policy',return_value=({},policy_path,core.sha256(policy_path))) as policy, \
                 patch.object(forward_artifacts,'_validated_jpx',return_value=(parsed,provenance)) as validate, \
                 patch.object(core,'_resolve_jpx_raw_source',return_value=raw), \
                 patch.object(core,'read_frozen',wraps=core.read_frozen) as load:
                calendar={'jp_sessions':['2026-10-08',day,'2026-10-13']}
                for execution in range(2):
                    stocks, hashes=inputs.load_execution_history(calendar,{},started,'2026-10-13')
                    self.assertTrue(all(stocks[ticker]==[dict(session_date='2026-10-08',close=99.0),dict(session_date=day,close=100.0)] for ticker in core.CORE))
                    self.assertEqual(validate.call_count, execution+1)
                    self.assertEqual(authenticate.call_count, execution+1)
                    self.assertEqual(policy.call_count, execution+1)
                    self.assertEqual(load.call_count, execution+1)
                    self.assertEqual(hashes[str(bridge_path)],core.sha256(bridge_path))
                raw.write_bytes(b'changed after validation')
                with self.assertRaisesRegex(ValueError,'INPUT_CHANGED_BEFORE_FREEZE'):
                    forward_artifacts._unchanged(hashes)

    def test_production_has_no_probe_imports_and_semantics_files_unchanged(self):
        root=Path(__file__).resolve().parents[1]
        for parent in ('engine','providers','scripts'):
            for path in (root/parent).rglob('*.py'):
                if path.name == 'one_session_dry_run.py':
                    continue
                self.assertNotIn('import one_session_dry_run',path.read_text(),str(path))
        # Compare immutable, current semantics against this Task's explicit base.
        if (root/'.git').exists():
            for relative in ('models/linear_pool/__init__.py','config/threshold_spec.json',
                             'config/baseline_spec.json','config/primary_forward_trial_v1.json',
                             'providers/jp/jpx_manual/parser.py'):
                body=binding.git(root,'show','f60fbba965ee858ca6e7a3afefbdb4e09655a4f0:'+relative)
                self.assertEqual(body,(root/relative).read_bytes(),relative)


class StorageFinalGuardTests(unittest.TestCase):
    def test_actual_probe_confirmation_entries_are_synced_before_publication(self):
        # Execute only the actual nested confirmation callback, without a Probe
        # Forecast, market inputs, or any production artifact access.
        project = Path(__file__).resolve().parents[1]
        tree = ast.parse((project / 'engine/one_session_dry_run.py').read_bytes())
        phase = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name == 'forecast_phase')
        confirm = next(node for node in ast.walk(phase) if isinstance(node, ast.FunctionDef)
                       and node.name == 'confirm')
        at = monitor.DEADLINE - timedelta(minutes=1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / 'bundle'
            synced = []
            real_sync = forward_storage.sync_dir
            def sync(path):
                if Path(path).name.startswith('.pending-'):
                    synced.append({entry.name for entry in Path(path).iterdir()})
                real_sync(path)
            namespace = dict(_now=lambda: at, JST=monitor.JST,
                             frozen=at - timedelta(minutes=1), cutoff=monitor.DEADLINE,
                             forecast={'forecast_id': 'synthetic'}, core=core,
                             _seal=forward_storage.seal, _sync_dir=sync)
            exec(compile(ast.Module(body=[confirm], type_ignores=[]), '<actual-probe-confirm>', 'exec'), namespace)
            with patch.object(forward_storage, 'sync_dir', side_effect=sync):
                forward_storage.commit(destination,
                    lambda stage: (stage / 'synthetic.json').write_bytes(b'{}'), namespace['confirm'])
            self.assertEqual(len(synced), 2)
            self.assertNotIn('freeze_confirmation.json', synced[0])
            self.assertTrue({'freeze_confirmation.json', 'freeze_confirmation.sha256'} <= synced[1])
            self.assertEqual(core.read_frozen(destination / 'freeze_confirmation.json'),
                             {'confirmed_at': at.isoformat(),
                              'forecast_sha256': core.sha256(destination / 'synthetic.json')})

    def test_staging_durability_precedes_final_cutoff_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            elapsed=[]
            def sync(path):
                elapsed.append('fsync')
            def confirm(stage):
                self.assertEqual(elapsed,['fsync'])
                raise ValueError('CUTOFF_PASSED_DURING_FSYNC')
            with patch.object(forward_storage,'sync_dir',side_effect=sync):
                with self.assertRaisesRegex(ValueError,'CUTOFF_PASSED'):
                    forward_storage.commit(root/'bundle',lambda stage:(stage/'test').write_text('test'),confirm)
            self.assertFalse((root/'bundle').exists())

    def test_dirty_runtime_ledger_code_is_execution_code(self):
        with patch.object(forward_storage.subprocess,'run',return_value=Mock(returncode=1)) as git:
            with self.assertRaisesRegex(ValueError,'DIRTY_EXECUTION_CODE'):
                forward_storage.code_commit()
            self.assertIn('scripts/forecast_not_run_ledger.py',git.call_args.args[0])


if __name__ == '__main__':
    unittest.main()

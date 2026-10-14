"""Offline synthetic official-history admission; no execution or live artifacts."""
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

from engine import forward_history as history
from engine import forward_storage as storage
from engine import forward_trial_inputs as inputs
from engine import historical_close_bridge as bridge
from engine import minimal_trial as core
from engine import forward_artifacts as artifacts
from engine import execution_policy as policy
from tests import test_fresh_ephemeral as fixture
NOW = fixture.NOW


class ForwardHistoryTests(unittest.TestCase):
    write = fixture.EphemeralFreshTests.write

    def setUp(self):
        fixture.EphemeralFreshTests.setUp(self)
        self.addCleanup(patch.stopall)
        patch.object(socket.socket, 'connect', side_effect=AssertionError('NO_LIVE_HTTP')).start()
        patch.object(history, '_now', return_value=NOW).start()
        patch.object(inputs, 'prepare_execution_inputs', side_effect=AssertionError('NO_FRESH_OR_PIT')).start()
        self.calendar['validation_range'] = dict(start='2026-09-14', end='2026-10-14')
        self.write(self.root / 'analysis/calendar_validation_2026-09-12.json', self.calendar)
        rules_path = self.root / 'config/forward_trial_execution_policy.json'
        rules = json.loads(rules_path.read_bytes())
        new_anchor = self.root / 'data/forward_trial_history/2026-10-09/history.json'
        self.registered.parent.rename(new_anchor.parent)
        self.registered = new_anchor
        rules['historical_snapshot'] = 'data/forward_trial_history/2026-10-09/history.json'
        rules['calendar_sha256'] = core.sha256(self.root / rules['calendar'])
        self.write(rules_path, rules)
        self.manifest_path = history._manifest_path()
        self.manifest_path.parent.mkdir(parents=True)
        self.entries = []
        self.parsed = {}
        # Existing verifier is exercised with HMAC-signed bridge records and
        # entry identities. Only PDF validation is mocked with synthetic OHLC.
        patch.object(core, '_resolve_jpx_raw_source', side_effect=lambda p: Path(p['source_file'])).start()
        patch.object(artifacts, '_validated_jpx', side_effect=self.validated_jpx).start()
        for day in self.calendar['jp_sessions']:
            if '2026-09-11' < day <= '2026-10-09':
                self.add_day(day)
        self.save_manifest()
        record = core.read_frozen(self.registered)
        for day in self.parsed:
            path = bridge._root() / day / 'record.json'
            record['source_hashes'][str(path)] = core.sha256(path)
            record['source_hashes'][str(path.with_suffix('.sha256'))] = core.sha256(path.with_suffix('.sha256'))
        record['source_hashes'][str(self.manifest_path)] = core.sha256(self.manifest_path)
        self.reseal_anchor(record)
        self.old = self.registered.read_bytes()
        self.old_sidecar = self.registered.with_suffix('.sha256').read_bytes()
        self.initial_ledger = {str(p): p.read_bytes() for p in (self.root / 'state').rglob('*') if p.is_file()}

    def reseal_anchor(self, record):
        record.pop('hmac_sha256', None)
        record = history._signed(record)
        self.write(self.registered, record)
        self.registered.with_suffix('.sha256').write_text(core.sha256(self.registered) + '\n')

    def save_manifest(self):
        self.write(self.manifest_path, dict(entries=self.entries))

    def add_day(self, day):
        raw = self.root / 'data/outcome_input/manual_jpx' / (day + '.pdf')
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(('SYNTHETIC_OFFICIAL:' + day).encode())
        path = self.root / 'data/outcome_parsed' / (day + '-official.json')
        closes = [r['close'] for r in self.history[core.CORE[0]] if r['session_date'] == day]
        close = closes[0] if closes else 123.5
        records = [dict(ticker=t, session_date=day, close=close) for t in sorted(core.CORE)]
        parsed = dict(session_date=day, source_file=str(raw), records=records, acquisition_method='HUMAN_MANUAL_DOWNLOAD')
        self.write(path, parsed)
        self.parsed[day] = parsed
        entry = dict(session_date=day, sha256=core.sha256(raw), parser_status='PASS',
                     normalized_output_path=str(path), normalized_output_sha256=core.sha256(path),
                     acquisition_origin='HUMAN_MANUAL_DOWNLOAD')
        self.entries.append(entry)
        folder = bridge._root() / day
        folder.mkdir(parents=True, exist_ok=True)
        policy_path = self.root / 'config/official_gap_policy.json'
        record = dict(schema_version='historical-close-bridge-v1', source_role='FORECAST_FEATURE',
            source_kind='HISTORICAL_CLOSE_BRIDGE', eligibility='NON_FORMAL', upstream_source_role='OUTCOME_ONLY',
            session_date=day, records=records, registered_at='2026-10-14T08:00:00+09:00',
            source_data_sha256=history._digest(records), raw_path=str(raw), raw_sha256=core.sha256(raw),
            parsed_path=str(path), parsed_sha256=core.sha256(path), policy_path=str(policy_path),
            policy_sha256=core.sha256(policy_path), manifest_entry_sha256=history._digest(entry))
        artifacts._seal(folder / 'record.json', history._signed(record))

    def validated_jpx(self, path, day, now):
        entry = next(e for e in json.loads(self.manifest_path.read_bytes())['entries'] if e['session_date'] == day)
        parsed = json.loads(path.read_bytes())
        if (parsed != self.parsed[day] or core.sha256(path) != entry['normalized_output_sha256']
                or core.sha256(parsed['source_file']) != entry['sha256']):
            raise ValueError('SYNTHETIC_OFFICIAL_VALIDATION_FAILED')
        return parsed, dict(raw_sha256=entry['sha256'], parsed_sha256=entry['normalized_output_sha256'],
                            manifest_sha256=core.sha256(self.manifest_path))

    def refresh(self, end='2026-10-09'):
        return history.refresh_forward_history_manifest(end)

    def register(self, ref=None, **kwargs):
        return history.register_forward_snapshot(self.refresh() if ref is None else ref, **kwargs)

    def load(self):
        return inputs.load_validated_history(self.calendar, self.thresholds, NOW)

    def inventory(self):
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in history._store().iterdir() if p.is_file()}

    def test_unchanged_history_same_canonical_sha_no_artifact_noise(self):
        first = self.refresh()
        before = self.inventory()
        self.assertEqual(first, self.refresh())
        self.assertEqual(before, self.inventory())
        record = json.loads(Path(first['path']).read_bytes())
        self.assertEqual(record['sha256'], history._digest(record['record']))
        self.assertNotIn('registered_at', record['record'])

    def test_valid_history_advance_new_sha(self):
        first = self.refresh()
        self.add_day('2026-10-13')
        self.save_manifest()
        second = self.refresh('2026-10-13')
        self.assertNotEqual(first['sha256'], second['sha256'])
        self.assertEqual(first['sha256'], json.loads(Path(first['path']).read_bytes())['sha256'])

    def test_snapshot_immutable(self):
        self.register()
        self.assertEqual(self.registered.read_bytes(), self.old)
        self.assertEqual(self.registered.with_suffix('.sha256').read_bytes(), self.old_sidecar)

    def test_registration_has_required_authenticated_lineage(self):
        ref = self.refresh()
        registered = self.register(ref)
        envelope = json.loads(Path(registered['path']).read_bytes())
        record = envelope['record']
        history._authenticated(record)
        self.assertEqual(record['previous_snapshot_identity'], dict(path=str(self.registered), sha256=core.sha256(self.registered)))
        self.assertEqual(record['manifest_sha256'], ref['sha256'])
        self.assertEqual(record['reason'], 'OFFICIAL_HISTORY_ADVANCED')
        for field in ('history_end', 'calendar_identity', 'policy_identity', 'registered_at', 'code_commit'):
            self.assertIn(field, record)

    def test_duplicate_identical_registration_idempotent(self):
        ref = self.refresh()
        old = dict(path=str(self.registered), sha256=core.sha256(self.registered))
        first = self.register(ref, previous_snapshot=old)
        before = self.inventory()
        self.assertEqual(first, self.register(ref, previous_snapshot=old))
        self.assertEqual(before, self.inventory())

    def test_conflicting_registration_rejected(self):
        ref = self.refresh()
        first = self.register(ref)
        before = self.inventory()
        with self.assertRaisesRegex(ValueError, 'CONFLICT'):
            self.register(ref, previous_snapshot=dict(path=first['path'], sha256='f' * 64))
        self.assertEqual(before, self.inventory())

    def test_stale_snapshot_and_refresh_without_registration_stay_blocked(self):
        self.add_day('2026-10-13')
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'INPUT_CHANGED_BEFORE_FREEZE'):
            self.load()
        self.refresh('2026-10-13')
        with self.assertRaisesRegex(ValueError, 'INPUT_CHANGED_BEFORE_FREEZE'):
            self.load()

    def test_valid_refresh_reregister_clears_legitimate_staleness(self):
        first = self.register()
        self.add_day('2026-10-13')
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'INPUT_CHANGED_BEFORE_FREEZE'):
            self.load()
        ref = self.refresh('2026-10-13')
        second = self.register(ref, previous_snapshot=first)
        self.assertNotEqual(first, second)
        stocks, hashes = self.load()
        self.assertEqual(stocks[core.CORE[0]][-1], dict(session_date='2026-10-13', close=123.5))
        artifacts._unchanged(hashes)
        self.assertEqual(self.registered.read_bytes(), self.old)

    def test_forged_retention_manifest_blocked(self):
        self.register()
        self.entries[0]['sha256'] = 'a' * 64
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'INPUT_CHANGED_BEFORE_FREEZE'):
            self.load()
        with self.assertRaisesRegex(ValueError, 'INPUT_CHANGED_BEFORE_FREEZE'):
            self.refresh()

    def test_manifest_entry_metadata_substitution_blocked_by_bridge(self):
        self.entries[0]['acquisition_origin'] = 'FORGED'
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'INPUT_CHANGED_BEFORE_FREEZE'):
            self.refresh()

    def test_forged_origin_outside_close_horizon_rejected(self):
        self.add_day('2026-10-13')
        self.entries[-1]['acquisition_origin'] = 'FORGED'
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'INPUT_CHANGED_BEFORE_FREEZE'):
            self.refresh()

    def test_direct_origin_cannot_be_downgraded_to_skip_discovery(self):
        day = '2026-10-13'
        self.add_day(day)
        self.parsed[day]['acquisition_method'] = 'OFFICIAL_LISTING_DIRECT_HTTPS'
        path = Path(self.entries[-1]['normalized_output_path'])
        self.write(path, self.parsed[day])
        self.entries[-1]['normalized_output_sha256'] = core.sha256(path)
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'INPUT_CHANGED_BEFORE_FREEZE'):
            self.refresh()

    def test_rehashed_forward_manifest_substitution_blocked(self):
        ref = self.refresh()
        self.register(ref)
        path = Path(ref['path'])
        envelope = json.loads(path.read_bytes())
        envelope['record']['stocks'][core.CORE[0]][-1]['close'] = 999
        envelope['sha256'] = history._digest(envelope['record'])
        self.write(path, envelope)
        with self.assertRaisesRegex(ValueError, 'INPUT_CHANGED_BEFORE_FREEZE'):
            self.load()

    def test_policy_changed_registration_blocked(self):
        ref = self.refresh()
        self.register(ref)
        path = self.root / 'config/forward_trial_execution_policy.json'
        record = json.loads(path.read_bytes())
        record['scope'] = 'FORGED'
        self.write(path, record)
        with self.assertRaises(ValueError):
            self.load()

    def test_not_run_and_execution_calls_unchanged(self):
        before = policy.not_run_sessions()
        self.register()
        self.assertEqual(before, policy.not_run_sessions())
        self.assertEqual(self.initial_ledger, {str(p): p.read_bytes() for p in (self.root / 'state').rglob('*') if p.is_file()})
        inputs.prepare_execution_inputs.assert_not_called()
        for fetch in self.fetches.values():
            fetch.assert_not_called()

    def test_snapshot_conflict_on_history_rollback(self):
        self.add_day('2026-10-13')
        self.save_manifest()
        self.register(self.refresh('2026-10-13'))
        with self.assertRaisesRegex(ValueError, 'CONFLICT'):
            self.register(self.refresh())

    def test_refreshed_manifest_changed_before_registration_blocked(self):
        ref = self.refresh()
        self.add_day('2026-10-13')
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'INPUT_CHANGED_BEFORE_FREEZE'):
            self.register(ref)

    def test_symlink_snapshot_store_and_manifest_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        store = history._store()
        store.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.refresh()
        store.unlink()
        source = self.root / 'manifest-copy.json'
        source.write_bytes(self.manifest_path.read_bytes())
        self.manifest_path.unlink()
        self.manifest_path.symlink_to(source)
        with self.assertRaises(OSError):
            self.refresh()

    def test_retention_swap_after_validation_is_not_blessed(self):
        self.add_day('2026-10-13')
        self.save_manifest()
        original = history._bytes
        swapped = False
        def swap(path):
            nonlocal swapped
            body = original(path)
            if Path(path) == self.manifest_path and not swapped:
                swapped = True
                self.entries[-1]['acquisition_origin'] = 'FORGED'
                self.save_manifest()
            return body
        with patch.object(history, '_bytes', side_effect=swap):
            with self.assertRaisesRegex(ValueError, 'INPUT_CHANGED_BEFORE_FREEZE'):
                self.refresh()

    def test_anchor_swap_after_authentication_is_not_blessed(self):
        original = history._frozen
        def swap(path, **kwargs):
            record = original(path, **kwargs)
            if Path(path) == self.registered:
                forged = json.loads(self.registered.read_bytes())
                forged['stocks'][core.CORE[0]][-1]['close'] = 999
                self.write(self.registered, forged)
                self.registered.with_suffix('.sha256').write_text(core.sha256(self.registered) + '\n')
            return record
        with patch.object(history, '_frozen', side_effect=swap):
            with self.assertRaisesRegex(ValueError, 'INPUT_CHANGED_BEFORE_FREEZE'):
                self.refresh()

    def test_registered_manifest_swap_during_load_is_not_blessed(self):
        ref = self.refresh()
        self.register(ref)
        original = history._envelope
        def swap(fd, name, **kwargs):
            value = original(fd, name, **kwargs)
            if name == Path(ref['path']).name:
                body = json.loads(Path(ref['path']).read_bytes())
                body['record']['stocks'][core.CORE[0]][-1]['close'] = 999
                body['sha256'] = history._digest(body['record'])
                self.write(Path(ref['path']), body)
            return value
        with patch.object(history, '_envelope', side_effect=swap):
            with self.assertRaisesRegex(ValueError, 'INPUT_CHANGED_BEFORE_FREEZE'):
                self.load()

    def test_initial_pointer_write_failure_recovers_same_immutable_registration(self):
        ref = self.refresh()
        write = storage.write_envelope_at
        def fail_pointer(fd, name, record, **kwargs):
            if name == 'current.json':
                raise OSError('POINTER_WRITE_FAILED')
            return write(fd, name, record, **kwargs)
        with patch.object(storage, 'write_envelope_at', side_effect=fail_pointer):
            with self.assertRaisesRegex(OSError, 'POINTER_WRITE_FAILED'):
                self.register(ref)
        snapshots = list(history._store().glob('snapshot-*.json'))
        self.assertEqual(len(snapshots), 1)
        before = snapshots[0].read_bytes()
        recovered = self.register(ref)
        self.assertEqual(recovered['path'], str(snapshots[0]))
        self.assertEqual(snapshots[0].read_bytes(), before)
        self.assertEqual(len(list(history._store().glob('snapshot-*.json'))), 1)
        self.assertFalse((history._store() / '.registration-intent.json').exists())
        self.load()

    def test_every_registration_fsync_failure_recovers_initial_and_advanced(self):
        import shutil
        original_entries = copy.deepcopy(self.entries)
        sync = os.fsync
        for advanced in (False, True):
            for fail_at in range(1, 8):
                with self.subTest(advanced=advanced, fail_at=fail_at):
                    if history._store().exists():
                        shutil.rmtree(history._store())  # Synthetic temporary store only.
                    self.entries = copy.deepcopy(original_entries)
                    self.save_manifest()
                    ref = self.refresh()
                    if advanced:
                        self.register(ref)
                        folder = bridge._root() / '2026-10-13'
                        if folder.exists():
                            shutil.rmtree(folder)
                        self.add_day('2026-10-13')
                        self.save_manifest()
                        ref = self.refresh('2026-10-13')
                    count = 0
                    def fail(fd):
                        nonlocal count
                        count += 1
                        if count == fail_at:
                            raise OSError('INJECTED_FSYNC_FAILURE')
                        sync(fd)
                    with patch.object(os, 'fsync', side_effect=fail):
                        with self.assertRaisesRegex(OSError, 'INJECTED_FSYNC_FAILURE'):
                            self.register(ref)
                    self.register(ref)
                    self.load()
                    self.assertEqual(len(list(history._store().glob('snapshot-*.json'))), 2 if advanced else 1)

    def test_concurrent_identical_registration_creates_one_snapshot(self):
        from concurrent.futures import ThreadPoolExecutor
        ref = self.refresh()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.register(ref), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(list(history._store().glob('snapshot-*.json'))), 1)

    def test_missing_current_pointer_fails_closed_without_new_registration(self):
        ref = self.refresh()
        self.register(ref)
        (history._store() / 'current.json').unlink()
        with self.assertRaisesRegex(ValueError, 'CURRENT_MISSING'):
            self.load()
        with self.assertRaisesRegex(ValueError, 'CURRENT_MISSING'):
            self.register(ref)
        self.assertEqual(len(list(history._store().glob('snapshot-*.json'))), 1)

    def test_official_evidence_advance_with_same_close_horizon_can_reregister(self):
        first = self.register()
        self.add_day('2026-10-13')
        self.save_manifest()
        second = self.register(self.refresh(), previous_snapshot=first)
        self.assertNotEqual(first, second)
        self.assertEqual(self.load()[0], self.history)

    def test_manifest_reference_path_traversal_rejected(self):
        ref = self.refresh()
        ref['path'] = str(history._store() / '..' / Path(ref['path']).name)
        with self.assertRaisesRegex(ValueError, 'REGISTRATION_INVALID'):
            self.register(ref)


class DurableEnvelopeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'store'

    def test_file_and_directory_fsync_and_hash_readback(self):
        original = os.fsync
        calls = []
        def sync(fd):
            calls.append(os.fstat(fd).st_mode)
            original(fd)
        import stat
        with storage.secure_directory(self.path, create=True, lock=True) as fd, patch.object(os, 'fsync', side_effect=sync):
            sha = storage.write_envelope_at(fd, 'manifest.json', {'history': 1})
            record, actual = history._envelope(fd, 'manifest.json')
        self.assertEqual(sha, actual)
        self.assertTrue(any(stat.S_ISREG(mode) for mode in calls))
        self.assertTrue(any(stat.S_ISDIR(mode) for mode in calls))

    def test_failed_file_fsync_publishes_no_partial_manifest(self):
        with storage.secure_directory(self.path, create=True, lock=True) as fd:
            with patch.object(os, 'fsync', side_effect=OSError('FSYNC_FAILED')):
                with self.assertRaisesRegex(OSError, 'FSYNC_FAILED'):
                    storage.write_envelope_at(fd, 'manifest.json', {'history': 1})
        self.assertEqual([p.name for p in self.path.iterdir()], ['.lock'])

    def test_failed_confirmation_preserves_previous_pointer(self):
        with storage.secure_directory(self.path, create=True, lock=True) as fd:
            storage.write_envelope_at(fd, 'current.json', {'history': 1})
            with self.assertRaisesRegex(ValueError, 'INPUT_CHANGED_BEFORE_FREEZE'):
                storage.write_envelope_at(fd, 'current.json', {'history': 2}, replace=True,
                    confirm=Mock(side_effect=ValueError('INPUT_CHANGED_BEFORE_FREEZE')))
            self.assertEqual(history._envelope(fd, 'current.json')[0], {'history': 1})

    def test_directory_fsync_failure_is_not_success(self):
        import stat
        original = os.fsync
        def fail_dir(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError('DIRECTORY_FSYNC_FAILED')
            original(fd)
        with storage.secure_directory(self.path, create=True, lock=True) as fd:
            with patch.object(os, 'fsync', side_effect=fail_dir):
                with self.assertRaisesRegex(OSError, 'DIRECTORY_FSYNC_FAILED'):
                    storage.write_envelope_at(fd, 'manifest.json', {'history': 1})
            self.assertEqual(history._envelope(fd, 'manifest.json')[0], {'history': 1})

    def test_conflicting_create_only_and_symlink_destination_fail_closed(self):
        with storage.secure_directory(self.path, create=True, lock=True) as fd:
            storage.write_envelope_at(fd, 'manifest.json', {'history': 1})
            with self.assertRaisesRegex(ValueError, 'IMMUTABLE_CONFLICT'):
                storage.write_envelope_at(fd, 'manifest.json', {'history': 2})
            (self.path / 'bad.json').symlink_to(self.path / 'manifest.json')
            with self.assertRaises(OSError):
                storage.write_envelope_at(fd, 'bad.json', {'history': 2}, replace=True)

"""Offline renewal preserves STOP evidence and component-scoped authorization."""
from datetime import timedelta
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from providers.jp.jpx_manual import acquisition_binding as binding
from scripts import install_jpx_publication_monitor as installer
from scripts import monitor_jpx_publication as m
from tests.test_monitor_final_window import RetainedDiscoveryTests


class RebindTests(unittest.TestCase):
    def setUp(self):
        RetainedDiscoveryTests.setUp(self)
        paths = (*binding.FILES, *binding.NODES, binding.BINDER, binding.COMPATIBILITY,
                 'engine/forward_artifacts.py', 'engine/forward_storage.py', *m.CODE_PATHS)
        for relative in paths:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists() or relative in m.CODE_PATHS:
                path.write_bytes((m.ROOT / relative).read_bytes())
        self.git('init', '-b', m.BRANCH)
        self.git('add', '.')
        self.git('commit', '-m', 'synthetic original binding')
        self.old_commit = self.git('rev-parse', 'HEAD').strip()
        installer.create_context(self.root, self.reference)
        m.write_state(self.folder / 'armed.json', dict(
            target_session=m.SESSION, deadline=m.DEADLINE.isoformat(), code_commit=self.old_commit,
            code_hashes={p: m.digest(self.root / p) for p in m.CODE_PATHS}))
        self.now = m.DEADLINE - timedelta(hours=3)
        stop = dict(result_state=m.STOP, target_session=m.SESSION, deadline_expired=False,
                    checked_at_jst=(self.now - timedelta(hours=1)).isoformat(), http_status=None,
                    stop_reason='MONITOR_PROTECTED_CONTEXT_CHANGED:config/jpx_daily_acquisition_config.json')
        for name in ('STOP.json', 'latest.json'):
            m.write_state(self.folder / name, stop)
        self.original = {name: (self.folder / name).read_bytes()
                         for name in ('context.json', 'armed.json', 'STOP.json', 'latest.json')}
        self.config = self.root / 'config/jpx_daily_acquisition_config.json'
        self.config.write_bytes(self.config.read_bytes() + b'\n')
        self.git('add', 'config/jpx_daily_acquisition_config.json')
        self.git('commit', '-m', 'synthetic approved acquisition policy change')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), '-c', 'user.name=Fixture',
                                       '-c', 'user.email=fixture@example.test', *args], text=True,
                                      stderr=subprocess.DEVNULL)

    def renew(self):
        with patch.object(m.http.client, 'HTTPSConnection', side_effect=AssertionError('HTTP forbidden')):
            return installer.rebind(self.root, now=self.now)

    def test_stale_rejected_and_current_binding_accepted_with_exact_old_evidence(self):
        with self.assertRaisesRegex(ValueError, 'PROTECTED_CONTEXT_CHANGED'):
            m.verify_context(self.root, self.folder)
        result = self.renew()
        self.assertEqual(result['http_requests'], 0)
        self.assertFalse((self.folder / 'STOP.json').exists())
        self.assertEqual(m.verify_context(self.root, self.folder)[1], self.git('rev-parse', 'HEAD').strip())
        archive = self.folder / 'bindings' / result['binding_identity']
        for name, body in self.original.items():
            self.assertEqual((archive / name).read_bytes(), body)
        self.assertEqual(m.read_state(self.folder / 'latest.json')['result_state'],
                         'JPX_PUBLICATION_MONITOR_BOUND_OFFLINE')

    def test_unrelated_commit_and_runtime_state_do_not_invalidate(self):
        self.renew()
        (self.root / 'README.md').write_text('unrelated')
        self.git('add', 'README.md'); self.git('commit', '-m', 'unrelated')
        (self.root / 'state/unrelated.json').write_text('{}')
        m.verify_context(self.root, self.folder)

    def test_relevant_config_and_acquisition_code_changes_invalidate(self):
        self.renew()
        for path in (self.config, self.root / 'providers/jp/jpx_manual/historical_direct.py'):
            original = path.read_bytes()
            with self.subTest(path=path):
                path.write_bytes(original + b'\n# changed\n')
                with self.assertRaisesRegex(ValueError, 'PROTECTED_CONTEXT_CHANGED|ACQUISITION_COMPONENT_CHANGED'):
                    m.verify_context(self.root, self.folder)
                path.write_bytes(original)

    def test_old_binding_cannot_be_replayed(self):
        self.renew()
        (self.folder / 'armed.json').write_bytes(self.original['armed.json'])
        with self.assertRaisesRegex(ValueError, 'CONTEXT_BINDING_CHANGED'):
            m.verify_context(self.root, self.folder)

    def test_guessed_url_cannot_replace_official_discovery(self):
        self.renew()
        context = m.read_state(self.folder / 'context.json')
        context['monthly_url'] = 'https://www.jpx.co.jp/guessed.json'
        m.write_state(self.folder / 'context.json', context)
        armed = m.read_state(self.folder / 'armed.json')
        armed['context_sha256'] = m.hashlib.sha256(m.canonical(context)).hexdigest()
        m.write_state(self.folder / 'armed.json', armed)
        with self.assertRaisesRegex(ValueError, 'DISCOVERY_IDENTITY_FAILURE'):
            m.verify_context(self.root, self.folder)

    def test_security_stop_is_not_released(self):
        for reason in ('SECURITY_CHALLENGE', 'HTTP_403', 'UNEXPECTED_REDIRECT',
                       'INTERRUPTED_CHECK_REVIEW_REQUIRED'):
            with self.subTest(reason=reason):
                stop = m.read_state(self.folder / 'STOP.json'); stop['stop_reason'] = reason
                for name in ('STOP.json', 'latest.json'):
                    m.write_state(self.folder / name, stop)
                with self.assertRaisesRegex(ValueError, 'SECURITY_STOP_FORBIDDEN'):
                    self.renew()
                self.assertTrue((self.folder / 'STOP.json').exists())

    def test_changed_security_evidence_is_not_rebound(self):
        (self.root / 'data/jpx_historical_direct/current_stops_v2.json').write_text('{"changed":true}')
        with self.assertRaisesRegex(ValueError, 'PROTECTED_EVIDENCE_CHANGED'):
            self.renew()
        self.assertTrue((self.folder / 'STOP.json').exists())

    def test_final_reservation_and_closed_window_cannot_be_reset(self):
        m.write_state(self.folder / 'final_check_reserved.json', {})
        with self.assertRaisesRegex(ValueError, 'RESERVED_OR_COMPLETED'):
            self.renew()
        with self.assertRaisesRegex(ValueError, 'WINDOW_CLOSED'):
            installer.rebind(self.root, now=m.DEADLINE - m.FINAL_CHECK_LEAD)

    def test_offline_installer_arms_timers_without_starting_services(self):
        self.renew()
        self.git('add', '.'); self.git('commit', '-m', 'synthetic state only')
        destination = self.root / '.config/systemd/user'; destination.mkdir(parents=True)
        import sys
        for name, content in installer.units(self.root, sys.executable, pinned=True).items():
            (destination / name).write_text(content)
        with patch.object(m, 'ROOT', self.root), patch.object(m, 'configure_window'), \
             patch.object(installer.Path, 'home', return_value=self.root), \
             patch.object(installer, 'rebind', return_value={'http_requests':0}), \
             patch.object(installer.subprocess, 'check_output', return_value=''), \
             patch.object(installer.subprocess, 'run') as run, patch('builtins.print'):
            installer.install_rebind(m.TARGET)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertFalse(any('start' in command for command in commands))
        self.assertTrue(any('daemon-reload' in command for command in commands))
        enable = next(command for command in commands if 'enable' in command)
        self.assertEqual(set(enable[-3:]), {m.UNIT + suffix for suffix in
                                           ('.timer', '-final.timer', '-deadline.timer')})


if __name__ == '__main__':
    unittest.main()

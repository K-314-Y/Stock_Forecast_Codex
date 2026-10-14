"""Real sealed renewal fixtures; no systemd, provider, or production writes."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from engine import minimal_trial as core
from providers.jp.jpx_manual import acquisition_binding as binding
from providers.jp.jpx_manual import historical_policy_v2 as policy
from scripts import install_jpx_publication_monitor as installer
from scripts import monitor_jpx_publication as m
from tests import test_legacy_release_renewal as fixtures


class ReleaseTransitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        f = cls.fixture = fixtures.RenewalTests('test_b1_generation1_unchanged_gate_passes')
        f.setUp()
        cls.root = f.root; cls.store = f.store
        cls.folder = f.root/'state/jpx_publication_monitor'/m.SESSION
        (f.root/'.gitignore').write_text('data/\nanalysis/\nstate/\n')
        f.git('branch','-m',m.BRANCH)
        project = Path(__file__).resolve().parents[1]
        for relative in (*m.CODE_PATHS, 'config/forward_trial_execution_policy.json',
                         'config/primary_forward_trial_v1.json',
                         'analysis/calendar_validation_2026-09-12.json'):
            dest = f.root/relative; dest.parent.mkdir(parents=True,exist_ok=True)
            dest.write_bytes((project/relative).read_bytes())
        f.git('add','.'); f.commit('synthetic monitor baseline')
        for _ in range(3): f.change_code(); f.renew()
        cls.previous = policy.Stops(f.store).index['reviewed_legacy_releases'][-1]['receipt_reference']
        # Repackage already synthetic retained discovery into the monitor's schema.
        directory=f.root/'data/retained'; directory.mkdir()
        old=core.read_frozen(f.evidence['path']); chains={}
        for name,ref in f.inputs.items():
            body=Path(ref['path']).read_bytes(); (directory/(name+'.body')).write_bytes(body)
            row=dict(old['redirect_chains'][name][0]); row['response_sha256']=row.pop('sha256')
            row['response_url']=row['url']; chains[name]=[row]
            policy.seal(directory/(name+'.response.json'),dict(url=row['url'],chain=[row],sha256=ref['sha256']))
        policy.seal(directory/'discovery.json',dict(old,redirect_chains=chains))
        context=installer.build_context(f.root,'data/retained/discovery.json')
        context['acquisition_fingerprint']=binding.fingerprint(binding.components(f.root,infrastructure=True))
        cls.folder.mkdir(parents=True)
        m.write_state(cls.folder/'context.json',context)
        cls.now=m.DEADLINE-timedelta(hours=4)
        m.write_state(cls.folder/'armed.json',dict(target_session=m.SESSION,
            deadline=m.DEADLINE.isoformat(),code_commit=f.git('rev-parse','HEAD').strip(),
            context_sha256=m.hashlib.sha256(m.canonical(context)).hexdigest(),
            code_hashes={p:m.digest(f.root/p) for p in m.CODE_PATHS}))
        m.write_state(cls.folder/'latest.json',dict(target_session=m.SESSION,
            checked_at_jst=(cls.now-timedelta(hours=1)).isoformat(),result_state=m.ABSENT,
            http_status=200,deadline_expired=False))
        cls.snapshot={p:p.read_bytes() for p in f.root.rglob('*') if p.is_file()}

    @classmethod
    def tearDownClass(cls): cls.fixture.doCleanups()

    def setUp(self):
        clock=patch.object(installer,'datetime',wraps=datetime); mock=clock.start()
        mock.now.return_value=self.now; self.addCleanup(clock.stop)

    def tearDown(self):
        for p in self.root.rglob('*'):
            if p.is_file() and p not in self.snapshot: p.unlink()
        for p,body in self.snapshot.items():
            if not p.exists() or p.read_bytes()!=body:
                p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(body)

    def prepare(self):
        return installer.prepare_release_transition(self.root,
            expected_previous_release_sha256=self.previous['sha256'],now=self.now)

    def begin_and_renew(self):
        ref=self.prepare();installer.begin_release_transition(self.root,ref['path'])
        release=self.fixture.renew()
        self.assertEqual(core.read_frozen(release['path'])['generation'],5)
        return ref,release

    def finish(self,ref,release):
        return installer.rebind_release_transition(self.root,ref['path'],
            expected_release_sha256=release['sha256'],now=self.now)

    def test_exact_transition_preserves_bytes_and_all_protections(self):
        ref,release=self.begin_and_renew();result=self.finish(ref,release)
        value=core.read_frozen(ref['path']);context=m.read_state(self.folder/'context.json')
        for name,r in value['before'].items():
            original=self.root/installer.INDEX_REFERENCE if name=='current_stops' else self.folder/(name+'.json')
            self.assertEqual(Path(r['path']).read_bytes(),self.snapshot[original])
        self.assertEqual(context['protected_references'][installer.INDEX_REFERENCE],m.digest(self.root/installer.INDEX_REFERENCE))
        for path in (Path(release['path']),Path(release['path']).with_suffix('.sha256')):
            self.assertEqual(context['protected_references'][str(path.relative_to(self.root))],m.digest(path))
        self.assertFalse((self.folder/'STOP.json').exists())
        self.assertEqual(result['http_requests'],0);m.verify_context(self.root,self.folder)

    def test_maintenance_blocks_due_manual_check_and_pre_release_abort_restores(self):
        ref=self.prepare();installer.begin_release_transition(self.root,ref['path'])
        fetch=unittest.mock.Mock(side_effect=AssertionError('HTTP forbidden'))
        result=m.check_once(self.root,now=self.now+timedelta(hours=2),fetch=fetch)
        self.assertEqual(result['result_state'],m.STOP);fetch.assert_not_called()
        installer.abort_release_transition(self.root,ref['path'])
        self.assertEqual((self.folder/'latest.json').read_bytes(),self.snapshot[self.folder/'latest.json'])
        self.assertFalse((self.folder/'STOP.json').exists())

    def test_abort_after_release_forbidden_and_no_rollback(self):
        ref,release=self.begin_and_renew()
        with self.assertRaisesRegex(ValueError,'ABORT_FORBIDDEN'):
            installer.abort_release_transition(self.root,ref['path'])
        self.assertTrue(Path(release['path']).exists());self.assertTrue((self.folder/'STOP.json').exists())

    def test_orphan_publication_before_index_save_also_forbids_abort(self):
        ref=self.prepare();installer.begin_release_transition(self.root,ref['path'])
        with patch.object(policy.Stops,'save',side_effect=OSError('synthetic interrupted index save')):
            with self.assertRaises(OSError):self.fixture.renew()
        self.assertEqual(len(list((self.store/'legacy_reviews_v1').glob('*.json'))),5)
        with self.assertRaisesRegex(ValueError,'REVIEWED_LEGACY_RELEASE_INVALID'):
            installer.abort_release_transition(self.root,ref['path'])
        self.assertTrue((self.folder/'STOP.json').exists())

    def test_release_cli_requires_deferred_rearm_before_systemd_mutation(self):
        with patch.object(m,'ROOT',self.root),patch.object(m,'configure_window'), \
             patch.object(installer.subprocess,'run') as run:
            with self.assertRaisesRegex(ValueError,'REQUIRES_DEFERRED_REARM'):
                installer.install_rebind(m.TARGET,release_transition='intent',expected_release_sha256='0'*64)
            run.assert_not_called()

    def test_wrong_release_index_substitution_and_inventory_change_rejected(self):
        ref,release=self.begin_and_renew()
        with self.assertRaisesRegex(ValueError,'INDEX_DELTA_INVALID'):
            installer.rebind_release_transition(self.root,ref['path'],expected_release_sha256='0'*64,now=self.now)
        index=policy.Stops(self.store).index;index['unrelated_substitution']=True
        policy.write_current(self.store/'current_stops_v2.json',index)
        with self.assertRaisesRegex(ValueError,'INDEX_DELTA_INVALID'): self.finish(ref,release)
        self.assertTrue((self.folder/'STOP.json').exists())

    def test_security_stop_and_state_race_cannot_be_cleared(self):
        ref=self.prepare()
        stop=dict(target_session=m.SESSION,result_state=m.STOP,stop_reason='HTTP_403',http_status=403)
        m.write_state(self.folder/'STOP.json',stop)
        with self.assertRaisesRegex(ValueError,'SECURITY_STOP_FORBIDDEN'):
            installer.begin_release_transition(self.root,ref['path'])
        with self.assertRaisesRegex(ValueError,'SECURITY_STOP_FORBIDDEN'): self.prepare()

    def test_partial_rebind_keeps_maintenance_stop(self):
        ref,release=self.begin_and_renew();original=m.write_state
        def fail(path,record):
            if Path(path)==self.folder/'armed.json': raise OSError('synthetic projection failure')
            return original(path,record)
        with patch.object(m,'write_state',side_effect=fail),self.assertRaises(OSError):self.finish(ref,release)
        self.assertTrue((self.folder/'STOP.json').exists())
        fetch=unittest.mock.Mock(side_effect=AssertionError('HTTP forbidden'))
        m.check_once(self.root,now=self.now+timedelta(hours=2),fetch=fetch);fetch.assert_not_called()
        self.assertTrue(Path(release['path']).exists())

    def test_tampered_release_stale_index_component_mutation_and_old_fallback_rejected(self):
        ref,release=self.begin_and_renew();self.finish(ref,release)
        for path in (Path(release['path']),self.root/installer.INDEX_REFERENCE,
                     self.root/'providers/jp/jpx_manual/historical_direct.py'):
            before=path.read_bytes();path.write_bytes(before+b' ')
            with self.assertRaises(ValueError):m.verify_context(self.root,self.folder)
            path.write_bytes(before)
        (self.root/'state/unrelated-noise.json').write_text('{}');m.verify_context(self.root,self.folder)
        (self.folder/'context.json').write_bytes(self.snapshot[self.folder/'context.json'])
        with self.assertRaises(ValueError):m.verify_context(self.root,self.folder)

    def test_transition_replay_and_seal_corruption_rejected(self):
        ref,release=self.begin_and_renew();self.finish(ref,release)
        with self.assertRaises(ValueError): self.finish(ref,release)
        Path(ref['path']).write_bytes(Path(ref['path']).read_bytes()+b' ')
        with self.assertRaises(ValueError):installer.begin_release_transition(self.root,ref['path'])

    def test_final_reserved_or_closed_window_rejected(self):
        m.write_state(self.folder/'final_check_reserved.json',{})
        with self.assertRaisesRegex(ValueError,'RESERVED_OR_COMPLETED'):self.prepare()
        with self.assertRaisesRegex(ValueError,'WINDOW_CLOSED'):
            installer.prepare_release_transition(self.root,expected_previous_release_sha256=self.previous['sha256'],now=m.DEADLINE)


if __name__=='__main__': unittest.main()

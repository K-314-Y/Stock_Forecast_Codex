"""Install the explicitly authorized one-night local user timers, then exit."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import monitor_jpx_publication as monitor


def quote(value):
    """Systemd command argument quoting, including its percent specifiers."""
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def units(root, python, *, pinned=False):
    script = root / 'scripts/monitor_jpx_publication.py'
    command = ' '.join([quote(python), quote(script), '--session', monitor.SESSION,
                        '--deadline', monitor.DEADLINE.isoformat(), '--once', '--scheduled'])
    if pinned:
        command += ' --target-session ' + monitor.TARGET
    unit = monitor.UNIT
    final = monitor.DEADLINE - monitor.FINAL_CHECK_LEAD
    calendar = lambda stamp: stamp.strftime('%Y-%m-%d %H:%M:%S') + ' Asia/Tokyo'
    common = ('[Service]\nType=oneshot\nWorkingDirectory=' + str(root).replace('%', '%%') + '\n'
              'Environment=TZ=Asia/Tokyo\nUMask=0077\nTimeoutStartSec=50\n')
    return {
        unit + '.service': ('[Unit]\nDescription=JPX publication metadata only\n'
                            'OnFailure=' + unit + '-disarm.service\n' + common +
                            'ExecStart=' + command + '\n'),
        unit + '.timer': ('[Unit]\nDescription=Hourly bounded JPX publication monitor\n'
                          '[Timer]\nOnActiveSec=1h 5s\nOnUnitInactiveSec=1h\n'
                          'AccuracySec=1s\nUnit=' + unit + '.service\n'
                          '[Install]\nWantedBy=timers.target\n'),
        unit + '-final.service': ('[Unit]\nDescription=Final official JPX monthly JSON publication check\n'
                                  'OnFailure=' + unit + '-disarm.service\n' + common +
                                  'ExecStart=' + command + ' --final-check\nRestart=no\n'),
        unit + '-final.timer': ('[Unit]\nDescription=Final JPX check at 08:35 JST\n'
                                '[Timer]\nOnCalendar=' + calendar(final) + '\n'
                                'Persistent=true\nAccuracySec=1s\nRandomizedDelaySec=0\nUnit=' + unit + '-final.service\n'
                                '[Install]\nWantedBy=timers.target\n'),
        unit + '-deadline.service': ('[Unit]\nDescription=JPX deadline bookkeeping without HTTP\n' +
                                     'OnFailure=' + unit + '-deadline-disarm.service\n' +
                                     common + 'ExecStart=' + command + ' --finalize\n'
                                     'Restart=no\nTimeoutStartSec=7500s\n'),
        unit + '-deadline.timer': ('[Unit]\nDescription=JPX monitor hard deadline JST\n'
                                   '[Timer]\nOnCalendar=' + calendar(monitor.DEADLINE) + '\n'
                                   'Persistent=true\nAccuracySec=1s\nUnit=' + unit + '-deadline.service\n'
                                   '[Install]\nWantedBy=timers.target\n'),
        unit + '-deadline-disarm.service': ('[Unit]\nDescription=Latch deadline STOP and disarm both timers\n'
                                           '[Service]\nType=oneshot\nExecStart=-' + command + ' --finalization-failed\n'
                                           'ExecStart=/usr/bin/systemctl --user disable --now ' + unit +
                                           '.timer ' + unit + '-final.timer ' + unit + '-deadline.timer\n'),
        unit + '-disarm.service': ('[Unit]\nDescription=Stop JPX polling on any service failure\n'
                                   '[Service]\nType=oneshot\nExecStart=/usr/bin/systemctl --user '
                                   'disable --now ' + unit + '.timer ' + unit + '-final.timer\n'),
    }


def build_context(root, discovery_reference, *, prior_context=None):
    """Replay retained official discovery; never generate or guess a URL.

    A same-month discovery can anchor another session's metadata check. This
    grants no PDF acquisition, registration, Fresh call or Forecast execution.
    """
    from engine import minimal_trial as core
    from providers.jp.jpx_manual import historical_direct as direct
    from providers.jp.jpx_manual import historical_policy_v2 as lifecycle
    root = Path(root).resolve()
    folder = root / 'state/jpx_publication_monitor' / monitor.SESSION
    path = monitor.safe_path(root, discovery_reference)
    receipt = core.read_frozen(path)
    if (receipt['schema_version'] != 'jpx-json-discovery-v1' or receipt['pdf_requests'] != 0
            or receipt['acquisition_method'] != direct.METHOD
            or receipt['session'][:7] != monitor.SESSION[:7]):
        raise ValueError('MONITOR_DISCOVERY_IDENTITY_FAILURE')
    # Existing hardened gate is checked offline. No release/renewal is authored.
    if prior_context is None:
        stops = lifecycle.Stops(root / 'data/jpx_historical_direct')
        stops.gate(receipt['session'])
    refs = {str(p.relative_to(root)): monitor.digest(p) for p in path.parent.iterdir() if p.is_file()}
    def response(label, first, hash_key):
        body = path.parent / (label + '.body')
        meta = core.read_frozen(path.parent / (label + '.response.json'))
        expected = receipt[hash_key]
        if monitor.digest(body) != expected or meta['sha256'] != expected:
            raise ValueError('MONITOR_DISCOVERY_BODY_HASH_CONFLICT')
        chain = receipt['redirect_chains'][label]
        direct._verify_redirect_evidence(chain, first, expected)
        if meta['chain'] != chain or meta['url'] != chain[-1]['response_url']:
            raise ValueError('MONITOR_DISCOVERY_CHAIN_CONFLICT')
        last = chain[-1]
        return direct.Response(meta['url'], last['status'], last['headers'], body.read_bytes())
    listing = response('listing', direct.LISTING, 'listing_sha256')
    discovery = direct.json_endpoints(listing)
    if discovery != receipt['discovery']:
        raise ValueError('MONITOR_DISCOVERY_PROGRAM_CONFLICT')
    monthly_list = response('monthly_list', discovery['monthly_list_endpoint'], 'monthly_list_sha256')
    url = direct.monthly_json_url(monthly_list, discovery, monitor.SESSION)
    if url != receipt['monthly_json_reference_url'] or not url:
        raise ValueError('MONITOR_DISCOVERY_MONTH_CONFLICT')
    monthly = response('monthly', url, 'monthly_json_sha256')
    direct.json_stock_link(monthly, monitor.SESSION)  # Existing JSON semantics, no PDF GET.
    if not monitor.urlsplit(url).path.startswith(monitor.MONTHLY_FAMILY):
        raise ValueError('MONITOR_DISCOVERY_SOURCE_FAMILY_CONFLICT')
    for relative in ('config/forward_trial_execution_policy.json',
                     'config/jpx_daily_acquisition_config.json', 'config/primary_forward_trial_v1.json',
                     'data/jpx_historical_direct/STOP.json', 'data/jpx_historical_direct/current_stops_v2.json'):
        source = monitor.safe_path(root, relative)
        refs[relative] = monitor.digest(source)
    rules = monitor.strict_json((root / 'config/forward_trial_execution_policy.json').read_bytes())
    refs[rules['calendar']] = monitor.digest(monitor.safe_path(root, rules['calendar']))
    for source in (root / 'data/jpx_historical_direct/legacy_reviews_v1').glob('*.json'):
        refs[str(source.relative_to(root))] = monitor.digest(source)
    context = dict(schema_version=monitor.WINDOW_SCHEMA, target_session=monitor.SESSION,
                   forecast_target_session=monitor.TARGET, deadline=monitor.DEADLINE.isoformat(),
                   monthly_url=url, discovery_reference=str(path.relative_to(root)),
                   protected_references=refs, approved_at_jst=datetime.now(monitor.JST).isoformat())
    return context


def create_context(root, discovery_reference):
    context = build_context(root, discovery_reference)
    folder = Path(root) / 'state/jpx_publication_monitor' / monitor.SESSION
    with monitor.locked(folder):
        if any(monitor.present(folder / name) for name in ('context.json', 'armed.json', 'latest.json', 'STOP.json')):
            raise ValueError('MONITOR_CONTEXT_CREATE_ONLY')
        monitor.write_state(folder / 'context.json', context)
    return context


def rebind(root, *, now=None):
    """Explicit offline renewal of an identity STOP, never a security release.

    Retain the exact old envelopes before replacing current projections. Only
    the acquisition config and committed component code may advance; discovery,
    calendar, execution policy, STOP and release evidence must stay unchanged.
    """
    from providers.jp.jpx_manual import acquisition_binding as binding
    root = Path(root).resolve()
    folder = root / 'state/jpx_publication_monitor' / monitor.SESSION
    now = now or datetime.now(monitor.JST)
    if now.tzinfo is None or now >= monitor.DEADLINE - monitor.FINAL_CHECK_LEAD:
        raise ValueError('MONITOR_REBIND_WINDOW_CLOSED')
    with monitor.locked(folder):
        old_context = monitor.read_state(folder / 'context.json')
        old_armed = monitor.read_state(folder / 'armed.json')
        stop = monitor.read_state(folder / 'STOP.json')
        if (stop != monitor.read_state(folder / 'latest.json')
                or stop.get('result_state') != monitor.STOP or stop.get('http_status') is not None
                or stop.get('deadline_expired') is not False
                or stop.get('stop_reason') not in {
                    'MONITOR_PROTECTED_CONTEXT_CHANGED:config/jpx_daily_acquisition_config.json',
                    'MONITOR_CODE_CHANGED', 'MONITOR_ACQUISITION_COMPONENT_CHANGED'}):
            raise ValueError('MONITOR_REBIND_SECURITY_STOP_FORBIDDEN')
        if any(monitor.present(folder / name) for name in (
                'final_check_reserved.json', 'deadline_reserved.json', 'deadline_retry.json',
                'task10c6c_completion.json')):
            raise ValueError('MONITOR_REBIND_RESERVED_OR_COMPLETED')
        if (old_context.get('schema_version') != monitor.WINDOW_SCHEMA
                or old_context.get('target_session') != monitor.SESSION
                or old_context.get('forecast_target_session') != monitor.TARGET
                or old_context.get('deadline') != monitor.DEADLINE.isoformat()
                or old_armed.get('target_session') != monitor.SESSION
                or old_armed.get('deadline') != monitor.DEADLINE.isoformat()
                or stop.get('target_session') != monitor.SESSION):
            raise ValueError('MONITOR_REBIND_WINDOW_CONFLICT')
        old_commit = old_armed['code_commit']
        if (not monitor.re.fullmatch(r'[0-9a-f]{40}', old_commit)
                or datetime.fromisoformat(stop['checked_at_jst']).tzinfo is None
                or now < datetime.fromisoformat(stop['checked_at_jst'])):
            raise ValueError('MONITOR_REBIND_OLD_IDENTITY_INVALID')
        binding.git(root, 'merge-base', '--is-ancestor', old_commit, 'HEAD')
        if set(old_armed['code_hashes']) != set(monitor.CODE_PATHS):
            raise ValueError('MONITOR_REBIND_OLD_CODE_INVALID')
        for relative, expected in old_armed['code_hashes'].items():
            if monitor.hashlib.sha256(binding.git(root, 'show', old_commit + ':' + relative)).hexdigest() != expected:
                raise ValueError('MONITOR_REBIND_OLD_CODE_INVALID')
        approved = 'config/jpx_daily_acquisition_config.json'
        for relative, expected in old_context['protected_references'].items():
            actual = monitor.digest(monitor.safe_path(root, relative))
            if relative == approved:
                if monitor.hashlib.sha256(binding.git(root, 'show', old_commit + ':' + relative)).hexdigest() != expected:
                    raise ValueError('MONITOR_REBIND_OLD_CONFIG_INVALID')
            elif actual != expected:
                raise ValueError('MONITOR_REBIND_PROTECTED_EVIDENCE_CHANGED:' + relative)
        # This monitor-only approval preserves the previous security gate's
        # exact evidence. It does not renew a stale historical acquisition release.
        context = build_context(root, old_context['discovery_reference'], prior_context=old_context)
        if (set(context['protected_references']) != set(old_context['protected_references'])
                or context['monthly_url'] != old_context['monthly_url']):
            raise ValueError('MONITOR_REBIND_PROTECTION_INVENTORY_CONFLICT')
        context['acquisition_fingerprint'] = binding.fingerprint(binding.components(root, infrastructure=True))
        commit = binding.git(root, 'rev-parse', 'HEAD').decode().strip()
        armed = dict(started_at_jst=now.isoformat(), target_session=monitor.SESSION,
                     deadline=monitor.DEADLINE.isoformat(), code_commit=commit,
                     context_sha256=monitor.hashlib.sha256(monitor.canonical(context)).hexdigest(),
                     code_hashes={p: monitor.digest(root / p) for p in monitor.CODE_PATHS})
        identity = monitor.hashlib.sha256(monitor.canonical(dict(context=context, armed=armed))).hexdigest()
        archive = folder / 'bindings' / identity
        with monitor._folder_fd(folder) as source, monitor._folder_fd(archive, create=True) as destination:
            for name in ('context.json', 'armed.json', 'STOP.json', 'latest.json'):
                # Preserve exact envelopes without giving current projections extra links.
                with os.fdopen(os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source), 'rb') as stream:
                    payload = stream.read()
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             mode=0o600, dir_fd=destination)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            os.fsync(destination)
        monitor.write_state(archive / 'renewal.json', dict(
            binding_identity=identity, previous_code_commit=old_commit, code_commit=commit,
            previous_stop_reason=stop['stop_reason'], approved_at_jst=now.isoformat(),
            authorization='EXPLICIT_MONITOR_IDENTITY_REBIND', http_requests=0))
        monitor.write_state(folder / 'context.json', context)
        monitor.write_state(folder / 'armed.json', armed)
        monitor.verify_context(root, folder)
        monitor.write_state(folder / 'latest.json', dict(
            target_session=monitor.SESSION, checked_at_jst=now.isoformat(),
            result_state=monitor.BOUND, deadline_expired=False,
            http_status=None, publication_found=False, code_commit=commit, binding_identity=identity,
            source_identity={'monthly_json_url': context['monthly_url']}))
        with monitor._folder_fd(folder) as directory:
            os.unlink('STOP.json', dir_fd=directory)
            os.fsync(directory)
        return dict(binding_identity=identity, code_commit=commit, http_requests=0)


INDEX_REFERENCE = 'data/jpx_historical_direct/current_stops_v2.json'
TRANSITION_SCHEMA = 'jpx-monitor-reviewed-release-transition-v1'


def _transition_window(now):
    if now.tzinfo is None or now >= monitor.DEADLINE - monitor.FINAL_CHECK_LEAD:
        raise ValueError('MONITOR_REBIND_WINDOW_CLOSED')


def _transition_receipt(root, path):
    from engine import minimal_trial as core
    from providers.jp.jpx_manual import historical_policy_v2 as policy
    folder = root / 'state/jpx_publication_monitor' / monitor.SESSION
    path = monitor.safe_path(root, str(Path(path).relative_to(root)))
    if path.parent.parent != folder / 'release_transitions' or path.name != 'transition.json':
        raise ValueError('MONITOR_RELEASE_TRANSITION_SCOPE_INVALID')
    value = core.read_frozen(path)
    if (value['schema_version'] != TRANSITION_SCHEMA or value['project_root'] != str(root)
            or value['target_session'] != monitor.SESSION
            or value['forecast_target_session'] != monitor.TARGET
            or value['deadline'] != monitor.DEADLINE.isoformat()):
        raise ValueError('MONITOR_RELEASE_TRANSITION_IDENTITY_INVALID')
    for ref in value['before'].values():
        source = policy.verify_reference(ref)
        if source.parent != path.parent or source.is_symlink():
            raise ValueError('MONITOR_RELEASE_TRANSITION_SCOPE_INVALID')
    return folder, path, value


def prepare_release_transition(root, *, expected_previous_release_sha256, now=None):
    """Capture exact pre-transition bytes; authorize only one append-only renewal.

    The caller quiesces all three timers/services before begin_release_transition.
    This preparation neither changes current projections nor runs a service.
    """
    from engine import forward_artifacts as storage
    from providers.jp.jpx_manual import historical_policy_v2 as policy
    from providers.jp.jpx_manual import legacy_stop_review as review
    from providers.jp.jpx_manual import acquisition_binding as binding
    root = Path(root).resolve(); now = now or datetime.now(monitor.JST)
    _transition_window(now)
    folder = root / 'state/jpx_publication_monitor' / monitor.SESSION
    with storage._locked(root / 'data/jpx_historical_direct'), monitor.locked(folder):
        stops = policy.Stops.__new__(policy.Stops)
        stops._load_evidence(root / 'data/jpx_historical_direct')
        chain = review._verified_chain(stops, bind_latest=False)
        if not chain or chain[-1][0]['sha256'] != expected_previous_release_sha256:
            raise ValueError('MONITOR_RELEASE_TRANSITION_PREVIOUS_RELEASE_INVALID')
        context = monitor.read_state(folder / 'context.json')
        armed = monitor.read_state(folder / 'armed.json')
        latest = monitor.read_state(folder / 'latest.json')
        if any(monitor.present(folder / name) for name in (
                'final_check_reserved.json', 'deadline_reserved.json', 'deadline_retry.json',
                'task10c6c_completion.json')):
            raise ValueError('MONITOR_REBIND_RESERVED_OR_COMPLETED')
        # A deployment identity STOP is eligible only with its exact old Git code.
        # Security, integrity, interrupted HTTP and terminal results never qualify.
        has_stop = monitor.present(folder / 'STOP.json')
        if has_stop:
            stop = monitor.read_state(folder / 'STOP.json')
            if (stop != latest or stop.get('stop_reason') != 'MONITOR_CODE_CHANGED'
                    or stop.get('http_status') is not None or stop.get('deadline_expired') is not False):
                raise ValueError('MONITOR_REBIND_SECURITY_STOP_FORBIDDEN')
        elif latest['result_state'] not in (monitor.ABSENT, monitor.BOUND):
            raise ValueError('MONITOR_RELEASE_TRANSITION_PENDING_OR_TERMINAL')
        if (context.get('target_session') != monitor.SESSION
                or context.get('forecast_target_session') != monitor.TARGET
                or context.get('deadline') != monitor.DEADLINE.isoformat()
                or armed.get('target_session') != monitor.SESSION
                or armed.get('deadline') != monitor.DEADLINE.isoformat()
                or armed.get('context_sha256') != monitor.hashlib.sha256(monitor.canonical(context)).hexdigest()
                or set(armed['code_hashes']) != set(monitor.CODE_PATHS)):
            raise ValueError('MONITOR_RELEASE_TRANSITION_IDENTITY_INVALID')
        binding.git(root, 'merge-base', '--is-ancestor', armed['code_commit'], 'HEAD')
        for name, digest in armed['code_hashes'].items():
            if monitor.hashlib.sha256(binding.git(root, 'show', armed['code_commit'] + ':' + name)).hexdigest() != digest:
                raise ValueError('MONITOR_REBIND_OLD_CODE_INVALID')
        for name, digest in context['protected_references'].items():
            if monitor.digest(monitor.safe_path(root, name)) != digest:
                raise ValueError('MONITOR_REBIND_PROTECTED_EVIDENCE_CHANGED:' + name)
        if (INDEX_REFERENCE not in context['protected_references']
                or context['acquisition_fingerprint'] != binding.fingerprint(binding.components(root, infrastructure=True))):
            raise ValueError('MONITOR_RELEASE_TRANSITION_ACQUISITION_CHANGED')
        # A random create-only transaction identity prevents reuse of an old intent.
        archive = folder / 'release_transitions' / monitor.uuid4().hex
        before = {}
        with monitor._folder_fd(archive, create=True) as destination:
            sources = dict(context=folder/'context.json', armed=folder/'armed.json',
                           latest=folder/'latest.json', current_stops=root/INDEX_REFERENCE)
            if has_stop: sources['stop'] = folder/'STOP.json'
            for name, source in sources.items():
                payload = source.read_bytes()
                fd = os.open(name+'.json', os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,
                             mode=0o600, dir_fd=destination)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(payload); stream.flush(); os.fsync(stream.fileno())
                before[name] = policy.reference(archive/(name+'.json'))
            os.fsync(destination)
        marker = dict(target_session=monitor.SESSION, checked_at_jst=now.isoformat(),
                      result_state=monitor.STOP, deadline_expired=False, http_status=None,
                      publication_found=False, stop_reason='AUTHORIZED_RELEASE_TRANSITION_IN_PROGRESS',
                      transition_id=archive.name)
        value = dict(schema_version=TRANSITION_SCHEMA, project_root=str(root),
                     target_session=monitor.SESSION, forecast_target_session=monitor.TARGET,
                     deadline=monitor.DEADLINE.isoformat(), prepared_at=now.isoformat(),
                     previous_release_reference=chain[-1][0], generation=len(chain)+1,
                     before=before, maintenance_stop=marker)
        policy.seal(archive/'transition.json', value)
        return policy.reference(archive/'transition.json')


def begin_release_transition(root, transition):
    """CAS the captured state, then latch a zero-HTTP maintenance STOP."""
    root = Path(root).resolve()
    folder, path, value = _transition_receipt(root, transition)
    with monitor.locked(folder):
        _transition_window(datetime.now(monitor.JST))
        for name, ref in value['before'].items():
            source = root/INDEX_REFERENCE if name == 'current_stops' else folder/('STOP.json' if name == 'stop' else name+'.json')
            if monitor.digest(source) != ref['sha256']:
                raise ValueError('MONITOR_RELEASE_TRANSITION_STATE_CHANGED')
        if 'stop' not in value['before'] and monitor.present(folder/'STOP.json'):
            raise ValueError('MONITOR_REBIND_SECURITY_STOP_FORBIDDEN')
        monitor.write_state(folder/'STOP.json', value['maintenance_stop'])
        monitor.write_state(folder/'latest.json', value['maintenance_stop'])


def abort_release_transition(root, transition):
    """Only a pre-release failure can restore the exact captured projections."""
    from engine import forward_artifacts as storage
    from providers.jp.jpx_manual import historical_policy_v2 as policy
    from providers.jp.jpx_manual import legacy_stop_review as review
    root = Path(root).resolve()
    folder, path, value = _transition_receipt(root, transition)
    with storage._locked(root/'data/jpx_historical_direct'), monitor.locked(folder):
        stops = policy.Stops.__new__(policy.Stops)
        stops._load_evidence(root/'data/jpx_historical_direct')
        chain = review._verified_chain(stops,bind_latest=False)
        if (monitor.digest(root/INDEX_REFERENCE) != value['before']['current_stops']['sha256']
                or len(chain) != value['generation']-1
                or chain[-1][0] != value['previous_release_reference']
                or monitor.read_state(folder/'STOP.json') != value['maintenance_stop']):
            raise ValueError('MONITOR_RELEASE_TRANSITION_ABORT_FORBIDDEN')
        monitor.write_state(folder/'latest.json', monitor.read_state(Path(value['before']['latest']['path'])))
        if 'stop' in value['before']:
            monitor.write_state(folder/'STOP.json', monitor.read_state(Path(value['before']['stop']['path'])))
        else:
            with monitor._folder_fd(folder) as fd:
                os.unlink('STOP.json', dir_fd=fd); os.fsync(fd)


def rebind_release_transition(root, transition, *, expected_release_sha256, now=None):
    """Explicit sealed intent + exact one-generation index delta; never blind rebind."""
    from engine import forward_artifacts as storage
    from providers.jp.jpx_manual import acquisition_binding as binding
    from providers.jp.jpx_manual import historical_policy_v2 as policy
    from providers.jp.jpx_manual import legacy_stop_review as review
    root = Path(root).resolve(); now = now or datetime.now(monitor.JST)
    _transition_window(now)
    folder, path, value = _transition_receipt(root, transition)
    with storage._locked(root/'data/jpx_historical_direct'), monitor.locked(folder):
        if (not monitor.present(folder/'STOP.json') or not monitor.present(folder/'latest.json')
                or monitor.read_state(folder/'STOP.json') != value['maintenance_stop']
                or monitor.read_state(folder/'latest.json') != value['maintenance_stop']):
            raise ValueError('MONITOR_RELEASE_TRANSITION_MAINTENANCE_CHANGED')
        if binding.git(root, 'status', '--porcelain=v1'):
            raise ValueError('COMMIT_MONITOR_CODE_BEFORE_ARMING')
        old_context = monitor.read_state(Path(value['before']['context']['path']))
        old_index = monitor.read_state(Path(value['before']['current_stops']['path']))
        stops = policy.Stops(root/'data/jpx_historical_direct')
        chain = review._verified_chain(stops)
        if (len(chain) != value['generation'] or chain[-1][1]['generation'] != value['generation']
                or chain[-1][0]['sha256'] != expected_release_sha256
                or chain[-1][1]['previous_release_reference'] != value['previous_release_reference']
                or old_index['reviewed_legacy_releases'] != stops.index['reviewed_legacy_releases'][:-1]
                or {k:v for k,v in old_index.items() if k!='reviewed_legacy_releases'} !=
                   {k:v for k,v in stops.index.items() if k!='reviewed_legacy_releases'}
                or chain[-1][1]['current_code_commit'] != binding.git(root,'rev-parse','HEAD').decode().strip()):
            raise ValueError('MONITOR_RELEASE_TRANSITION_INDEX_DELTA_INVALID')
        stops.gate(monitor.SESSION)  # Other host/global/session security STOPs still apply.
        for name, ref in value['before'].items():
            if name in ('context','armed') and monitor.digest(folder/(name+'.json')) != ref['sha256']:
                raise ValueError('MONITOR_RELEASE_TRANSITION_STATE_CHANGED')
        for name, digest in old_context['protected_references'].items():
            expected = value['before']['current_stops']['sha256'] if name == INDEX_REFERENCE else monitor.digest(monitor.safe_path(root,name))
            if digest != expected:
                raise ValueError('MONITOR_REBIND_PROTECTED_EVIDENCE_CHANGED:'+name)
        context = build_context(root, old_context['discovery_reference'])
        receipt_path = Path(chain[-1][0]['path'])
        new_refs = {str(p.relative_to(root)) for p in (receipt_path,receipt_path.with_suffix('.sha256'))}
        context['protected_references'][str(receipt_path.with_suffix('.sha256').relative_to(root))] = monitor.digest(receipt_path.with_suffix('.sha256'))
        if set(context['protected_references']) != set(old_context['protected_references']) | new_refs:
            raise ValueError('MONITOR_REBIND_PROTECTION_INVENTORY_CONFLICT')
        context['acquisition_fingerprint'] = binding.fingerprint(binding.components(root,infrastructure=True))
        commit = binding.git(root,'rev-parse','HEAD').decode().strip()
        if context['acquisition_fingerprint'] != chain[-1][1].get('component_fingerprint'):
            raise ValueError('MONITOR_RELEASE_TRANSITION_ACQUISITION_CHANGED')
        armed = dict(started_at_jst=now.isoformat(), target_session=monitor.SESSION,
                     deadline=monitor.DEADLINE.isoformat(),code_commit=commit,
                     context_sha256=monitor.hashlib.sha256(monitor.canonical(context)).hexdigest(),
                     code_hashes={p:monitor.digest(root/p) for p in monitor.CODE_PATHS})
        if (binding.git(root,'status','--porcelain=v1')
                or commit != chain[-1][1]['current_code_commit']
                or any(digest != monitor.hashlib.sha256(binding.git(root,'show',commit+':'+name)).hexdigest()
                       for name,digest in armed['code_hashes'].items())):
            raise ValueError('MONITOR_RELEASE_TRANSITION_CURRENT_CODE_CHANGED')
        identity = monitor.hashlib.sha256(monitor.canonical(dict(context=context,armed=armed))).hexdigest()
        policy.seal(path.parent/'rebind.json', dict(binding_identity=identity,
                    release_reference=chain[-1][0],current_stops=policy.reference(root/INDEX_REFERENCE),
                    authorization='EXPLICIT_REVIEWED_RELEASE_STATE_TRANSITION',http_requests=0))
        monitor.write_state(folder/'context.json',context)
        monitor.write_state(folder/'armed.json',armed)
        monitor.verify_context(root,folder)
        _transition_window(datetime.now(monitor.JST))
        monitor.write_state(folder/'latest.json',dict(target_session=monitor.SESSION,
                    checked_at_jst=now.isoformat(),result_state=monitor.BOUND,deadline_expired=False,
                    http_status=None,publication_found=False,code_commit=commit,binding_identity=identity,
                    source_identity={'monthly_json_url':context['monthly_url']}))
        with monitor._folder_fd(folder) as fd:
            os.unlink('STOP.json',dir_fd=fd);os.fsync(fd)
        return dict(binding_identity=identity,code_commit=commit,http_requests=0)


def install_rebind(target, *, release_transition=None, expected_release_sha256=None, defer_rearm=False):
    """Arm generated timers without starting a service or making HTTP calls."""
    root = monitor.ROOT
    monitor.configure_window(root, target)
    if release_transition and not defer_rearm:
        raise ValueError('MONITOR_RELEASE_TRANSITION_REQUIRES_DEFERRED_REARM')
    env = monitor.systemd_env()
    if subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain'], text=True):
        raise ValueError('COMMIT_MONITOR_CODE_BEFORE_ARMING')
    destination = Path.home() / '.config/systemd/user'
    definitions = units(root, sys.executable, pinned=True)
    for name, content in definitions.items():
        if (destination / name).read_text() != content:
            raise ValueError('EXISTING_SYSTEMD_UNIT_CONFLICT:' + name)
    subprocess.run(['systemctl', '--user', 'stop', *[monitor.UNIT+suffix for suffix in
                    ('.timer','-final.timer','-deadline.timer','.service','-final.service','-deadline.service')]], env=env, check=True)
    result = (rebind_release_transition(root,release_transition,expected_release_sha256=expected_release_sha256)
              if release_transition else rebind(root))
    subprocess.run(['systemd-analyze', '--user', 'verify',
                    *[str(destination / name) for name in definitions]], env=env, check=True)
    subprocess.run(['systemctl', '--user', 'daemon-reload'], env=env, check=True)
    if not defer_rearm:
        subprocess.run(['systemctl', '--user', 'enable', '--now', monitor.UNIT + '-deadline.timer',
                        monitor.UNIT + '-final.timer', monitor.UNIT + '.timer'], env=env, check=True)
    print(json.dumps(result, ensure_ascii=False))


def install(discovery_reference=None):
    root = monitor.ROOT
    if discovery_reference:
        monitor.configure_window(root)
    folder = root / 'state/jpx_publication_monitor' / monitor.SESSION
    now = datetime.now(monitor.JST)
    if now >= monitor.DEADLINE:
        raise ValueError('MONITOR_DEADLINE_ALREADY_PASSED')
    env = monitor.systemd_env()
    subprocess.run(['systemctl', '--user', 'show-environment'], env=env,
                   check=True, stdout=subprocess.DEVNULL)
    if subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain'], text=True):
        raise ValueError('COMMIT_MONITOR_CODE_BEFORE_ARMING')
    if discovery_reference:
        create_context(root, discovery_reference)
    with monitor.locked(folder):
        for name in ('STOP.json', 'latest.json'):
            if monitor.present(folder / name) and monitor.read_state(folder / name)['result_state'] in monitor.TERMINAL:
                raise ValueError('TERMINAL_MONITOR_MUST_NOT_BE_REARMED')
        if monitor.present(folder / 'armed.json'):
            raise ValueError('MONITOR_ALREADY_ARMED_USE_SYSTEMCTL_STATUS')
        commit = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
        armed = dict(started_at_jst=now.isoformat(), target_session=monitor.SESSION,
                     deadline=monitor.DEADLINE.isoformat(), code_commit=commit,
                     code_hashes={p: monitor.digest(root / p) for p in (
                         *monitor.CODE_PATHS,)})
        monitor.write_state(folder / 'armed.json', armed)
        try:
            monitor.verify_context(root, folder)
        except Exception:
            (folder / 'armed.json').unlink()
            raise
        destination = Path.home() / '.config/systemd/user'
        destination.mkdir(parents=True, exist_ok=True)
        definitions = units(root, sys.executable, pinned=bool(discovery_reference))
        for name, content in definitions.items():
            path = destination / name
            if path.exists() and path.read_text() != content:
                raise ValueError('EXISTING_SYSTEMD_UNIT_CONFLICT:' + name)
        for name, content in definitions.items():
            (destination / name).write_text(content)
        subprocess.run(['systemd-analyze', '--user', 'verify',
                        *[str(destination / name) for name in definitions]], env=env, check=True)
        subprocess.run(['systemctl', '--user', 'daemon-reload'], env=env, check=True)
        subprocess.run(['systemctl', '--user', 'enable', '--now', monitor.UNIT + '-deadline.timer'], env=env, check=True)
        subprocess.run(['systemctl', '--user', 'enable', '--now', monitor.UNIT + '-final.timer'], env=env, check=True)
        # Enable polling before the immediate check; a terminal initial result
        # can then disable it and cannot accidentally be re-enabled afterwards.
        subprocess.run(['systemctl', '--user', 'enable', '--now', monitor.UNIT + '.timer'], env=env, check=True)
    subprocess.run(['systemctl', '--user', 'start', monitor.UNIT + '.service'], env=env, check=True)
    print(json.dumps(armed, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--arm', action='store_true', required=True)
    parser.add_argument('--discovery-reference', help='Retained official same-month discovery.json; select next calendar window')
    parser.add_argument('--rebind-target', help='Explicit offline identity-STOP renewal; no immediate HTTP')
    parser.add_argument('--release-transition', help='Sealed explicit reviewed-release transition intent')
    parser.add_argument('--expected-release-sha256', help='Exact newly verified release SHA256')
    parser.add_argument('--defer-rearm', action='store_true', help='Keep timers stopped for fresh-process verification')
    args = parser.parse_args()
    if args.rebind_target:
        if args.discovery_reference:
            parser.error('Rebind uses the existing retained discovery reference')
        if bool(args.release_transition) != bool(args.expected_release_sha256):
            parser.error('Release transition and expected release SHA256 are required together')
        install_rebind(args.rebind_target,release_transition=args.release_transition,
                       expected_release_sha256=args.expected_release_sha256,defer_rearm=args.defer_rearm)
    else:
        if args.release_transition or args.expected_release_sha256 or args.defer_rearm:
            parser.error('Transition options require --rebind-target')
        install(args.discovery_reference)

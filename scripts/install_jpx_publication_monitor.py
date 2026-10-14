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
            result_state='JPX_PUBLICATION_MONITOR_BOUND_OFFLINE', deadline_expired=False,
            http_status=None, publication_found=False, code_commit=commit, binding_identity=identity,
            source_identity={'monthly_json_url': context['monthly_url']}))
        with monitor._folder_fd(folder) as directory:
            os.unlink('STOP.json', dir_fd=directory)
            os.fsync(directory)
        return dict(binding_identity=identity, code_commit=commit, http_requests=0)


def install_rebind(target):
    """Arm generated timers without starting a service or making HTTP calls."""
    root = monitor.ROOT
    monitor.configure_window(root, target)
    env = monitor.systemd_env()
    if subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain'], text=True):
        raise ValueError('COMMIT_MONITOR_CODE_BEFORE_ARMING')
    destination = Path.home() / '.config/systemd/user'
    definitions = units(root, sys.executable, pinned=True)
    for name, content in definitions.items():
        if (destination / name).read_text() != content:
            raise ValueError('EXISTING_SYSTEMD_UNIT_CONFLICT:' + name)
    subprocess.run(['systemctl', '--user', 'stop', monitor.UNIT + '.timer', monitor.UNIT + '-final.timer',
                    monitor.UNIT + '.service', monitor.UNIT + '-final.service'], env=env, check=True)
    result = rebind(root)
    subprocess.run(['systemd-analyze', '--user', 'verify',
                    *[str(destination / name) for name in definitions]], env=env, check=True)
    subprocess.run(['systemctl', '--user', 'daemon-reload'], env=env, check=True)
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
    args = parser.parse_args()
    if args.rebind_target:
        if args.discovery_reference:
            parser.error('Rebind uses the existing retained discovery reference')
        install_rebind(args.rebind_target)
    else:
        install(args.discovery_reference)

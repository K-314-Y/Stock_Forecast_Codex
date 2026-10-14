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


def create_context(root, discovery_reference):
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
    with monitor.locked(folder):
        if any(monitor.present(folder / name) for name in ('context.json', 'armed.json', 'latest.json', 'STOP.json')):
            raise ValueError('MONITOR_CONTEXT_CREATE_ONLY')
        monitor.write_state(folder / 'context.json', context)
    return context


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
    args = parser.parse_args()
    install(args.discovery_reference)

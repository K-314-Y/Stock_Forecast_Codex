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


def units(root, python):
    script = root / 'scripts/monitor_jpx_publication.py'
    command = ' '.join([quote(python), quote(script), '--session', monitor.SESSION,
                        '--deadline', monitor.DEADLINE.isoformat(), '--once', '--scheduled'])
    unit = monitor.UNIT
    common = ('[Service]\nType=oneshot\nWorkingDirectory=' + str(root).replace('%', '%%') + '\n'
              'Environment=TZ=Asia/Tokyo\nUMask=0077\nTimeoutStartSec=50\n')
    return {
        unit + '.service': ('[Unit]\nDescription=JPX October 13 publication metadata only\n'
                            'OnFailure=' + unit + '-disarm.service\n' + common +
                            'ExecStart=' + command + '\n'),
        unit + '.timer': ('[Unit]\nDescription=Hourly bounded JPX publication monitor\n'
                          '[Timer]\nOnActiveSec=1h\nOnUnitInactiveSec=1h\n'
                          'AccuracySec=1s\nUnit=' + unit + '.service\n'
                          '[Install]\nWantedBy=timers.target\n'),
        unit + '-deadline.service': ('[Unit]\nDescription=JPX deadline bookkeeping without HTTP\n' +
                                     common + 'ExecStart=' + command + ' --finalize\n'
                                     'Restart=on-failure\nRestartSec=1h\n'),
        unit + '-deadline.timer': ('[Unit]\nDescription=JPX monitor hard deadline JST\n'
                                   '[Timer]\nOnCalendar=2026-10-14 08:45:00 Asia/Tokyo\n'
                                   'Persistent=true\nAccuracySec=1s\nUnit=' + unit + '-deadline.service\n'
                                   '[Install]\nWantedBy=timers.target\n'),
        unit + '-disarm.service': ('[Unit]\nDescription=Stop JPX polling on any service failure\n'
                                   '[Service]\nType=oneshot\nExecStart=/usr/bin/systemctl --user '
                                   'disable --now ' + unit + '.timer\n'),
    }


def install():
    root = monitor.ROOT
    folder = root / 'state/jpx_publication_monitor' / monitor.SESSION
    now = datetime.now(monitor.JST)
    if now >= monitor.DEADLINE:
        raise ValueError('MONITOR_DEADLINE_ALREADY_PASSED')
    env = monitor.systemd_env()
    subprocess.run(['systemctl', '--user', 'show-environment'], env=env,
                   check=True, stdout=subprocess.DEVNULL)
    if subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain'], text=True):
        raise ValueError('COMMIT_MONITOR_CODE_BEFORE_ARMING')
    with monitor.locked(folder):
        for name in ('STOP.json', 'latest.json'):
            if (folder / name).exists() and monitor.read_state(folder / name)['result_state'] in monitor.TERMINAL:
                raise ValueError('TERMINAL_MONITOR_MUST_NOT_BE_REARMED')
        if (folder / 'armed.json').exists():
            raise ValueError('MONITOR_ALREADY_ARMED_USE_SYSTEMCTL_STATUS')
        commit = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
        armed = dict(started_at_jst=now.isoformat(), target_session=monitor.SESSION,
                     deadline=monitor.DEADLINE.isoformat(), code_commit=commit,
                     code_hashes={p: monitor.digest(root / p) for p in (
                         'scripts/monitor_jpx_publication.py', 'scripts/install_jpx_publication_monitor.py')})
        monitor.write_state(folder / 'armed.json', armed)
        try:
            monitor.verify_context(root, folder)
        except Exception:
            (folder / 'armed.json').unlink()
            raise
        destination = Path.home() / '.config/systemd/user'
        destination.mkdir(parents=True, exist_ok=True)
        definitions = units(root, sys.executable)
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
        # Enable polling before the immediate check; a terminal initial result
        # can then disable it and cannot accidentally be re-enabled afterwards.
        subprocess.run(['systemctl', '--user', 'enable', '--now', monitor.UNIT + '.timer'], env=env, check=True)
    subprocess.run(['systemctl', '--user', 'start', monitor.UNIT + '.service'], env=env, check=True)
    print(json.dumps(armed, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--arm', action='store_true', required=True)
    parser.parse_args()
    install()

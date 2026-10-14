"""Bounded, metadata-only JPX monitor. No acquisition or Forecast imports.

The ignored context is an offline checkpoint of the already approved discovery
and STOP gate. It grants only this separately authorized monthly-JSON check;
it is never a release receipt for the historical acquisition adapter.
"""
import argparse
from contextlib import contextmanager
from datetime import date, datetime, timedelta
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import stat
import subprocess
import time
import sys
from urllib.parse import unquote, urljoin, urlsplit
from zoneinfo import ZoneInfo
from uuid import uuid4

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import forecast_not_run_ledger as not_run

ROOT = Path(__file__).resolve().parents[1]
JST = ZoneInfo('Asia/Tokyo')
SESSION = '2026-10-13'
TARGET = '2026-10-14'
DEADLINE = datetime(2026, 10, 14, 8, 45, tzinfo=JST)
BRANCH = 'dev/parallel-forward-models-v1'
ABSENT = 'JPX_2026_10_13_NOT_YET_AVAILABLE'
DETECTED = 'JPX_2026_10_13_PUBLICATION_DETECTED'
EXPIRED = 'JPX_PUBLICATION_MONITOR_DEADLINE_EXPIRED'
STOP = 'JPX_PUBLICATION_MONITOR_STOP'
PENDING = 'JPX_PUBLICATION_MONITOR_CHECK_IN_PROGRESS'
TERMINAL = {DETECTED, EXPIRED, STOP}
UNIT = 'jpx-publication-20261013'
MAX_FINALIZATION_ATTEMPTS = 3
FINALIZATION_RETRY_SECONDS = 3600
CODE_PATHS = ('scripts/monitor_jpx_publication.py',
              'scripts/install_jpx_publication_monitor.py',
              'scripts/forecast_not_run_ledger.py')
WINDOW_SCHEMA = 'jpx-publication-window-v2'
FINAL_CHECK_LEAD = timedelta(minutes=10)
FINAL_CHECK_LATEST_LEAD = timedelta(minutes=5)
MONTHLY_FAMILY = '/automation/markets/statistics-equities/daily/json/'
CHALLENGE = re.compile(
    r'captcha|access\s*denied|security\s*challenge|verify.{0,30}(human|browser)|'
    r'checking\s+your\s+browser|just\s+a\s+moment|cloudflare\s+ray|'
    r'アクセス.{0,8}(拒否|制限)|bot\s*challenge|auth(?:entication)?\s*'
    r'(challenge|required)|login\s*required|sign[\s-]*in\s*(required|to\s+continue)|'
    r'ロボットではない|セキュリティ.{0,8}(チェック|確認)', re.I)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode()


def safe_path(root, relative):
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError('MONITOR_REFERENCE_PATH_INVALID')
    path = root / path
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.resolve().is_relative_to(root):
        raise ValueError('MONITOR_REFERENCE_PATH_INVALID')
    return path


def digest(path):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('MONITOR_REFERENCE_SYMLINK')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        return hashlib.sha256(stream.read()).hexdigest()


def strict_json(body):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('JSON_DUPLICATE_KEY')
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError('JSON_NONFINITE')
    return json.loads(body.decode('utf-8-sig'), object_pairs_hook=pairs,
                      parse_constant=nonfinite)


@contextmanager
def _folder_fd(folder, *, create=False):
    """Traverse every directory without following links; operations use its fd."""
    path = Path(folder).absolute()
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in path.parts[1:]:
            if name in {'.', '..'}:
                raise ValueError('MONITOR_STATE_PATH_INVALID')
            if create:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def present(path):
    # Dangling links are evidence errors, never an absent/reset state.
    return os.path.lexists(path)


def read_state(path):
    path = Path(path)
    with _folder_fd(path.parent) as folder:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > 4 * 1024 * 1024:
                raise ValueError('MONITOR_STATE_FILE_INVALID')
            body = stream.read(4 * 1024 * 1024 + 1)
            after = os.fstat(stream.fileno())
        current = os.stat(path.name, dir_fd=folder, follow_symlinks=False)
        identity = lambda info: (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                                 info.st_mtime_ns, info.st_ctime_ns)
        if identity(before) != identity(after) or identity(before) != identity(current):
            raise ValueError('MONITOR_STATE_CONCURRENT_CHANGE')
    envelope = strict_json(body)
    if (not isinstance(envelope, dict) or set(envelope) != {'index', 'sha256'}
            or hashlib.sha256(canonical(envelope['index'])).hexdigest() != envelope['sha256']):
        raise ValueError('MONITOR_STATE_HASH_CONFLICT')
    return envelope['index']


def write_state(path, value):
    path = Path(path)
    payload = canonical({'index': value, 'sha256': hashlib.sha256(canonical(value)).hexdigest()})
    with _folder_fd(path.parent, create=True) as folder:
        pending = '.monitor-' + uuid4().hex
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     mode=0o600, dir_fd=folder)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                info = os.stat(path.name, dir_fd=folder, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError('MONITOR_STATE_PATH_INVALID')
            except FileNotFoundError:
                pass
            os.replace(pending, path.name, src_dir_fd=folder, dst_dir_fd=folder)
            os.fsync(folder)
        finally:
            if pending in os.listdir(folder):
                os.unlink(pending, dir_fd=folder)
                os.fsync(folder)


@contextmanager
def locked(folder):
    with _folder_fd(folder, create=True) as directory:
        fd = os.open('.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                     mode=0o600, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('MONITOR_LOCK_INVALID')
            fcntl.flock(fd, fcntl.LOCK_EX)
            current = os.stat('.lock', dir_fd=directory, follow_symlinks=False)
            if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                raise ValueError('MONITOR_LOCK_REPLACED')
            yield
        finally:
            os.close(fd)


def official_url(value, *, json_only=False):
    if not isinstance(value, str) or re.search(r'[\s\\\x00-\x1f\x7f]', unquote(value)):
        raise ValueError('UNSAFE_URL')
    p = urlsplit(value)
    if (p.scheme != 'https' or p.hostname not in {'www.jpx.co.jp', 'jpx.co.jp'}
            or p.port not in (None, 443) or p.username or p.password or p.fragment
            or not p.path.startswith('/') or (json_only and not p.path.endswith('.json'))):
        raise ValueError('OFFICIAL_SOURCE_IDENTITY_FAILURE')
    return value


def calendar_window(root, target=None, *, now=None):
    """Select the next eligible window from the hash-validated JPX calendar.

    An explicit target pins an already armed window, including late bookkeeping;
    it never advances that window to another session after a restart.
    """
    root = Path(root).resolve()
    rules = strict_json((root / 'config/forward_trial_execution_policy.json').read_bytes())
    path = safe_path(root, rules['calendar'])
    if digest(path) != rules['calendar_sha256']:
        raise ValueError('VALIDATED_CALENDAR_HASH_MISMATCH')
    days = strict_json(path.read_bytes())['jp_sessions']
    if not days or days != sorted(set(days)):
        raise ValueError('VALIDATED_CALENDAR_INVALID')
    for day in days:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError('VALIDATED_CALENDAR_INVALID')
    if target is None:
        now = now or datetime.now(JST)
        if now.tzinfo is None:
            raise ValueError('TIMEZONE_REQUIRED')
        prohibited = not_run.sessions(root, rules)
        target = next((day for day in days if day not in prohibited and
                       now < datetime.fromisoformat(day + 'T08:45:00+09:00')), None)
    if target not in days or days.index(target) == 0:
        raise ValueError('VALIDATED_CALENDAR_EXHAUSTED_OR_TARGET_INVALID')
    return dict(target_session=target, previous_session=days[days.index(target) - 1],
                deadline=target + 'T08:45:00+09:00')


def configure_window(root, target=None, *, now=None):
    """Process-start configuration only; CLI services each pin one window."""
    global SESSION, TARGET, DEADLINE, UNIT, ABSENT, DETECTED, TERMINAL
    window = calendar_window(root, target, now=now)
    SESSION, TARGET = window['previous_session'], window['target_session']
    DEADLINE = datetime.fromisoformat(window['deadline'])
    UNIT = 'jpx-publication-' + SESSION.replace('-', '')
    ABSENT = 'JPX_' + SESSION.replace('-', '_') + '_NOT_YET_AVAILABLE'
    DETECTED = 'JPX_' + SESSION.replace('-', '_') + '_PUBLICATION_DETECTED'
    TERMINAL = {DETECTED, EXPIRED, STOP}
    return window


def verify_context(root, folder):
    context = read_state(folder / 'context.json')
    if context['target_session'] != SESSION or datetime.fromisoformat(context['deadline']) != DEADLINE:
        raise ValueError('MONITOR_TARGET_CONTEXT_CONFLICT')
    for relative, expected in context['protected_references'].items():
        path = safe_path(root, relative)
        if digest(path) != expected:
            raise ValueError('MONITOR_PROTECTED_CONTEXT_CHANGED:' + relative)
    rules = strict_json((root / 'config/forward_trial_execution_policy.json').read_bytes())
    calendar_path = safe_path(root, rules['calendar'])
    if digest(calendar_path) != rules['calendar_sha256']:
        raise ValueError('VALIDATED_CALENDAR_HASH_MISMATCH')
    days = strict_json(calendar_path.read_bytes())['jp_sessions']
    if days != sorted(set(days)) or SESSION not in days or TARGET not in days:
        raise ValueError('MONITOR_CALENDAR_CONFLICT')
    window_v2 = context.get('schema_version') == WINDOW_SCHEMA
    if days[days.index(SESSION) + 1] != TARGET:
        raise ValueError('MONITOR_TARGET_POLICY_CONFLICT')
    if window_v2:
        if (context.get('forecast_target_session') != TARGET
                or TARGET in not_run.sessions(root, rules)):
            raise ValueError('MONITOR_TARGET_POLICY_CONFLICT')
    elif SESSION not in not_run.sessions(root, rules):
        raise ValueError('MONITOR_TARGET_POLICY_CONFLICT')
    branch = subprocess.check_output(['git', '-C', str(root), 'branch', '--show-current'], text=True).strip()
    if branch != BRANCH:
        raise ValueError('MONITOR_BRANCH_CONFLICT')
    armed = read_state(folder / 'armed.json')
    if (armed.get('target_session') != SESSION
            or datetime.fromisoformat(armed['deadline']) != DEADLINE
            or not re.fullmatch(r'[0-9a-f]{40}', armed['code_commit'])
            or not set(CODE_PATHS).issubset(armed['code_hashes'])):
        raise ValueError('MONITOR_ARMED_IDENTITY_INVALID')
    for relative, expected in armed['code_hashes'].items():
        if digest(safe_path(root, relative)) != expected:
            raise ValueError('MONITOR_CODE_CHANGED')
    if (context['discovery_reference'] not in context['protected_references']
            or 'config/forward_trial_execution_policy.json' not in context['protected_references']):
        raise ValueError('MONITOR_REQUIRED_PROTECTION_MISSING')
    source = strict_json(safe_path(root, context['discovery_reference']).read_bytes())
    if ((source['session'][:7] != SESSION[:7] if window_v2 else source['session'] != SESSION)
            or source['pdf_requests'] != 0
            or source['schema_version'] != 'jpx-json-discovery-v1'
            or source['monthly_json_reference_url'] != context['monthly_url']):
        raise ValueError('MONITOR_DISCOVERY_IDENTITY_FAILURE')
    if window_v2 and not urlsplit(context['monthly_url']).path.startswith(MONTHLY_FAMILY):
        raise ValueError('MONITOR_DISCOVERY_IDENTITY_FAILURE')
    return official_url(context['monthly_url'], json_only=True), armed['code_commit']


def get_monthly(url):
    """Exactly one GET, no redirects, retries, cookies, or PDF capability."""
    official_url(url, json_only=True)
    p = urlsplit(url)
    conn = http.client.HTTPSConnection(p.hostname, timeout=25, context=ssl.create_default_context())
    try:
        conn.request('GET', p.path + ('?' + p.query if p.query else ''), headers={
            'User-Agent': 'JPX-Publication-Monitor/1.0', 'Accept-Encoding': 'identity'})
        response = conn.getresponse()
        pairs = response.getheaders()
        if len({k.lower() for k, _ in pairs}) != len(pairs):
            raise ValueError('AMBIGUOUS_HTTP_HEADER')
        headers = {k.lower(): v for k, v in pairs}
        # Restriction/redirect status wins even when reading the body would fail.
        if response.status in (401, 403, 429) or 300 <= response.status < 400:
            return response.status, headers, b''
        body = response.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            raise ValueError('RESPONSE_TOO_LARGE')
        return response.status, headers, body
    finally:
        conn.close()


def publication(status, headers, body, url):
    if status in (401, 403, 429):
        raise ValueError({401: 'AUTHENTICATION_CHALLENGE', 403: 'HTTP_403', 429: 'RATE_LIMIT'}[status])
    if 300 <= status < 400:
        raise ValueError('UNEXPECTED_REDIRECT')
    if CHALLENGE.search(body[:65536].decode('utf-8', errors='replace')):
        raise ValueError('SECURITY_CHALLENGE')
    kind = headers.get('content-type', '').split(';')[0].strip().lower()
    if status != 200 or kind not in {'application/json', 'text/json', 'binary/octet-stream', 'application/octet-stream'}:
        raise ValueError('UNEXPECTED_JSON_RESPONSE')
    official_url(url, json_only=True)
    data = strict_json(body)
    if not isinstance(data, dict) or not isinstance(data.get('TableDatas'), list):
        raise ValueError('JSON_SCHEMA_MISMATCH')
    matches = []
    for row in data['TableDatas']:
        if not isinstance(row, dict) or not isinstance(row.get('TradeDate'), str) or 'Stocks' not in row:
            raise ValueError('MONTHLY_JSON_SCHEMA_MISMATCH')
        trade = row['TradeDate']
        if not re.fullmatch(r'\d{8}', trade):
            raise ValueError('MONTHLY_JSON_DATE_INVALID')
        datetime.strptime(trade, '%Y%m%d')
        if row['Stocks'] is not None and not isinstance(row['Stocks'], str):
            raise ValueError('MONTHLY_JSON_STOCKS_INVALID')
        if trade == SESSION.replace('-', ''):
            matches.append(row)
    if len(matches) > 1:
        raise ValueError('MONTHLY_JSON_SESSION_AMBIGUOUS')
    if not matches or matches[0]['Stocks'] in (None, '', '-'):
        return None
    href = official_url(urljoin('https://www.jpx.co.jp/markets/statistics-equities/daily/', matches[0]['Stocks']))
    if not urlsplit(href).path.lower().endswith('.pdf'):
        raise ValueError('PDF_LINK_UNKNOWN')
    return {'trade_date': matches[0]['TradeDate'], 'exact_stocks_href': matches[0]['Stocks'],
            'resolved_href': href, 'metadata_sha256': hashlib.sha256(canonical(matches[0])).hexdigest()}


def completed_history_in_time(root, deadline):
    """Read only a separate Task 10C-6c completion receipt; never run that task.

    Task 10C-6c must seal its PASS result and bind the full validated history,
    official registration and preflight source hashes. Detection alone is not
    usable history. Without this independently completed evidence fail closed.
    """
    path = root / 'state/jpx_publication_monitor' / SESSION / 'task10c6c_completion.json'
    if not present(path):
        return False
    try:
        return _completed_history_receipt(root, deadline, read_state(path))
    except (ValueError, OSError, KeyError, TypeError):
        return False


def _completed_history_receipt(root, deadline, receipt):
    if (receipt.get('target_session') != TARGET or receipt.get('previous_session') != SESSION
            or receipt.get('history_ready') is not True or receipt.get('official_status') != 'OFFICIAL_VALIDATED'
            or receipt.get('task') != '10C-6c' or receipt.get('status') != 'PASS'
            or not receipt.get('source_hashes')):
        return False
    stamp = datetime.fromisoformat(receipt['completed_at_jst'])
    if stamp.tzinfo is None or stamp >= deadline:
        return False
    rules = strict_json((root / 'config/forward_trial_execution_policy.json').read_bytes())
    required = {rules['historical_snapshot'], 'state/runtime/recent_sessions.json',
                'data/outcome_input/manual_jpx/retention_manifest.json'}
    refs = receipt['source_hashes']
    if not required.issubset(refs) or not any('preflight' in Path(p).name for p in refs):
        return False
    for relative, expected in receipt['source_hashes'].items():
        source = (root / relative).resolve()
        if not source.is_relative_to(root) or digest(source) != expected:
            return False
    recent = strict_json((root / 'state/runtime/recent_sessions.json').read_bytes())
    if recent['sessions'].get(SESSION) != 'OFFICIAL_VALIDATED':
        return False
    return True


def record_not_run(root, recorded_at):
    rules = strict_json((root / 'config/forward_trial_execution_policy.json').read_bytes())
    context_path = root / 'state/jpx_publication_monitor' / SESSION / 'context.json'
    window_v2 = present(context_path) and read_state(context_path).get('schema_version') == WINDOW_SCHEMA
    if window_v2:
        if calendar_window(root, TARGET)['previous_session'] != SESSION:
            raise ValueError('PERMANENT_NOT_RUN_POLICY_CONFLICT')
        # Fail closed if manual execution has already frozen this target. The
        # metadata-only monitor never validates or rewrites Forecast bundles.
        primary = strict_json((root / 'config/primary_forward_trial_v1.json').read_bytes())
        store = safe_path(root, primary['forecast_store'])
        if store.exists():
            for bundle in store.iterdir():
                if bundle.name.startswith('.') or not bundle.is_dir():
                    continue
                path = safe_path(root, str((bundle / 'receipt.json').relative_to(root)))
                receipt = strict_json(path.read_bytes())
                if receipt['target_session'] == TARGET:
                    raise ValueError('FROZEN_FORECAST_PRESENT_NOT_RUN_FORBIDDEN')
    elif SESSION not in not_run.sessions(root, rules):
        raise ValueError('PERMANENT_NOT_RUN_POLICY_CONFLICT')
    # Already tracked exclusions remain authoritative; never rewrite old policy.
    not_run.record(root, TARGET, reason='FORECAST_DEADLINE_EXPIRED_WITHOUT_VALIDATED_HISTORY',
                   source='JPX_PUBLICATION_MONITOR:' + SESSION, recorded_at=recorded_at)


def _retry_state(folder):
    path = folder / 'deadline_retry.json'
    witness = folder / 'deadline_reserved.json'
    if present(witness) and read_state(witness) != dict(target_session=TARGET,deadline=DEADLINE.isoformat()):
        raise ValueError('DEADLINE_RETRY_WITNESS_INVALID')
    if not present(path):
        if present(witness):
            raise ValueError('DEADLINE_RETRY_LEDGER_MISSING')
        for name in ('STOP.json', 'latest.json'):
            if present(folder / name):
                old = read_state(folder / name)
                if 'finalization_attempts' in old:
                    raise ValueError('DEADLINE_RETRY_LEDGER_MISSING')
        return dict(target_session=TARGET, deadline=DEADLINE.isoformat(), attempts=0,
                    last_attempt_at=None, stopped=False, completion=None)
    state = read_state(path)
    if not present(witness):
        raise ValueError('DEADLINE_RETRY_WITNESS_MISSING')
    if (set(state) not in ({'target_session', 'deadline', 'attempts', 'last_attempt_at', 'stopped'},
                          {'target_session', 'deadline', 'attempts', 'last_attempt_at', 'stopped', 'completion'})
            or state['target_session'] != TARGET or state['deadline'] != DEADLINE.isoformat()
            or type(state['attempts']) is not int or not 1 <= state['attempts'] <= MAX_FINALIZATION_ATTEMPTS
            or type(state['stopped']) is not bool
            or datetime.fromisoformat(state['last_attempt_at']).tzinfo is None):
        raise ValueError('DEADLINE_RETRY_LEDGER_INVALID')
    completion = state.setdefault('completion', None)
    if completion is not None and (not state['stopped'] or not isinstance(completion, dict)
            or completion.get('target_session') != SESSION or completion.get('deadline_expired') is not True
            or completion.get('result_state') not in TERMINAL
            or completion.get('finalization_attempts') != state['attempts']
            or completion.get('checked_at_jst') != state['last_attempt_at']):
        raise ValueError('DEADLINE_RETRY_COMPLETION_INVALID')
    return state


def finalize_deadline(root, folder, result, history_check):
    result['deadline_expired'] = True
    try:
        retry = _retry_state(folder)
        now = datetime.fromisoformat(result['checked_at_jst'])
        if retry['last_attempt_at'] and now < datetime.fromisoformat(retry['last_attempt_at']):
            raise ValueError('DEADLINE_FINALIZATION_CLOCK_ROLLBACK')
        if retry['completion'] is not None:
            result = retry['completion']  # Recover interrupted latest/STOP publication.
            write_state(folder / 'latest.json', result)
            if result['result_state'] == STOP or present(folder / 'STOP.json'):
                write_state(folder / 'STOP.json', result)
            return result
        if retry['stopped'] or retry['attempts'] >= MAX_FINALIZATION_ATTEMPTS:
            raise ValueError('DEADLINE_FINALIZATION_RETRY_EXHAUSTED')
        if retry['last_attempt_at'] and now < (datetime.fromisoformat(retry['last_attempt_at'])
                                               + timedelta(seconds=FINALIZATION_RETRY_SECONDS)):
            result.update(result_state=STOP, deadline_expired=False, finalization_exhausted=False,
                          finalization_attempts=retry['attempts'],
                          stop_reason='DEADLINE_FINALIZATION_RETRY_NOT_YET_DUE')
            write_state(folder / 'latest.json', result)
            write_state(folder / 'STOP.json', result)
            return result
        retry.update(attempts=retry['attempts'] + 1, last_attempt_at=now.isoformat())
        # Count before any work: a crash consumes an attempt rather than resetting it.
        if not present(folder / 'deadline_reserved.json'):
            write_state(folder / 'deadline_reserved.json',dict(target_session=TARGET,deadline=DEADLINE.isoformat()))
        write_state(folder / 'deadline_retry.json', retry)
        result['finalization_attempts'] = retry['attempts']
        if not history_check(root, DEADLINE):
            record_not_run(root, result['checked_at_jst'])
            result.update(result_state=EXPIRED, stop_reason=TARGET + '_FORECAST_PERMANENTLY_NOT_RUN')
        else:
            result.update(result_state=DETECTED, publication_found=True,
                          stop_reason='DEADLINE_REACHED_HISTORY_COMPLETED_NO_FORECAST')
        retry['stopped'] = True
        retry['completion'] = dict(result)
        write_state(folder / 'deadline_retry.json', retry)
    except (ValueError, OSError, KeyError, TypeError) as error:
        result.update(result_state=STOP, stop_reason='DEADLINE_FINALIZATION_FAILED:' + str(error))
        # Only failures with a valid reservation have any retries left.
        try:
            retry = _retry_state(folder)
            exhausted = (retry['stopped'] or retry['attempts'] >= MAX_FINALIZATION_ATTEMPTS
                         or (retry['last_attempt_at'] and
                             datetime.fromisoformat(result['checked_at_jst']) <
                             datetime.fromisoformat(retry['last_attempt_at'])))
            retry['stopped'] = bool(exhausted)
            write_state(folder / 'deadline_retry.json', retry)
            result['finalization_attempts'] = retry['attempts']
        except (ValueError, OSError, KeyError, TypeError):
            exhausted = True
        result.update(deadline_expired=bool(exhausted), finalization_exhausted=bool(exhausted))
    write_state(folder / 'latest.json', result)
    if result['result_state'] == STOP or present(folder / 'STOP.json'):
        write_state(folder / 'STOP.json', result)
    return result


def stop_finalization(root=ROOT, *, now=None):
    """Service failure endpoint: latch durable STOP; no context or network calls."""
    now = now or datetime.now(JST)
    if now.tzinfo is None:
        raise ValueError('TIMEZONE_REQUIRED')
    folder = Path(root) / 'state/jpx_publication_monitor' / SESSION
    result = dict(checked_at_jst=now.isoformat(), target_session=SESSION,
                  source_identity=None, http_status=None, result_state=STOP,
                  publication_found=False, deadline_expired=True, code_commit=None,
                  finalization_exhausted=True, stop_reason='DEADLINE_SERVICE_FAILED_REVIEW_REQUIRED')
    with locked(folder):
        write_state(folder / 'STOP.json', result)
        write_state(folder / 'latest.json', result)
    return result


def run_deadline_finalization(root=ROOT, *, sleep=time.sleep):
    """One process, at most three attempts, persisted across process restarts."""
    for attempt in range(MAX_FINALIZATION_ATTEMPTS):
        result = check_once(root, finalize_only=True)
        if result['deadline_expired']:
            return result
        if attempt + 1 < MAX_FINALIZATION_ATTEMPTS:
            sleep(FINALIZATION_RETRY_SECONDS)
    return stop_finalization(root)


def check_once(root=ROOT, *, now=None, fetch=get_monthly, context_check=verify_context,
               history_check=completed_history_in_time, finalize_only=False, final_check=False):
    root = Path(root).resolve()
    live_clock = now is None
    now = now or datetime.now(JST)
    if now.tzinfo is None:
        raise ValueError('TIMEZONE_REQUIRED')
    now = now.astimezone(JST)
    folder = root / 'state/jpx_publication_monitor' / SESSION
    with locked(folder):
        old = None
        try:
            if present(folder / 'STOP.json'):
                old = read_state(folder / 'STOP.json')
            elif present(folder / 'latest.json'):
                old = read_state(folder / 'latest.json')
            if old is not None and (old['target_session'] != SESSION or old['result_state'] not in TERMINAL | {ABSENT, PENDING}):
                raise ValueError('MONITOR_STATE_CONTEXT_CONFLICT')
        except (ValueError, OSError, KeyError, TypeError) as error:
            old = dict(checked_at_jst=now.isoformat(), target_session=SESSION,
                       source_identity=None, http_status=None, result_state=STOP,
                       publication_found=False, deadline_expired=False, code_commit=None,
                       stop_reason='MONITOR_STATE_INTEGRITY_FAILURE:' + type(error).__name__)
            write_state(folder / 'STOP.json', old)
        result = dict(checked_at_jst=now.isoformat(), target_session=SESSION,
                      source_identity=(old or {}).get('source_identity'), http_status=None,
                      result_state=ABSENT, publication_found=(old or {}).get('publication_found', False),
                      deadline_expired=now >= DEADLINE, stop_reason=None,
                      code_commit=(old or {}).get('code_commit'))
        if finalize_only and now < DEADLINE:
            result.update(result_state=STOP, deadline_expired=True, finalization_exhausted=True,
                          stop_reason='DEADLINE_FINALIZATION_CLOCK_ROLLBACK')
            write_state(folder / 'STOP.json', result)
            write_state(folder / 'latest.json', result)
            return result
        if now >= DEADLINE:
            if old and old.get('deadline_expired') and old['result_state'] in TERMINAL:
                if not present(folder / 'deadline_retry.json'):
                    return old  # Original terminal evidence remains readable.
                try:
                    retry = _retry_state(folder)
                    if retry['completion'] == old or (retry['stopped'] and
                            retry['completion'] is None and old.get('finalization_exhausted')):
                        return old
                except (ValueError, OSError, KeyError, TypeError):
                    pass  # The finalizer durably closes a corrupt retry ledger.
            return finalize_deadline(root, folder, result, history_check)
        if old and old['result_state'] in TERMINAL:
            return old
        try:
            url, commit = context_check(root, folder)
            result.update(source_identity={'monthly_json_url': url}, code_commit=commit)
            if old and old['result_state'] == PENDING:
                raise ValueError('INTERRUPTED_CHECK_REVIEW_REQUIRED')
            if final_check:
                if not DEADLINE - FINAL_CHECK_LEAD <= now <= DEADLINE - FINAL_CHECK_LATEST_LEAD:
                    raise ValueError('FINAL_CHECK_OUTSIDE_APPROVED_WINDOW')
                if old and old.get('final_publication_check') and not present(folder / 'final_check_reserved.json'):
                    raise ValueError('FINAL_CHECK_RESERVATION_MISSING')
                if present(folder / 'final_check_reserved.json'):
                    reservation = read_state(folder / 'final_check_reserved.json')
                    stamp = datetime.fromisoformat(reservation['reserved_at'])
                    if (reservation.get('target_session') != TARGET
                            or reservation.get('deadline') != DEADLINE.isoformat()
                            or stamp.tzinfo is None or now < stamp or old is None):
                        raise ValueError('FINAL_CHECK_RESERVATION_INVALID')
                    return old  # A completed final check cannot be repeated.
            if now.date() < date.fromisoformat(SESSION):
                raise ValueError('MONITOR_WINDOW_NOT_STARTED')
            if old:
                checked = datetime.fromisoformat(old['checked_at_jst'])
                if checked.tzinfo is None or now < checked:
                    raise ValueError('MONITOR_CLOCK_ROLLBACK')
                if now - checked < timedelta(hours=1) and not final_check:
                    return old  # Repeated manual/scheduled calls cannot increase cadence.
            if final_check:
                write_state(folder / 'final_check_reserved.json', dict(
                    target_session=TARGET, deadline=DEADLINE.isoformat(), reserved_at=now.isoformat()))
                result['final_publication_check'] = True
            result['result_state'] = PENDING
            write_state(folder / 'latest.json', result)  # Durable reservation before GET.
            if live_clock and (finalized_at := datetime.now(JST)) >= DEADLINE:
                result['checked_at_jst'] = finalized_at.isoformat()
                return finalize_deadline(root, folder, result, history_check)
            status, headers, body = fetch(url)
            result['http_status'] = status
            link = publication(status, headers, body, url)
            result['source_identity']['response_sha256'] = hashlib.sha256(body).hexdigest()
            if link:
                result['source_identity']['publication_metadata'] = link
            result.update(result_state=DETECTED if link else ABSENT, publication_found=bool(link))
        except Exception as error:
            reason = str(error) if isinstance(error, ValueError) else type(error).__name__
            result.update(result_state=STOP, stop_reason=reason)
        write_state(folder / 'latest.json', result)
        if result['result_state'] == STOP:
            write_state(folder / 'STOP.json', result)
        # A GET started before cutoff may finish late; it never grants readiness.
        if live_clock and (finalized_at := datetime.now(JST)) >= DEADLINE:
            result['checked_at_jst'] = finalized_at.isoformat()
            return finalize_deadline(root, folder, result, history_check)
        return result


def systemd_env():
    env = os.environ.copy()
    runtime = '/run/user/' + str(os.getuid())
    env.update(XDG_RUNTIME_DIR=runtime, DBUS_SESSION_BUS_ADDRESS='unix:path=' + runtime + '/bus')
    return env


def disable_timers(*, all_timers=False):
    timers = [UNIT + '.timer', UNIT + '-final.timer']
    if all_timers:
        timers.append(UNIT + '-deadline.timer')
    subprocess.run(['systemctl', '--user', 'disable', '--now', *timers],
                   env=systemd_env(), check=True, timeout=15)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', required=True)
    parser.add_argument('--target-session', help='Calendar-validated pinned Forecast target')
    parser.add_argument('--deadline', required=True)
    parser.add_argument('--once', action='store_true', required=True)
    parser.add_argument('--scheduled', action='store_true')
    parser.add_argument('--finalize', action='store_true', help='Bounded deadline bookkeeping only; no network')
    parser.add_argument('--final-check', action='store_true', help='One final monthly JSON check at 08:35 JST')
    parser.add_argument('--finalization-failed', action='store_true', help='Latch failed deadline service STOP; no network')
    args = parser.parse_args()
    if args.target_session:
        configure_window(ROOT, args.target_session)
    if args.session != SESSION:
        parser.error('Session must be the validated previous JPX session')
    if sum((args.finalize, args.finalization_failed, args.final_check)) > 1:
        parser.error('Choose exactly one monitor operation')
    if datetime.fromisoformat(args.deadline) != DEADLINE:
        parser.error('Only the approved aware deadline is accepted')
    if args.finalize and datetime.now(JST) < DEADLINE:
        parser.error('Deadline finalization cannot run before cutoff')
    result = (stop_finalization() if args.finalization_failed else
              run_deadline_finalization() if args.finalize else
              check_once(final_check=True) if args.final_check else check_once())
    # A request that crosses the cutoff cannot count as publication in time.
    if not args.finalize and not args.finalization_failed and datetime.now(JST) >= DEADLINE and not result['deadline_expired']:
        result = check_once()
    print(json.dumps(result, ensure_ascii=False))
    if args.scheduled and result['result_state'] in TERMINAL:
        disable_timers(all_timers=result['deadline_expired'])
        if result['result_state'] == DETECTED and not result['deadline_expired']:
            try:
                subprocess.run(['notify-send', 'JPX ' + SESSION + ' 公開を検出',
                                '監視停止。Task 10C-6c の公式取得・検証・履歴刷新・preflight が必要です。Forecast は未実行。'],
                               env=systemd_env(), timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass  # Journal and durable state still surface the detection.


if __name__ == '__main__':
    main()

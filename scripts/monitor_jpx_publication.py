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
import subprocess
import tempfile
from urllib.parse import unquote, urljoin, urlsplit
from zoneinfo import ZoneInfo

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
CHALLENGE = re.compile(
    r'captcha|access\s*denied|security\s*challenge|verify.{0,30}(human|browser)|'
    r'checking\s+your\s+browser|just\s+a\s+moment|cloudflare\s+ray|'
    r'アクセス.{0,8}(拒否|制限)|bot\s*challenge|auth(?:entication)?\s*'
    r'(challenge|required)|login\s*required|sign[\s-]*in\s*(required|to\s+continue)|'
    r'ロボットではない|セキュリティ.{0,8}(チェック|確認)', re.I)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


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


def read_state(path):
    envelope = strict_json(Path(path).read_bytes())
    if hashlib.sha256(canonical(envelope['index'])).hexdigest() != envelope['sha256']:
        raise ValueError('MONITOR_STATE_HASH_CONFLICT')
    return envelope['index']


def write_state(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical({'index': value, 'sha256': hashlib.sha256(canonical(value)).hexdigest()})
    fd, pending = tempfile.mkstemp(prefix='.monitor-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(pending):
            os.unlink(pending)


@contextmanager
def locked(folder):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / '.lock').open('a+b') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def official_url(value, *, json_only=False):
    if not isinstance(value, str) or re.search(r'[\s\\\x00-\x1f\x7f]', unquote(value)):
        raise ValueError('UNSAFE_URL')
    p = urlsplit(value)
    if (p.scheme != 'https' or p.hostname not in {'www.jpx.co.jp', 'jpx.co.jp'}
            or p.port not in (None, 443) or p.username or p.password or p.fragment
            or not p.path.startswith('/') or (json_only and not p.path.endswith('.json'))):
        raise ValueError('OFFICIAL_SOURCE_IDENTITY_FAILURE')
    return value


def verify_context(root, folder):
    context = read_state(folder / 'context.json')
    if context['target_session'] != SESSION or datetime.fromisoformat(context['deadline']) != DEADLINE:
        raise ValueError('MONITOR_TARGET_CONTEXT_CONFLICT')
    for relative, expected in context['protected_references'].items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or digest(path) != expected:
            raise ValueError('MONITOR_PROTECTED_CONTEXT_CHANGED:' + relative)
    rules = strict_json((root / 'config/forward_trial_execution_policy.json').read_bytes())
    calendar_path = root / rules['calendar']
    if digest(calendar_path) != rules['calendar_sha256']:
        raise ValueError('VALIDATED_CALENDAR_HASH_MISMATCH')
    days = strict_json(calendar_path.read_bytes())['jp_sessions']
    if days != sorted(set(days)) or SESSION not in days or TARGET not in days:
        raise ValueError('MONITOR_CALENDAR_CONFLICT')
    if days[days.index(SESSION) + 1] != TARGET or SESSION not in rules['permanently_not_run_sessions']:
        raise ValueError('MONITOR_TARGET_POLICY_CONFLICT')
    branch = subprocess.check_output(['git', '-C', str(root), 'branch', '--show-current'], text=True).strip()
    if branch != BRANCH:
        raise ValueError('MONITOR_BRANCH_CONFLICT')
    armed = read_state(folder / 'armed.json')
    head = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    if head != armed['code_commit']:
        raise ValueError('MONITOR_COMMIT_CHANGED')
    for relative, expected in armed['code_hashes'].items():
        if digest(root / relative) != expected:
            raise ValueError('MONITOR_CODE_CHANGED')
    source = strict_json((root / context['discovery_reference']).read_bytes())
    if (source['session'] != SESSION or source['pdf_requests'] != 0
            or source['schema_version'] != 'jpx-json-discovery-v1'
            or source['monthly_json_reference_url'] != context['monthly_url']):
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
    if not path.exists():
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


def record_not_run(root):
    """Append only the missed target to the existing permanent exclusion policy.

    No Forecast runtime, persistence contract, or model settings change. The
    caller holds the monitor lock; compare bytes to detect concurrent edits.
    """
    path = root / 'config/forward_trial_execution_policy.json'
    before = path.read_bytes()
    rules = strict_json(before)
    sessions = rules['permanently_not_run_sessions']
    if SESSION not in sessions:
        raise ValueError('PERMANENT_NOT_RUN_POLICY_CONFLICT')
    if TARGET in sessions:
        return
    rules['permanently_not_run_sessions'] = sorted(set(sessions + [TARGET]))
    fd, pending = tempfile.mkstemp(prefix='.monitor-policy-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(json.dumps(rules, ensure_ascii=False, indent=2).encode() + b'\n')
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_bytes() != before:
            raise ValueError('CONCURRENT_POLICY_CHANGE')
        os.replace(pending, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(pending):
            os.unlink(pending)


def finalize_deadline(root, folder, result, history_check):
    result['deadline_expired'] = True
    try:
        if not history_check(root, DEADLINE):
            record_not_run(root)
            result.update(result_state=EXPIRED, stop_reason=TARGET + '_FORECAST_PERMANENTLY_NOT_RUN')
        else:
            result.update(result_state=DETECTED, publication_found=True,
                          stop_reason='DEADLINE_REACHED_HISTORY_COMPLETED_NO_FORECAST')
    except (ValueError, OSError, KeyError, TypeError) as error:
        result.update(result_state=STOP, stop_reason='DEADLINE_FINALIZATION_FAILED:' + type(error).__name__)
        result['deadline_expired'] = False  # Retry bookkeeping only, never HTTP.
    write_state(folder / 'latest.json', result)
    if (folder / 'STOP.json').exists():
        write_state(folder / 'STOP.json', result)
    return result


def check_once(root=ROOT, *, now=None, fetch=get_monthly, context_check=verify_context,
               history_check=completed_history_in_time):
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
            if (folder / 'STOP.json').exists():
                old = read_state(folder / 'STOP.json')
            elif (folder / 'latest.json').exists():
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
        if now >= DEADLINE:
            if old and old.get('deadline_expired') and old['result_state'] in TERMINAL:
                return old
            return finalize_deadline(root, folder, result, history_check)
        if old and old['result_state'] in TERMINAL:
            return old
        try:
            url, commit = context_check(root, folder)
            result.update(source_identity={'monthly_json_url': url}, code_commit=commit)
            if now.date() < date.fromisoformat(SESSION):
                raise ValueError('MONITOR_WINDOW_NOT_STARTED')
            if old and old['result_state'] == PENDING:
                raise ValueError('INTERRUPTED_CHECK_REVIEW_REQUIRED')
            if old:
                checked = datetime.fromisoformat(old['checked_at_jst'])
                if checked.tzinfo is None or now < checked:
                    raise ValueError('MONITOR_CLOCK_ROLLBACK')
                if now - checked < timedelta(hours=1):
                    return old  # Repeated manual/scheduled calls cannot increase cadence.
            result['result_state'] = PENDING
            write_state(folder / 'latest.json', result)  # Durable reservation before GET.
            if live_clock and datetime.now(JST) >= DEADLINE:
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
        if live_clock and datetime.now(JST) >= DEADLINE:
            return finalize_deadline(root, folder, result, history_check)
        return result


def systemd_env():
    env = os.environ.copy()
    runtime = '/run/user/' + str(os.getuid())
    env.update(XDG_RUNTIME_DIR=runtime, DBUS_SESSION_BUS_ADDRESS='unix:path=' + runtime + '/bus')
    return env


def disable_timers(*, all_timers=False):
    timers = [UNIT + '.timer']
    if all_timers:
        timers.append(UNIT + '-deadline.timer')
    subprocess.run(['systemctl', '--user', 'disable', '--now', *timers],
                   env=systemd_env(), check=True, timeout=15)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', required=True, choices=[SESSION])
    parser.add_argument('--deadline', required=True)
    parser.add_argument('--once', action='store_true', required=True)
    parser.add_argument('--scheduled', action='store_true')
    parser.add_argument('--finalize', action='store_true', help='Deadline bookkeeping only; no network')
    args = parser.parse_args()
    if datetime.fromisoformat(args.deadline) != DEADLINE:
        parser.error('Only the approved aware deadline is accepted')
    if args.finalize and datetime.now(JST) < DEADLINE:
        parser.error('Deadline finalization cannot run before cutoff')
    result = check_once()
    # A request that crosses the cutoff cannot count as publication in time.
    if not args.finalize and datetime.now(JST) >= DEADLINE and not result['deadline_expired']:
        result = check_once()
    print(json.dumps(result, ensure_ascii=False))
    if args.finalize and not result['deadline_expired']:
        raise RuntimeError('DEADLINE_BOOKKEEPING_REQUIRES_RETRY_NO_HTTP')
    if args.scheduled and result['result_state'] in TERMINAL:
        disable_timers(all_timers=result['deadline_expired'])
        if result['result_state'] == DETECTED and not result['deadline_expired']:
            try:
                subprocess.run(['notify-send', 'JPX 10/13 公開を検出',
                                '監視停止。Task 10C-6c の公式取得・検証・履歴刷新・preflight が必要です。Forecast は未実行。'],
                               env=systemd_env(), timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass  # Journal and durable state still surface the detection.


if __name__ == '__main__':
    main()

"""Explicit one-session historical acquisition, never Fresh/Forecast/Outcome.

No CLI, scheduler, bulk operation, or implicit network transport.
Call acquire_one(root, session, transport=HTTPS()) only for an authorized live
operation. Tests inject a transport with no network. Each request is a single
GET; redirects are checked before following and bounded separately from retries.
Validated bundles remain pending explicit Manifest/Historical Bridge registration.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from io import BytesIO
import http.client
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import socket
import errno
import time
import tempfile
from email.utils import parsedate_to_datetime
from uuid import uuid4
from urllib.parse import unquote, urljoin, urlsplit
from zoneinfo import ZoneInfo

from pypdf import PdfReader
from engine import minimal_trial as core
from engine import official_gap as gap
from engine import one_session_dry_run as frozen
from providers.jp.jpx_manual import parser
from providers.jp.jpx_manual import historical_policy_v2 as lifecycle

METHOD = parser.DIRECT_METHOD
LISTING = 'https://www.jpx.co.jp/markets/statistics-equities/daily/'
HOSTS = {'www.jpx.co.jp', 'jpx.co.jp'}
STOP_STATUSES = {401: 'AUTHENTICATION_CHALLENGE', 403: 'HTTP_403', 429: 'RATE_LIMIT'}
CHALLENGE = re.compile(r'captcha|access\s*denied|security\s*challenge|'
                       r'verify.{0,30}(human|browser)|checking\s+your\s+browser|'
                       r'just\s+a\s+moment|cloudflare\s+ray|アクセス.{0,8}(拒否|制限)|'
                       r'bot\s*challenge|auth(?:entication)?\s*(challenge|required)|login\s*required|'
                       r'sign[\s-]*in\s*(required|to\s+continue)|'
                       r'ロボットではない|セキュリティ.{0,8}(チェック|確認)', re.I)


def _now():
    return datetime.now(timezone.utc)


def official_url(value):
    if not isinstance(value, str) or re.search(r'[\s\\\x00-\x1f\x7f]', unquote(value)):
        raise ValueError('UNSAFE_URL')
    try:
        p = urlsplit(value)
        valid = (p.scheme == 'https' and p.hostname in HOSTS and p.port in (None, 443)
                 and not p.username and not p.password and not p.fragment and p.path.startswith('/'))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('NON_OFFICIAL_HTTPS_URL')
    return value


def _canonical_request_url(url):
    """Resource identity only; requests still use the exact observed official URL."""
    parts = urlsplit(official_url(url))
    def escapes(value):
        def normalize(match):
            character = chr(int(match[0][1:], 16))
            return character if re.fullmatch(r'[A-Za-z0-9._~-]', character) else match[0].upper()
        return re.sub(r'%[0-9A-Fa-f]{2}', normalize, value)
    path = escapes(parts.path)
    segments = []
    for segment in path.split('/'):
        if segment == '..':
            if len(segments) > 1:
                segments.pop()
        elif segment != '.':
            segments.append(segment)
    normalized = '/'.join(segments) or '/'
    if path.endswith(('/.', '/..')) and not normalized.endswith('/'):
        normalized += '/'
    return parts._replace(netloc=parts.hostname, path=normalized, query=escapes(parts.query)).geturl()


@dataclass(frozen=True)
class Response:
    url: str
    status: int
    headers: dict
    body: bytes

    def content_type(self):
        return next((v.split(';')[0].strip().lower() for k, v in self.headers.items()
                     if k.lower() == 'content-type'), '')

    def header(self, name):
        values = [v for k, v in self.headers.items() if k.lower() == name.lower()]
        if len(values) > 1:
            raise ValueError('AMBIGUOUS_HTTP_HEADER')
        return values[0] if values else None


class HTTPS:
    """No automatic redirects, retries, cookies, proxy/session extraction."""
    def get(self, url, *, limit):
        official_url(url)
        p = urlsplit(url)
        connection = http.client.HTTPSConnection(p.hostname, port=p.port or 443,
                                                timeout=30, context=ssl.create_default_context())
        try:
            connection.request('GET', p.path + ('?' + p.query if p.query else ''),
                               headers={'User-Agent': 'JPX-Historical-Research/1.0',
                                        'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            pairs = response.getheaders()
            if len({k.lower() for k, _ in pairs}) != len(pairs):
                raise ValueError('AMBIGUOUS_HTTP_HEADER')
            try:
                body = response.read(limit + 1)
            except (OSError, http.client.HTTPException) as error:
                partial = getattr(error, 'partial', b'')
                # A body-read failure cannot erase a known restriction status.
                if response.status in (401, 403, 429):
                    return Response(url, response.status, dict(pairs), partial)
                # Return the known response context with the transport failure;
                # the central boundary classifies all received security controls.
                error.response = Response(url, response.status, dict(pairs), partial)
                raise
            if len(body) > limit and response.status not in (401, 403, 429):
                raise ValueError('RESPONSE_TOO_LARGE')
            return Response(url, response.status, dict(pairs), body)
        finally:
            connection.close()


@dataclass
class RunState:
    """One serialized catch-up run; budget covers listing, JSON and PDF together."""
    run_id: str = field(default_factory=lambda: uuid4().hex)
    retries: dict = field(default_factory=dict)
    blocked_hosts: dict = field(default_factory=dict)
    rate_limited_hosts: set = field(default_factory=set)
    exhausted: set = field(default_factory=set)
    store: Path | None = None

    def bind(self, store):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', self.run_id):
            raise ValueError('INVALID_HISTORICAL_RUN_ID')
        store = Path(store).resolve()
        if self.store is not None and self.store != store:
            raise ValueError('RUN_STORE_MISMATCH')
        self.store = store
        path = self.store / 'runs_v2' / (self.run_id + '.json')
        if path.exists():
            data = json.loads(path.read_bytes())
            if hashlib.sha256(core.canonical_bytes(data['index'])).hexdigest() != data['sha256']:
                raise ValueError('RUN_STATE_HASH_CONFLICT')
            self.retries = data['index']['retries']
            self.blocked_hosts = data['index']['blocked_hosts']
            self.rate_limited_hosts = set(data['index']['rate_limited_hosts'])
            self.exhausted = set(data['index'].get('exhausted', []))
        if (not isinstance(self.retries, dict) or any(type(count) is not int or not 0 <= count <= 2
                                                    for count in self.retries.values())):
            raise ValueError('INVALID_RETRY_BUDGET')

    def save(self):
        if self.store is None:
            return
        folder = self.store / 'runs_v2'
        folder.mkdir(exist_ok=True)
        state = dict(retries=self.retries, blocked_hosts=self.blocked_hosts,
                     rate_limited_hosts=sorted(self.rate_limited_hosts), exhausted=sorted(self.exhausted))
        lifecycle.write_current(folder / (self.run_id + '.json'), state)


@dataclass
class RequestContext:
    run: RunState = field(default_factory=RunState)
    session: str = 'REQUEST'
    folder: Path | None = None
    count: int = 0
    warnings: list = field(default_factory=list)
    stops: lifecycle.Stops | None = None
    stop_reference: dict | None = None
    request_key: str | None = None
    request_keys: set = field(default_factory=set)
    observation_day: str | None = None
    integrity_evidence: dict = field(default_factory=dict)

    def budget(self):
        """Same target budget survives run IDs; later normal JPX days are new observations."""
        if self.stops is None:
            return None
        targets = self.stops.index.setdefault('retry_targets', {})
        if self.observation_day is None:
            self.observation_day = _now().astimezone(ZoneInfo('Asia/Tokyo')).date().isoformat()
        day = self.observation_day  # Never reset a running operation at midnight.
        value = targets.get(self.session)
        if value is None or value['day'] != day and not value.get('in_flight'):
            value = dict(day=day, retry_count=0, exhausted=False, in_flight=False, requests={})
            targets[self.session] = value
        count = value['retry_count']
        if type(count) is not int or not 0 <= count <= 2:
            raise ValueError('INVALID_RETRY_BUDGET')
        self.run.retries[self.session] = max(self.run.retries.get(self.session, 0), count)
        return value

    def before_request(self, url, role):
        if self.run.blocked_hosts:
            raise ValueError('HARD_STOP_ACTIVE:' + next(iter(self.run.blocked_hosts.values())))
        if self.session in self.run.exhausted:
            raise ValueError('RETRY_EXHAUSTED')
        if self.stops is None:
            return
        self.stops.gate(self.session)
        budget = self.budget()
        if budget['exhausted']:
            raise ValueError('RETRY_EXHAUSTED')
        if budget['in_flight']:
            raise ValueError('RETRY_INTERRUPTED_REVIEW_REQUIRED')
        # Older summaries cannot prove how many attempts an unfinished operation
        # already made. Preserve their fail-closed review path, not a fresh budget.
        if 'requests' not in budget and budget.get('url'):
            raise ValueError('RETRY_INTERRUPTED_REVIEW_REQUIRED')
        canonical_url = _canonical_request_url(url)
        identity = dict(session=self.session, url=canonical_url, role=role)
        key = hashlib.sha256(core.canonical_bytes(identity)).hexdigest()
        requests = budget.setdefault('requests', {})
        operation = requests.setdefault(key, dict(identity, attempts_started=0, state='PENDING'))
        count = operation['attempts_started']
        if type(count) is not int or not 0 <= count <= 3:
            raise ValueError('INVALID_RETRY_BUDGET')
        if count >= 3:
            raise ValueError('RETRY_EXHAUSTED')
        deadlines = [value for value in (budget.get('retry_not_before'),
                     self.stops.index.get('host_retry_not_before')) if value]
        deadline = max(deadlines) if deadlines else None
        if deadline:
            delay = max(0, (datetime.fromisoformat(deadline) - _now()).total_seconds())
            if delay:
                time.sleep(delay)
        budget.update(in_flight=True, url=url, run_id=self.run.run_id)
        operation.update(attempts_started=count + 1, state='IN_FLIGHT',
                         last_attempt=dict(started_at=_now().isoformat(), run_id=self.run.run_id))
        self.request_key = key
        self.request_keys.add(key)
        self.stops.save()  # Unknown interrupted requests cannot gain a new initial attempt.

    def completed_request(self, metadata=None):
        if self.stops is not None:
            budget = self.budget()
            budget.update(in_flight=False)
            budget.pop('retry_not_before', None)
            if self.request_key:
                operation = budget['requests'][self.request_key]
                operation.update(state='RESPONSE_RECEIVED', last_result=metadata)
            self.stops.save()

    def finalize_requests(self, result_path):
        """Full operation completion is distinct from receiving a 200 response."""
        if self.stops is not None and self.request_keys:
            result = lifecycle.reference(result_path)
            for key in self.request_keys:
                self.budget()['requests'][key].update(state='COMPLETED', result_reference=result)
            self.stops.save()

    def commit_hard_failure(self, reason, *, evidence, refs=(), last_success=None, previous=None):
        """Safety state must not depend on completing a detailed failure log."""
        if (self.stops is None or self.stop_reference is not None
                or reason in ('RETRY_EXHAUSTED', 'RETRY_INTERRUPTED_REVIEW_REQUIRED')
                or reason.startswith('HARD_STOP_ACTIVE')
                or lifecycle.classification(reason, self.session)[0] != 'HARD_STOP'):
            return
        self.stop_reference = self.stops.stop(reason, self.session, evidence_refs=list(refs),
            last_success=last_success, run_id=self.run.run_id,
            retry_count=self.run.retries.get(self.session, 0),
            previous_stop_reference=previous, evidence=evidence)

    def hard_stop(self, reason, *, metadata=None, body=None):
        if self.stops is not None and self.stop_reference is None:
            refs = []  # No fallible diagnostic reads before committing this STOP.
            evidence = None
            if metadata is not None:
                name = 'request-' + str(self.count + 1).zfill(4)
                refs.append(dict(path=str(self.folder / (name + '.json')),
                                 sha256=hashlib.sha256(core.canonical_bytes(metadata)).hexdigest()))
                evidence = dict(response=metadata, body_reference=dict(
                    path=str(self.folder / (name + '.body')), sha256=hashlib.sha256(body).hexdigest())
                    if body is not None else None)
            self.stop_reference = self.stops.stop(reason, self.session, evidence_refs=refs,
                last_success=None, run_id=self.run.run_id, retry_count=self.run.retries.get(self.session, 0),
                evidence=evidence)
        if metadata is not None:
            self.capture(metadata, body)
        # Durable STOP precedes ordinary response logging and run bookkeeping.
        self.run.blocked_hosts['jpx.co.jp'] = reason
        self.run.save()

    def capture(self, metadata, body=None):
        self.count += 1
        if self.folder is not None:
            name = 'request-' + str(self.count).zfill(4)
            if body is not None:
                with (self.folder / (name + '.body')).open('xb') as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
            lifecycle.seal(self.folder / (name + '.json'), metadata)

    def retry(self, response=None, *, metadata=None, body=None):
        budget = self.budget()
        count = self.run.retries.get(self.session, 0)
        if type(count) is not int or not 0 <= count <= 2:
            raise ValueError('INVALID_RETRY_BUDGET')
        if count >= 2:
            if budget is not None:
                budget.update(exhausted=True, in_flight=False)
                if self.request_key:
                    budget['requests'][self.request_key].update(state='RETRY_EXHAUSTED', last_result=metadata)
                self.stops.save()
            self.run.exhausted.add(self.session)
            self.run.save()
            if metadata is not None:
                self.capture(metadata, response.body if response is not None else body)
            raise ValueError('RETRY_EXHAUSTED')
        delay = 0
        if response is not None:
            try:
                value = response.header('retry-after')
                if value is None:
                    delay = 1
                elif re.fullmatch(r'\d{1,10}', value.strip()):
                    delay = int(value.strip())
                else:
                    when = parsedate_to_datetime(value)
                    if when.tzinfo is None:
                        raise ValueError('RETRY_AFTER_INVALID')
                    delay = max(0, (when - _now()).total_seconds())
                if delay > 2 ** 31 - 1:
                    raise ValueError('RETRY_AFTER_INVALID')
                # No shorter policy wait cap exists. Reject instructions that
                # cannot be represented as an absolute deadline; never truncate.
                deadline = datetime.fromtimestamp(_now().timestamp() + delay, timezone.utc).isoformat()
            except (ValueError, TypeError, OverflowError, OSError) as error:
                self.hard_stop('RETRY_AFTER_INVALID', metadata=metadata, body=response.body)
                raise ValueError('RETRY_AFTER_INVALID') from error
        self.run.retries[self.session] = count + 1
        if budget is not None:
            budget.update(retry_count=count + 1, in_flight=False,
                          retry_not_before=datetime.fromtimestamp(_now().timestamp() + delay, timezone.utc).isoformat())
            if response is not None:
                self.stops.index['host_retry_not_before'] = deadline
                self.stops.index['rate_limited_hosts'] = ['jpx.co.jp']
            if self.request_key:
                budget['requests'][self.request_key].update(state='RETRY', last_result=metadata)
            self.stops.save()
        if response is not None:
            self.run.rate_limited_hosts.add('jpx.co.jp')
        self.run.save()  # Reserve before sleeping/requesting; interruption cannot reset it.
        if metadata is not None:
            self.capture(metadata, response.body if response is not None else body)
        if self.folder is not None:
            frozen._seal(self.folder / ('retry-' + str(count + 1) + '.json'),
                         dict(state='RETRY', target=self.session, retry_count=count + 1,
                              trigger='HTTP_429' if response is not None else 'TRANSIENT_TRANSPORT',
                              retried_at=_now().isoformat(), run_id=self.run.run_id,
                              final_result_references=[str(self.folder / 'result.json'), str(self.folder / 'failure.json')]))
        if delay and budget is None:
            time.sleep(delay)


def _transient(error):
    return (isinstance(error, (TimeoutError, ConnectionError, http.client.RemoteDisconnected,
                              http.client.IncompleteRead))
            or isinstance(error, socket.gaierror) and error.errno == socket.EAI_AGAIN
            or isinstance(error, OSError) and error.errno in
            {errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE,
             errno.ENETUNREACH, errno.EHOSTUNREACH})


class ChallengeControls(HTMLParser):
    """Strong widget controls or corroborating HTML security structures."""
    def __init__(self, *, inspect_scripts=True):
        super().__init__(convert_charrefs=True)
        self.found = False
        self.stack = []
        self.signals = set()
        self.inspect_scripts = inspect_scripts

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:  # Duplicate attributes cannot hide a widget marker.
            if key in ('id', 'class', 'name', 'src', 'action') and value:
                self.found |= bool(re.search(r'(?:g-recaptcha|h-captcha|recaptcha|cf-challenge|cf-turnstile|'
                    r'cf-browser-verification|challenge-platform|bot-challenge|security-challenge|'
                    r'human-verification|challenges\.cloudflare\.com/turnstile/|'
                    r'hcaptcha\.com/(?:1/)?api\.js|(?:^|[\s_-])captcha(?:$|[\s_-]))', value, re.I))
        attrs = dict(attrs)
        restriction = attrs.get('role') == 'alert' or bool(re.search(
            r'access[-_]?(?:denied|restricted)|restriction|security[-_]alert',
            ' '.join(attrs.get(key) or '' for key in ('id', 'class')), re.I))
        if tag not in ListingHTML.VOID:
            self.stack.append((tag, restriction, []))
        if tag == 'input' and attrs.get('type', '').lower() in ('submit', 'button'):
            self.inspect_text('button', False, attrs.get('value') or '')

    def handle_data(self, text):
        for _, _, parts in self.stack:
            parts.append(text)
        if self.inspect_scripts and self.stack and self.stack[-1][0] == 'script':
            # Known widget render calls or literal HTML written into the DOM are
            # structured controls. Do not evaluate JS or match descriptive words.
            if re.search(r'\b(?:grecaptcha|hcaptcha|turnstile)\s*\.\s*(?:render|execute)\s*\(', text):
                self.found = True
            for match in re.finditer(r'''\bdocument\s*\.\s*write(?:ln)?\s*\(\s*(["'])(.*?)\1\s*\)''', text, re.S):
                control = ChallengeControls(inspect_scripts=False)
                control.feed(match[2])
                control.close()
                self.found |= control.found

    def inspect_text(self, tag, restriction, text):
        text = re.sub(r'\s+', ' ', text).strip()
        challenge = bool(CHALLENGE.search(text))
        restricted = bool(re.search(r'access\s+(?:restricted|denied)|verification\s+required', text, re.I))
        if restriction and (challenge or restricted):
            self.signals.add('restriction')
        if tag in ('title', 'h1', 'h2') and (challenge or restricted):
            self.signals.add('heading')
        if tag in ('button', 'form') and re.search(
                r'^(?:verify|continue|human[ -]check)$|'
                r'verify.{0,30}(?:human|browser)|human\s+verification', text, re.I):
            self.signals.add('verification_control')
        # A standalone known prompt remains decisive; explanatory headings and
        # generic "verification required" need another structural signal.
        if tag in ('title', 'h1', 'form', 'iframe') and CHALLENGE.fullmatch(text.rstrip('.!?: ')):
            self.found = True

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                for node_tag, restriction, parts in self.stack[index:]:
                    self.inspect_text(node_tag, restriction, ''.join(parts))
                del self.stack[index:]
                break

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def close(self):
        super().close()
        for tag, restriction, parts in self.stack:
            self.inspect_text(tag, restriction, ''.join(parts))
        self.found |= len(self.signals) >= 2


def _challenge(response):
    # Transport size limits bound the entire received body, including claimed
    # PDFs. A late HTML widget must not be hidden by MIME or a PDF prefix.
    text = response.body.decode('utf-8', errors='replace')
    # Inspect recognizable HTML controls even if MIME/magic claims PDF. Ordinary
    # prose (including extracted PDF text) containing a word is not a control.
    controls_parser = ChallengeControls()
    controls_parser.feed(text)
    controls_parser.close()
    if controls_parser.found:
        return True
    if CHALLENGE.fullmatch(text.strip()):
        return True  # A bare restriction banner, including a partial read.
    if response.status != 200:
        return bool(CHALLENGE.search(text))
    if response.content_type() != 'text/html' and not response.body.startswith(b'%PDF-'):
        return bool(CHALLENGE.search(text))
    if response.body.startswith(b'%PDF-') and CHALLENGE.search(text):
        try:
            reader = PdfReader(BytesIO(response.body), strict=True)
            return reader.is_encrypted or not len(reader.pages)
        except Exception:
            return True  # A claimed PDF with challenge text needs more than magic.
    return False


def _request(transport, url, limit, max_redirects, chain=(), seen=(), *, context=None, role='listing'):
    context = context or RequestContext()
    official_url(url)  # Validate BEFORE contacting even a redirect destination.
    if url in seen:
        raise ValueError('REDIRECT_CYCLE')
    host = 'jpx.co.jp'  # Both official names share the same restriction boundary.
    while True:
        context.before_request(url, role)
        if host in context.run.blocked_hosts:
            raise ValueError('HARD_STOP_ACTIVE:' + context.run.blocked_hosts[host])
        if context.session in context.run.exhausted:
            raise ValueError('RETRY_EXHAUSTED')
        start = _now()
        try:
            response = transport.get(url, limit=limit)
        except Exception as error:
            end = _now()
            partial = getattr(error, 'partial', getattr(error.__cause__, 'partial', None))
            metadata = dict(url=url, started_at=start.isoformat(), completed_at=end.isoformat(),
                            result=type(error).__name__, reason=str(error),
                            partial_sha256=hashlib.sha256(partial).hexdigest() if partial is not None else None)
            observed_response = getattr(error, 'response', None)
            if observed_response is not None and _challenge(observed_response):
                metadata.update(status=observed_response.status, headers=observed_response.headers)
                context.hard_stop('SECURITY_CHALLENGE', metadata=metadata, body=observed_response.body)
                raise ValueError('SECURITY_CHALLENGE') from error
            if str(error).split(':', 1)[0] in lifecycle.HOST_CODES:
                context.hard_stop(str(error).split(':', 1)[0], metadata=metadata, body=partial)
                raise
            if start.tzinfo is None or end.tzinfo is None or end < start:
                context.commit_hard_failure('ACQUISITION_CLOCK_INVALID', evidence=metadata)
                raise ValueError('ACQUISITION_CLOCK_INVALID') from error
            if not _transient(error):
                context.commit_hard_failure(str(error), evidence=metadata)
                context.completed_request(metadata)
                context.capture(metadata, partial)
                raise
            context.retry(metadata=metadata, body=partial)
            continue
        end = _now()
        metadata = dict(url=url, started_at=start.isoformat(), completed_at=end.isoformat(),
                        response_url=response.url, status=response.status, content_type=response.content_type(),
                        headers=response.headers, response_sha256=hashlib.sha256(response.body).hexdigest())
        security = (STOP_STATUSES[response.status] if response.status in (401, 403)
                    else 'SECURITY_CHALLENGE' if _challenge(response) else None)
        if security:
            context.hard_stop(security, metadata=metadata, body=response.body)
            raise ValueError(security)
        if response.url != url:
            context.hard_stop('TRANSPORT_FOLLOWED_REDIRECT_WITHOUT_CHECK', metadata=metadata, body=response.body)
            raise ValueError('TRANSPORT_FOLLOWED_REDIRECT_WITHOUT_CHECK')
        streak = (context.run.rate_limited_hosts if context.stops is None else
                  context.stops.index.get('rate_limited_hosts', []))
        if response.status == 429 and (host in streak or context.run.retries.get(context.session, 0) >= 2):
            context.hard_stop('HOST_PERSISTENT_429', metadata=metadata, body=response.body)
            raise ValueError('HOST_PERSISTENT_429')
        if start.tzinfo is None or end.tzinfo is None or end < start:
            context.commit_hard_failure('ACQUISITION_CLOCK_INVALID', evidence=metadata)
            raise ValueError('ACQUISITION_CLOCK_INVALID')
        if len(response.body) > limit:
            context.commit_hard_failure('RESPONSE_TOO_LARGE', evidence=metadata)
            raise ValueError('RESPONSE_TOO_LARGE')
        target = None
        try:
            if response.status in (301, 302, 303, 307, 308):
                location = response.header('location')
                if not location or len(chain) + 1 > max_redirects:
                    raise ValueError('REDIRECT_LIMIT_OR_LOCATION_INVALID')
                target = official_url(urljoin(url, location))
                if target in (*seen, url):
                    raise ValueError('REDIRECT_CYCLE')
            elif response.status not in (200, 429):
                raise ValueError('UNEXPECTED_HTTP_STATUS:' + str(response.status))
            elif response.status == 200 and response.header('content-encoding') not in (None, 'identity'):
                raise ValueError('UNEXPECTED_CONTENT_ENCODING')
        except ValueError as error:
            # Header-visible safety failures are known before any response log.
            context.hard_stop(str(error), metadata=metadata, body=response.body)
            raise
        if response.status != 429:
            if response.status == 200:
                context.run.rate_limited_hosts.discard(host)
                if context.stops is not None:
                    context.stops.index['rate_limited_hosts'] = []
                    context.stops.index.pop('host_retry_not_before', None)
                    context.stops.save()
                context.run.save()
            context.completed_request(metadata)
            context.capture(metadata, response.body)
            break
        try:
            context.retry(response, metadata=metadata)
        except ValueError as error:
            if str(error) == 'RETRY_EXHAUSTED':
                context.hard_stop('HOST_PERSISTENT_429')
                raise ValueError('HOST_PERSISTENT_429') from error
            raise
    chain = (*chain, metadata)
    if target is not None:
        return _request(transport, target, limit, max_redirects, chain, (*seen, url), context=context, role=role)
    return response, list(chain)


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    start: int = 0
    end: int = 0

    def descendants(self, tag):
        result = []
        for child in self.children:
            if isinstance(child, Node):
                if child.tag == tag:
                    result.append(child)
                result.extend(child.descendants(tag))
        return result

    def text(self):
        return ''.join(c.text() if isinstance(c, Node) and c.tag not in ('script', 'style')
                       else c if isinstance(c, str) else '' for c in self.children).strip()


class ListingHTML(HTMLParser):
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'wbr'}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.html = html
        self.offsets = [0]
        for match in re.finditer('\n', html):
            self.offsets.append(match.end())
        self.root = Node('root')
        self.stack = [self.root]
        self.feed(html)
        self.close()
        if len(self.stack) != 1:
            raise ValueError('MALFORMED_LISTING_HTML')

    def position(self):
        line, col = self.getpos()
        return self.offsets[line - 1] + col

    def handle_starttag(self, tag, attrs):
        if len(dict(attrs)) != len(attrs):
            raise ValueError('AMBIGUOUS_HTML_ATTRIBUTES')
        node = Node(tag, dict(attrs), start=self.position())
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if len(self.stack) < 2 or self.stack[-1].tag != tag:
            raise ValueError('MALFORMED_LISTING_HTML')
        self.stack.pop().end = self.html.find('>', self.position()) + 1

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def listing_link(response, session):
    official_url(response.url)
    if response.status in STOP_STATUSES:
        raise ValueError(STOP_STATUSES[response.status])
    if response.status != 200 or response.content_type() != 'text/html':
        raise ValueError('UNEXPECTED_LISTING_RESPONSE')
    try:
        html = response.body.decode('utf-8-sig')
    except UnicodeDecodeError as e:
        raise ValueError('UNEXPECTED_LISTING_ENCODING') from e
    dom = ListingHTML(html)
    headings = dom.root.descendants('title') + dom.root.descendants('h1')
    if any(CHALLENGE.search(n.text()) for n in headings):
        raise ValueError('SECURITY_CHALLENGE')
    for n in dom.root.descendants('form') + dom.root.descendants('iframe'):
        if CHALLENGE.search(n.text() + ' ' + ' '.join(str(v) for v in n.attrs.values())):
            raise ValueError('SECURITY_CHALLENGE')
    if dom.root.descendants('base') or not any('東京証券取引所日報' in n.text() for n in headings):
        raise ValueError('UNEXPECTED_LISTING_HTML')
    tables = []
    for table in dom.root.descendants('table'):
        if table.descendants('table'):
            continue
        rows = table.descendants('tr')
        if not rows:
            continue
        headers = [re.sub(r'\s+', '', n.text()) for n in rows[0].children if isinstance(n, Node) and n.tag in ('th', 'td')]
        if headers == ['日付', '概算・精算表', '株式相場表', '債券相場表']:
            tables.append((rows, headers.index('株式相場表')))
    if len(tables) != 1:
        raise ValueError('UNEXPECTED_LISTING_HTML')
    rows, stock_column = tables[0]
    dated, matches = 0, []
    for row in rows[1:]:
        cells = [n for n in row.children if isinstance(n, Node) and n.tag in ('td', 'th')]
        if not cells or not re.fullmatch(r'\d{4}/\d{2}/\d{2}', cells[0].text()):
            raise ValueError('UNEXPECTED_LISTING_ROW')
        if len(cells) != 4 or any(n.attrs.get('colspan', '1') != '1' or n.attrs.get('rowspan', '1') != '1' for n in cells):
            raise ValueError('UNEXPECTED_LISTING_ROW')
        dated += 1
        if cells[0].text().replace('/', '-') == session:
            links = cells[stock_column].descendants('a')
            if len(links) != 1 or not links[0].attrs.get('href'):
                raise ValueError('PDF_LINK_UNKNOWN')
            href = links[0].attrs['href']
            url = official_url(urljoin(response.url, href))
            if not urlsplit(url).path.lower().endswith('.pdf'):
                raise ValueError('PDF_LINK_UNKNOWN')
            matches.append(dict(session=session, row_html=html[row.start:row.end],
                                exact_href=href, resolved_pdf_url=url))
    if not dated or len(matches) > 1:
        raise ValueError('LISTING_EMPTY_OR_AMBIGUOUS')
    return matches[0] if matches else None


def json_endpoints(response):
    """Recognize the observed inline JPX program; never invent endpoint names."""
    try:
        listing_link(response, '1900-01-01')
    except ValueError as e:
        if str(e) != 'LISTING_EMPTY_OR_AMBIGUOUS':
            raise
    dom = ListingHTML(response.body.decode('utf-8-sig'))
    candidates = []
    for script in dom.root.descendants('script'):
        code = ''.join(c for c in script.children if isinstance(c, str))
        base = re.findall(r"const\s+BASE_PATH\s*=\s*(['\"])([^'\"]+)\1\s*;", code)
        monthly = re.findall(r"const\s+BUCKNUMBER_JSON\s*=\s*BASE_PATH\s*\+\s*(['\"])([^'\"]+\.json)\1\s*;", code)
        template = re.findall(r'const\s+jsonUrl\s*=\s*`\$\{BASE_PATH\}([^`]+)`\s*;', code)
        if base and monthly and template:
            if len(base) != 1 or len(monthly) != 1 or len(template) != 1 or template[0].count('${month}') != 1:
                raise ValueError('JSON_DISCOVERY_AMBIGUOUS')
            if not all(token in code for token in ('item.Month', 'data.TableDatas', 'row.TradeDate', "'Stocks'", 'row[key]')):
                raise ValueError('JSON_DISCOVERY_PROGRAM_MISMATCH')
            base_url = official_url(urljoin(response.url, base[0][1]))
            candidates.append(dict(base_url=base_url,
                monthly_list_endpoint=official_url(urljoin(base_url, monthly[0][1])),
                monthly_json_template=template[0], script_sha256=hashlib.sha256(code.encode()).hexdigest()))
    if len(candidates) != 1:
        raise ValueError('JSON_DISCOVERY_MISSING_OR_AMBIGUOUS')
    return candidates[0]


def _json_response(response, warnings=None):
    official_url(response.url)
    if response.status in STOP_STATUSES:
        raise ValueError(STOP_STATUSES[response.status])
    if CHALLENGE.search(response.body[:65536].decode('utf-8', errors='replace')):
        raise ValueError('SECURITY_CHALLENGE')
    kind = response.content_type()
    # JPX's observed official JSON objects are served by S3 with this MIME.
    valid_type = kind in ('application/json', 'text/json') or (
        kind in ('binary/octet-stream', 'application/octet-stream') and urlsplit(response.url).path.endswith('.json'))
    if response.status != 200 or not valid_type:
        raise ValueError('UNEXPECTED_JSON_RESPONSE')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('JSON_DUPLICATE_KEY')
            result[key] = value
        return result
    try:
        data = json.loads(response.body.decode('utf-8-sig'), object_pairs_hook=pairs,
                          parse_constant=lambda x: (_ for _ in ()).throw(ValueError('JSON_NONFINITE')))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError('JSON_DECODE_FAILED') from e
    if not isinstance(data, dict) or not isinstance(data.get('TableDatas'), list):
        raise ValueError('JSON_SCHEMA_MISMATCH')
    if warnings is not None and kind not in ('application/json', 'text/json'):
        warnings.append(dict(state='WARN', code='CONTENT_TYPE_MISMATCH', target=response.url,
                             action='CONTINUE_AFTER_VALIDATION', content_type=kind))
    return data['TableDatas']


def monthly_json_url(response, discovery, session, *, warnings=None):
    rows = _json_response(response, warnings)
    months = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('Month'), str) or not re.fullmatch(r'\d{4}(0[1-9]|1[0-2])', row['Month']):
            raise ValueError('MONTHLY_LIST_SCHEMA_MISMATCH')
        months.append(row['Month'])
    if len(months) != len(set(months)):
        raise ValueError('MONTHLY_LIST_AMBIGUOUS')
    target = session[:7].replace('-', '')
    if target not in months:
        return None
    # The month must be published in the list and the substitution must be
    # explicitly specified by the observed HTML program. No PDF construction.
    reference = discovery['monthly_json_template'].replace('${month}', target)
    return official_url(urljoin(discovery['base_url'], reference))


def json_stock_link(response, session, *, warnings=None):
    rows = _json_response(response, warnings)
    matches = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('TradeDate'), str) or 'Stocks' not in row:
            raise ValueError('MONTHLY_JSON_SCHEMA_MISMATCH')
        trade = row['TradeDate']
        if not re.fullmatch(r'\d{8}', trade):
            raise ValueError('MONTHLY_JSON_DATE_INVALID')
        try:
            datetime.strptime(trade, '%Y%m%d')
        except ValueError as e:
            raise ValueError('MONTHLY_JSON_DATE_INVALID') from e
        if row['Stocks'] is not None and not isinstance(row['Stocks'], str):
            raise ValueError('MONTHLY_JSON_STOCKS_INVALID')
        if trade == session.replace('-', ''):
            matches.append(row)
    if len(matches) > 1:
        raise ValueError('MONTHLY_JSON_SESSION_AMBIGUOUS')
    if not matches or matches[0]['Stocks'] in (None, '', '-'):
        return None
    value = matches[0]['Stocks']
    url = official_url(urljoin(LISTING, value))
    if not urlsplit(url).path.lower().endswith('.pdf'):
        raise ValueError('PDF_LINK_UNKNOWN')
    return dict(session=session, trade_date=matches[0]['TradeDate'], stocks_raw=value,
                exact_href=value, resolved_pdf_url=url)


def discover_json_one(root, session, *, transport, evidence_folder, run_state=None):
    """Explicit JSON diagnostic, subject to the same STOP and host/retry boundary."""
    store = Path(root).resolve() / 'data/jpx_historical_direct'
    with frozen._locked(store):
        stops = lifecycle.Stops(store)
        stops.gate(session)
        run = run_state or RunState()
        run.bind(store)
        context = RequestContext(run, session, Path(evidence_folder), stops=stops)
        try:
            result = _discover_json(root, session, transport=transport, evidence_folder=evidence_folder,
                                    _listing=None, context=context)
            context.finalize_requests(Path(evidence_folder) / 'discovery.json')
            return result
        except FileExistsError:
            raise
        except Exception as error:
            if str(error) not in ('RETRY_EXHAUSTED', 'RETRY_INTERRUPTED_REVIEW_REQUIRED') and context.stop_reference is None:
                failure = Path(evidence_folder) / 'failure.json'
                refs = [lifecycle.reference(failure)] if failure.is_file() else []
                stops.stop(str(error), session, evidence_refs=refs, last_success=None,
                           run_id=run.run_id, retry_count=run.retries.get(session, 0))
            raise


def _discover_json(root, session, *, transport, evidence_folder, _listing, context):
    root = Path(root).resolve()
    policy, policy_hash = _policy(root)
    if session not in policy['allowed_sessions']:
        raise ValueError('NOT_APPROVED_HISTORICAL_SESSION')
    folder = Path(evidence_folder)
    folder.mkdir()  # Every diagnostic attempt has a fresh create-only folder.
    def fetch(label, url):
        # All attempt evidence is captured at the central boundary, after safety
        # state. No wrapper may log a response before its security classification.
        response, chain = _request(transport, url, policy['max_listing_bytes'], policy['max_redirects'],
                                   context=context, role=label)
        with (folder / (label + '.body')).open('xb') as stream:
            stream.write(response.body)
        frozen._seal(folder / (label + '.response.json'), dict(url=response.url, chain=chain,
            sha256=core.sha256(folder / (label + '.body'))))
        return response, chain
    try:
        if _listing is None:
            listing, listing_chain = fetch('listing', LISTING)
        else:
            listing, listing_chain = _listing
            with (folder / 'listing.body').open('xb') as stream:
                stream.write(listing.body)
            frozen._seal(folder / 'listing.response.json', dict(url=listing.url, chain=listing_chain,
                sha256=hashlib.sha256(listing.body).hexdigest()))
        discovery = json_endpoints(listing)
        monthly_list, list_chain = fetch('monthly_list', discovery['monthly_list_endpoint'])
        warnings = []
        month_url = monthly_json_url(monthly_list, discovery, session, warnings=warnings)
        month_chain, link = [], None
        if month_url is not None:
            monthly, month_chain = fetch('monthly', month_url)
            link = json_stock_link(monthly, session, warnings=warnings)
        receipt = dict(schema_version='jpx-json-discovery-v1', acquisition_method=METHOD,
            session=session, status='HREF_DISCOVERED' if link else 'NOT_YET_AVAILABLE',
            policy_sha256=policy_hash, listing_url=listing.url,
            listing_sha256=hashlib.sha256(listing.body).hexdigest(), listing_observed_at=listing_chain[-1]['completed_at'],
            discovery=discovery, monthly_list_url=monthly_list.url,
            monthly_list_sha256=hashlib.sha256(monthly_list.body).hexdigest(),
            monthly_list_observed_at=list_chain[-1]['completed_at'], monthly_json_reference_url=month_url,
            monthly_json_sha256=month_chain[-1]['response_sha256'] if month_chain else None,
            monthly_json_observed_at=month_chain[-1]['completed_at'] if month_chain else None,
            link=link, redirect_chains=dict(listing=listing_chain, monthly_list=list_chain, monthly=month_chain),
            pdf_requests=0, warnings=warnings, retry_count=context.run.retries.get(session, 0),
            final_result='HREF_DISCOVERED' if link else 'NOT_YET_AVAILABLE')
        frozen._seal(folder / 'discovery.json', receipt)
        context.warnings.extend(warnings)
        return receipt
    except Exception as e:
        context.commit_hard_failure(str(e), evidence=dict(reason=str(e), session=session,
            acquisition_method=METHOD, failed_at=_now().isoformat(), attempt=str(folder)))
        frozen._seal(folder / 'failure.json', dict(reason=str(e), automatic_retry=False, pdf_requests=0,
            retry_count=context.run.retries.get(session, 0), final_result=str(e).split(':', 1)[0],
            attempt_refs=[lifecycle.reference(path) for path in sorted(context.folder.glob('request-*.json'))]))
        raise


def validate_pdf(response, *, warnings=None):
    official_url(response.url)
    if not urlsplit(response.url).path.lower().endswith('.pdf'):
        raise ValueError('PDF_LINK_UNKNOWN')
    if _challenge(response):
        raise ValueError('SECURITY_CHALLENGE')
    if response.status in STOP_STATUSES:
        raise ValueError(STOP_STATUSES[response.status])
    if response.status != 200 or response.content_type() not in ('application/pdf', 'binary/octet-stream', 'application/octet-stream'):
        text = response.body[:65536].decode('utf-8', errors='replace')
        raise ValueError('SECURITY_CHALLENGE' if CHALLENGE.search(text) else 'PDF_EXPECTED_NON_PDF_RESPONSE')
    if not response.body.startswith(b'%PDF-'):
        raise ValueError('PDF_MAGIC_MISMATCH')
    try:
        reader = PdfReader(BytesIO(response.body), strict=True)
        if reader.is_encrypted or not len(reader.pages):
            raise ValueError('PDF_EMPTY_OR_ENCRYPTED')
    except Exception as e:
        raise ValueError('PDF_CORRUPT') from e
    if response.content_type() != 'application/pdf' and warnings is not None:
        warnings.append(dict(code='CONTENT_TYPE_MISMATCH', target=response.url,
                             action='CONTINUE_AFTER_PDF_AND_PROVENANCE_VALIDATION'))


def _policy(root):
    path = root / 'config/jpx_daily_acquisition_config.json'
    config = json.loads(path.read_bytes())
    policy = config['historical_direct']
    if (policy['acquisition_method'] != METHOD or policy['scope'] != 'HISTORICAL_CATCH_UP_ONLY'
            or policy['listing_url'] != LISTING or set(policy['allowed_hosts']) != HOSTS
            or policy.get('automatic_retry') is not True or policy.get('max_retries') != 2
            or policy.get('stop_policy_version') != lifecycle.VERSION or policy['scheduled']
            or config['timezone'] != 'Asia/Tokyo'):
        raise ValueError('HISTORICAL_DIRECT_POLICY_INVALID')
    return policy, core.sha256(path)


def _already_local(root, session, calendar):
    inbox = root / 'data/outcome_input/manual_jpx'
    manifest_path = inbox / 'retention_manifest.json'
    entries = json.loads(manifest_path.read_bytes())['entries'] if manifest_path.exists() else []
    matches = [entry for entry in entries if entry['session_date'] == session]
    if len(matches) > 1:
        raise ValueError('LOCAL_MANIFEST_SESSION_AMBIGUOUS')
    for entry in matches:
        retained = inbox / entry['filename']
        if retained.is_file() and core.sha256(retained) != entry.get('sha256'):
            raise ValueError('LOCAL_RAW_HASH_CONFLICT_REVIEW_REQUIRED')
        source_raw = entry.get('source_metadata', {}).get('raw_path')
        if source_raw and Path(source_raw).is_file() and core.sha256(source_raw) != entry.get('sha256'):
            raise ValueError('LOCAL_SOURCE_RAW_HASH_CONFLICT_REVIEW_REQUIRED')
        parsed_path = entry.get('normalized_output_path')
        if parsed_path and entry.get('normalized_output_sha256'):
            parsed_path = Path(parsed_path)
            if not parsed_path.is_file() or core.sha256(parsed_path) != entry['normalized_output_sha256']:
                raise ValueError('LOCAL_PARSED_HASH_CONFLICT_REVIEW_REQUIRED')
            if retained.is_file():
                with tempfile.TemporaryDirectory(prefix='jpx-manifest-validation-') as temporary:
                    gap.stage_existing_pdf(parsed_path, dict(entries=matches), calendar, inbox,
                                           Path(temporary) / 'layer.json', allowed_sessions=[session])
                return dict(code='EXISTING_VALID_LOCAL', local_validation='PASS')
        return dict(code='EXISTING_LOCAL_REQUIRES_VALIDATION', local_validation='PENDING')
    if (inbox / ('stq_' + session.replace('-', '') + '.pdf')).exists():
        return dict(code='EXISTING_LOCAL_REQUIRES_VALIDATION', local_validation='PENDING')
    for path in (root / 'data/outcome_parsed/jpx').glob('*.json'):
        if json.loads(path.read_bytes()).get('session_date') == session:
            return dict(code='EXISTING_LOCAL_REQUIRES_VALIDATION', local_validation='PENDING')
    known = {(inbox / e['filename']).resolve() for e in entries}
    if any(p.resolve() not in known for p in inbox.rglob('*.pdf')):
        raise ValueError('UNREGISTERED_LOCAL_RAW_REVIEW_REQUIRED')
    return False


def _verify_redirect_evidence(chain, first, digest):
    if not chain or chain[0]['url'] != first or chain[-1]['response_sha256'] != digest or chain[-1]['status'] != 200:
        raise ValueError('REDIRECT_PROVENANCE_CONFLICT')
    for index, record in enumerate(chain):
        official_url(record['url'])
        if record.get('response_url', record['url']) != record['url']:
            raise ValueError('REDIRECT_PROVENANCE_CONFLICT')
        if index < len(chain) - 1:
            location = Response(record['url'], record['status'], record['headers'], b'').header('location')
            if (record['status'] not in (301, 302, 303, 307, 308) or not location
                    or official_url(urljoin(record['url'], location)) != chain[index + 1]['url']):
                raise ValueError('REDIRECT_PROVENANCE_CONFLICT')


def _verified_acquisition(folder, session):
    """Replay official discovery and hash correspondence using retained bytes only."""
    receipt = core.read_frozen(folder / 'acquisition.json')
    if receipt.get('reused_acquisition_reference'):
        ref = receipt['reused_acquisition_reference']
        previous = lifecycle.read_reference(ref)
        if any(receipt.get(key) != value for key, value in previous.items()
               if key != 'reused_acquisition_reference'):
            raise ValueError('REUSED_ACQUISITION_PROVENANCE_CONFLICT')
        prior_folder = Path(ref['path']).parent
        if prior_folder == folder or not prior_folder.is_relative_to(folder.parents[2]):
            raise ValueError('REUSED_ACQUISITION_SCOPE_CONFLICT')
        _, raw = _verified_acquisition(prior_folder, session)
        return receipt, raw
    raw = Path(receipt['raw_path']).resolve()
    if (receipt.get('session') != session or receipt.get('acquisition_method') != METHOD
            or receipt.get('source_role') != 'OUTCOME_ONLY' or receipt.get('listing_url') != LISTING
            or not raw.is_relative_to(folder.parent.resolve())
            or core.sha256(raw) != receipt['raw_sha256']
            or core.sha256(folder / 'listing.html') != receipt['listing_sha256']):
        raise ValueError('ACQUISITION_PROVENANCE_OR_HASH_CONFLICT')
    chain = receipt['listing_response_metadata']
    listing = Response(chain[-1]['url'], chain[-1]['status'], chain[-1]['headers'],
                       (folder / 'listing.html').read_bytes())
    if chain[0]['url'] != LISTING or chain[-1]['response_sha256'] != receipt['listing_sha256']:
        raise ValueError('LISTING_PROVENANCE_CONFLICT')
    dom = ListingHTML(listing.body.decode('utf-8-sig'))
    if any('BUCKNUMBER_JSON' in ''.join(c for c in n.children if isinstance(c, str))
           for n in dom.root.descendants('script')):
        saved = folder / 'json_discovery'
        discovery_receipt = core.read_frozen(saved / 'discovery.json')
        def load(label, first):
            meta = core.read_frozen(saved / (label + '.response.json'))
            body = (saved / (label + '.body')).read_bytes()
            if hashlib.sha256(body).hexdigest() != meta['sha256']:
                raise ValueError('JSON_DISCOVERY_HASH_CONFLICT')
            last = meta['chain'][-1]
            if last['response_sha256'] != meta['sha256']:
                raise ValueError('JSON_DISCOVERY_PROVENANCE_CONFLICT')
            _verify_redirect_evidence(meta['chain'], first, meta['sha256'])
            if last['url'] != meta['url']:
                raise ValueError('JSON_DISCOVERY_PROVENANCE_CONFLICT')
            return Response(meta['url'], last['status'], last['headers'], body)
        discovery = json_endpoints(listing)
        monthly_list = load('monthly_list', discovery['monthly_list_endpoint'])
        month_url = monthly_json_url(monthly_list, discovery, session)
        monthly = load('monthly', month_url)
        if discovery_receipt['session'] != session:
            raise ValueError('JSON_DISCOVERY_PROVENANCE_CONFLICT')
        link = json_stock_link(monthly, session)
    else:
        link = listing_link(listing, session)
    if not link or any(receipt.get(key) != value for key, value in link.items()):
        raise ValueError('SOURCE_IDENTITY_MISMATCH')
    pdf_chain = receipt['redirect_chain']
    if (pdf_chain[0]['url'] != link['resolved_pdf_url']
            or pdf_chain[-1]['url'] != receipt['final_pdf_url']
            or pdf_chain[-1]['response_sha256'] != receipt['raw_sha256']):
        raise ValueError('PDF_PROVENANCE_CONFLICT')
    _verify_redirect_evidence(chain, LISTING, receipt['listing_sha256'])
    _verify_redirect_evidence(pdf_chain, link['resolved_pdf_url'], receipt['raw_sha256'])
    validate_pdf(Response(receipt['final_pdf_url'], receipt['http_status'],
                          {'Content-Type': receipt['content_type']}, raw.read_bytes()))
    return receipt, raw


def _candidate(folder, receipt, raw, session, calendar, *, context=None):
    if context is not None:
        context.integrity_evidence.update(source_sha256=receipt['raw_sha256'], expected_session=session)
    parsed = parser.parse(raw, session, acquisition_method=METHOD)
    frozen._seal(folder / 'parsed.json', parsed)
    entry = dict(session_date=session, filename=raw.name, sha256=receipt['raw_sha256'],
                 downloaded_at=receipt['completed_at'], parsed_at=parsed['retrieved_at'],
                 acquisition_type='BACKFILL', acquisition_origin=METHOD, parser_status='PASS',
                 core6_count=6, schema_validation='PASS', normalized_output_path=str(folder / 'parsed.json'),
                 normalized_output_sha256=core.sha256(folder / 'parsed.json'),
                 source_metadata=receipt, manual_review=True, eligible_for_trash=False,
                 scoring_status='NOT_RUN', forecast_ids=[], corporate_action_flag='UNKNOWN')
    if context is not None:
        # These hashes are already available for required validation. Retain them
        # in memory so a HARD failure needs no diagnostic reads before its STOP.
        context.integrity_evidence.update(parsed_source_sha256=parsed.get('source_file_hash'),
            parsed_source_sha256_after=parsed.get('source_file_hash_after'), parsed_session=parsed.get('session_date'),
            parsed_reference=dict(path=entry['normalized_output_path'], sha256=entry['normalized_output_sha256']))
    gap.stage_existing_pdf(folder / 'parsed.json', dict(entries=[entry]), calendar,
                           raw.parent, folder / 'validated_layer.json', allowed_sessions=[session])
    return entry


def _validated_saved(folder, session, calendar):
    receipt, raw = _verified_acquisition(folder, session)
    result = core.read_frozen(folder / 'result.json')
    entry = result['manifest_entry_candidate']
    if (entry['normalized_output_sha256'] != core.sha256(folder / 'parsed.json')
            or entry['source_metadata'] != receipt):
        raise ValueError('SAVED_BUNDLE_HASH_CONFLICT')
    core.read_frozen(folder / 'parsed.json')
    with tempfile.TemporaryDirectory(prefix='jpx-existing-validation-') as temporary:
        gap.stage_existing_pdf(folder / 'parsed.json', dict(entries=[entry]), calendar,
                               raw.parent, Path(temporary) / 'layer.json', allowed_sessions=[session])
    return result


def acquire_one(root, session, *, transport, resolution=None, run_state=None):
    """One authorized historical session, with bounded retry and offline SOFT resume.

    Bundles stay pending explicit registration. STOP, Raw and old attempts are
    immutable; this function never changes Manifest, Runtime or Forecast state.
    """
    root = Path(root).resolve()
    policy, policy_hash = _policy(root)
    calendar = json.loads((root / 'analysis/calendar_validation_2026-09-12.json').read_bytes())
    now = _now()
    if (now.tzinfo is None or calendar['jp_sessions'] != sorted(set(calendar['jp_sessions']))
            or not calendar['validation_range']['start'] <= session <= calendar['validation_range']['end']):
        raise ValueError('HISTORICAL_CALENDAR_OR_CLOCK_INVALID')
    prior = [s for s in calendar['jp_sessions'] if s < policy['forecast_target']]
    if (session not in policy['allowed_sessions'] or session not in calendar['jp_sessions']
            or not prior or session >= prior[-1] or session >= now.date().isoformat()):
        raise ValueError('NOT_APPROVED_HISTORICAL_SESSION')
    store = root / 'data/jpx_historical_direct'
    with frozen._locked(store):
        stops = lifecycle.Stops(store)
        active = stops.gate(session, resolution, network=False)
        run = run_state or RunState()
        run.bind(store)
        folder = None
        context = RequestContext(run, session, stops=stops)
        last_success = None
        validating_evidence = True
        try:
            existing = _already_local(root, session, calendar)
            if existing and active is None:
                return dict(status='SKIP_EXISTING_LOCAL', state='SKIP', session=session, web_requests=0, **existing)
            previous = store / session
            if previous.exists() and active is None:
                candidates = [previous, *sorted((previous / 'attempts').glob('*'))]
                for candidate in reversed(candidates):
                    if (candidate / 'result.json').exists():
                        old = core.read_frozen(candidate / 'result.json')
                        if old['status'] == 'VALIDATED_PENDING_EXPLICIT_REGISTRATION':
                            _validated_saved(candidate, session, calendar)
                            return dict(status='SKIP_EXISTING_LOCAL', state='SKIP', code='EXISTING_VALID_LOCAL',
                                        local_validation='PASS', session=session, web_requests=0)
                        if old.get('completed_at', now.isoformat())[:10] == now.date().isoformat():
                            return dict(old, web_requests=0)
                        break
                else:
                    raise FileExistsError('INTERRUPTED_ATTEMPT_REVIEW_REQUIRED')
            if active is not None:
                validating_evidence = False  # Invalid declarations alone do not invent an integrity failure.
                resolution_ref = stops.check_resolution(active, resolution, root)
                run.retries[session] = max(run.retries.get(session, 0), active['record'].get('retry_count', 0))
                run.save()
                # SOFT resume requires retained official acquisition evidence; no refetch.
                refs = active['record']['evidence_refs']
                acquisition_refs = [ref for ref in refs if Path(ref['path']).name == 'acquisition.json']
                source_folder = Path(acquisition_refs[0]['path']).parent if acquisition_refs else previous
                validating_evidence = True
                receipt, raw = _verified_acquisition(source_folder, session)
            else:
                stops.gate(session)  # Host STOP allows offline checks above, never a network attempt.
            if previous.exists():
                attempts = previous / 'attempts'
                attempts.mkdir(exist_ok=True)
                folder = attempts / str(len(list(attempts.iterdir())) + 1).zfill(6)
            else:
                folder = previous
            folder.mkdir()
            context.folder = folder
            frozen._seal(folder / 'start.json', dict(session=session, acquisition_method=METHOD,
                started_at=now.isoformat(), policy_sha256=policy_hash, policy_version=lifecycle.VERSION,
                run_id=run.run_id, retry_count=run.retries.get(session, 0)))
            resumed = None
            if active is not None:
                receipt = dict(receipt, reused_acquisition_reference=lifecycle.reference(source_folder / 'acquisition.json'))
                frozen._seal(folder / 'acquisition.json', receipt)
                last_success = lifecycle.reference(folder / 'acquisition.json')
                entry = _candidate(folder, receipt, raw, session, calendar, context=context)
                validating_evidence = False  # A receipt write failure keeps SOFT; it is not a new semantic conflict.
                resumed = stops.resume(active, resolution_ref, evidence_validation='PASS', run_id=run.run_id,
                                       retry_count=run.retries.get(session, 0), attempt=folder)
            else:
                listing, listing_chain = _request(transport, LISTING, policy['max_listing_bytes'],
                                                  policy['max_redirects'], context=context, role='listing')
                with (folder / 'listing.html').open('xb') as stream:
                    stream.write(listing.body)
                dom = ListingHTML(listing.body.decode('utf-8-sig'))
                if any('BUCKNUMBER_JSON' in ''.join(c for c in n.children if isinstance(c, str))
                       for n in dom.root.descendants('script')):
                    discovery = _discover_json(root, session, transport=transport,
                        evidence_folder=folder / 'json_discovery', _listing=(listing, listing_chain), context=context)
                    link = discovery['link']
                else:
                    link = listing_link(listing, session)
                observed_at = listing_chain[-1]['completed_at']
                if link is None:
                    result = dict(status='NOT_YET_AVAILABLE', state='SKIP', session=session, acquisition_method=METHOD,
                                  listing_url=LISTING, listing_observed_at=observed_at,
                                  listing_response_metadata=listing_chain, completed_at=_now().isoformat(),
                                  retry_count=run.retries.get(session, 0), final_result='NOT_YET_AVAILABLE',
                                  warnings=context.warnings, web_requests=context.count)
                    frozen._seal(folder / 'result.json', result)
                    context.finalize_requests(folder / 'result.json')
                    return result
                response, pdf_chain = _request(transport, link['resolved_pdf_url'], policy['max_pdf_bytes'],
                                               policy['max_redirects'], context=context, role='pdf')
                validate_pdf(response, warnings=context.warnings)
                # Local filename is never used to construct a retrieval URL.
                raw = folder / ('stq_' + session.replace('-', '') + '.pdf')
                with raw.open('xb') as stream:
                    stream.write(response.body)
                    stream.flush()
                    os.fsync(stream.fileno())
                receipt = dict(schema_version='jpx-historical-direct-v1', scope='HISTORICAL_CATCH_UP_ONLY',
                               source_role='OUTCOME_ONLY', acquisition_method=METHOD, browser_actor=None,
                               listing_url=LISTING, listing_observed_at=observed_at,
                               listing_response_metadata=listing_chain,
                               listing_sha256=core.sha256(folder / 'listing.html'), **link,
                               redirect_chain=pdf_chain, final_pdf_url=response.url,
                               started_at=now.isoformat(), completed_at=pdf_chain[-1]['completed_at'],
                               http_status=response.status, content_type=response.content_type(),
                               raw_sha256=core.sha256(raw), raw_path=str(raw), policy_sha256=policy_hash,
                               publication_time=None, terms_status='UNRESOLVED', warnings=context.warnings)
                if datetime.fromisoformat(receipt['completed_at']) < datetime.fromisoformat(observed_at) or datetime.fromisoformat(observed_at) < now:
                    raise ValueError('ACQUISITION_CLOCK_INVALID')
                frozen._seal(folder / 'acquisition.json', receipt)
                last_success = lifecycle.reference(folder / 'acquisition.json')
                entry = _candidate(folder, receipt, raw, session, calendar, context=context)
            result = dict(status='VALIDATED_PENDING_EXPLICIT_REGISTRATION', state='WARN' if receipt.get('warnings') else 'PASS',
                          session=session, acquisition_method=METHOD, acquisition_sha256=core.sha256(folder / 'acquisition.json'),
                          manifest_entry_candidate=entry, automatic_registration=False, warnings=receipt.get('warnings', []),
                          resumed_reference=resumed, completed_at=_now().isoformat(), web_requests=context.count,
                          retry_count=run.retries.get(session, 0), final_result='VALIDATED_PENDING_EXPLICIT_REGISTRATION',
                          aggregate=dict(validated_sessions=1, validated_tickers=6))
            frozen._seal(folder / 'result.json', result)
            context.finalize_requests(folder / 'result.json')
            return result
        except FileExistsError:
            raise
        except Exception as error:
            failure = dict(status='SKIP' if str(error) == 'RETRY_EXHAUSTED' else 'REVIEW_REQUIRED',
                           session=session, acquisition_method=METHOD, failed_at=_now().isoformat(),
                           reason=str(error), final_result=str(error).split(':', 1)[0],
                           run_id=run.run_id, retry_count=run.retries.get(session, 0))
            kind, _, _ = lifecycle.classification(str(error), session)
            integrity_conflict = validating_evidence or str(error) == 'STOP_EVIDENCE_HASH_CONFLICT'
            if active is None or integrity_conflict:
                refs = ([active['reference'], *active['record']['evidence_refs']] if active else [])
                if last_success:
                    refs.append(last_success)
                    failure['acquisition_reference'] = last_success
                if context.integrity_evidence:
                    failure['integrity'] = context.integrity_evidence
                    if context.integrity_evidence.get('parsed_reference'):
                        refs.append(context.integrity_evidence['parsed_reference'])
                context.commit_hard_failure(str(error), refs=refs, last_success=last_success,
                    previous=active['reference'] if active else None, evidence=failure)
            refs = []
            if folder is not None:
                failure['attempt_refs'] = [lifecycle.reference(path) for path in sorted(folder.glob('request-*.json'))]
                frozen._seal(folder / 'failure.json', failure)
                refs.append(lifecycle.reference(folder / 'failure.json'))
                if last_success:
                    refs.append(last_success)
            else:
                manifest = root / 'data/outcome_input/manual_jpx/retention_manifest.json'
                failure['local_manifest_reference'] = lifecycle.reference(manifest) if manifest.is_file() else None
                failure['local_inbox'] = str(root / 'data/outcome_input/manual_jpx')
                failure_path = store / ('local-failure-' + uuid4().hex + '.json')
                frozen._seal(failure_path, failure)
                refs.append(lifecycle.reference(failure_path))
            if str(error) in ('RETRY_EXHAUSTED', 'RETRY_INTERRUPTED_REVIEW_REQUIRED'):
                result = dict(status='SKIP_' + str(error), state='SKIP', session=session,
                              retry_count=run.retries.get(session, 0), final_result=str(error),
                              completed_at=_now().isoformat(), web_requests=context.count)
                if folder is not None:
                    frozen._seal(folder / 'result.json', result)
                return result
            # Failed resolution checks preserve the active stop without inventing a new cause.
            if (context.stop_reference is None and not str(error).startswith('HARD_STOP_ACTIVE')
                    and (active is None or kind == 'HARD_STOP' and integrity_conflict)):
                stops.stop(str(error), session, evidence_refs=refs, last_success=last_success,
                           run_id=run.run_id, retry_count=run.retries.get(session, 0),
                           previous_stop_reference=active['reference'] if active else None)
            raise

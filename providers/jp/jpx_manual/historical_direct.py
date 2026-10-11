"""Explicit one-session historical acquisition, never Fresh/Forecast/Outcome.

No CLI, scheduler, bulk operation, retry, or implicit network transport.
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
from urllib.parse import unquote, urljoin, urlsplit

from pypdf import PdfReader
from engine import minimal_trial as core
from engine import official_gap as gap
from engine import one_session_dry_run as frozen
from providers.jp.jpx_manual import parser

METHOD = parser.DIRECT_METHOD
LISTING = 'https://www.jpx.co.jp/markets/statistics-equities/daily/'
HOSTS = {'www.jpx.co.jp', 'jpx.co.jp'}
STOP_STATUSES = {403: 'HTTP_403', 429: 'RATE_LIMIT'}
CHALLENGE = re.compile(r'captcha|access\s*denied|security\s*challenge|'
                       r'verify.{0,30}(human|browser)|checking\s+your\s+browser|'
                       r'just\s+a\s+moment|cloudflare\s+ray|アクセス.{0,8}(拒否|制限)|'
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
            body = response.read(limit + 1)
            if len(body) > limit:
                raise ValueError('RESPONSE_TOO_LARGE')
            return Response(url, response.status, dict(pairs), body)
        finally:
            connection.close()


def _request(transport, url, limit, max_redirects, chain=(), seen=()):
    official_url(url)  # Validate BEFORE contacting even a redirect destination.
    if url in seen:
        raise ValueError('REDIRECT_CYCLE')
    start = _now()
    response = transport.get(url, limit=limit)
    end = _now()
    if start.tzinfo is None or end.tzinfo is None or end < start:
        raise ValueError('ACQUISITION_CLOCK_INVALID')
    if response.url != url:
        raise ValueError('TRANSPORT_FOLLOWED_REDIRECT_WITHOUT_CHECK')
    if len(response.body) > limit:
        raise ValueError('RESPONSE_TOO_LARGE')
    metadata = dict(url=url, started_at=start.isoformat(), completed_at=end.isoformat(),
                    status=response.status, content_type=response.content_type(),
                    headers=response.headers, response_sha256=hashlib.sha256(response.body).hexdigest())
    chain = (*chain, metadata)
    if response.status in STOP_STATUSES:
        raise ValueError(STOP_STATUSES[response.status])
    if response.status in (301, 302, 303, 307, 308):
        if response.content_type() == 'text/html' and CHALLENGE.search(response.body[:65536].decode('utf-8', errors='replace')):
            raise ValueError('SECURITY_CHALLENGE')
        location = response.header('location')
        if not location or len(chain) > max_redirects:
            raise ValueError('REDIRECT_LIMIT_OR_LOCATION_INVALID')
        target = official_url(urljoin(url, location))
        return _request(transport, target, limit, max_redirects, chain, (*seen, url))
    if response.status != 200:
        raise ValueError('UNEXPECTED_HTTP_STATUS:' + str(response.status))
    if response.header('content-encoding') not in (None, 'identity'):
        raise ValueError('UNEXPECTED_CONTENT_ENCODING')
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


def _json_response(response):
    official_url(response.url)
    if response.status in STOP_STATUSES:
        raise ValueError(STOP_STATUSES[response.status])
    if CHALLENGE.search(response.body[:65536].decode('utf-8', errors='replace')):
        raise ValueError('SECURITY_CHALLENGE')
    kind = response.content_type()
    # JPX's observed official JSON objects are served by S3 with this MIME.
    valid_type = kind in ('application/json', 'text/json') or (
        kind == 'binary/octet-stream' and urlsplit(response.url).path.endswith('.json'))
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
    return data['TableDatas']


def monthly_json_url(response, discovery, session):
    rows = _json_response(response)
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


def json_stock_link(response, session):
    rows = _json_response(response)
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


def discover_json_one(root, session, *, transport, evidence_folder, _listing=None):
    """Explicit listing/JSON-only diagnostic. Does not clear/bypass STOP for PDFs."""
    root = Path(root).resolve()
    policy, policy_hash = _policy(root)
    if session not in policy['allowed_sessions']:
        raise ValueError('NOT_APPROVED_HISTORICAL_SESSION')
    folder = Path(evidence_folder)
    folder.mkdir()  # Every diagnostic attempt has a fresh create-only folder.
    def fetch(label, url):
        class EvidenceTransport:
            count = 0
            def get(self, target, *, limit):
                started = _now().isoformat()
                response = transport.get(target, limit=limit)
                completed = _now().isoformat()
                self.count += 1
                name = label + '.http' + str(self.count)
                with (folder / (name + '.body')).open('xb') as stream:
                    stream.write(response.body)
                frozen._seal(folder / (name + '.json'), dict(url=target, status=response.status,
                    headers=response.headers, started_at=started, completed_at=completed,
                    sha256=hashlib.sha256(response.body).hexdigest()))
                return response
        response, chain = _request(EvidenceTransport(), url, policy['max_listing_bytes'], policy['max_redirects'])
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
        month_url = monthly_json_url(monthly_list, discovery, session)
        month_chain, link = [], None
        if month_url is not None:
            monthly, month_chain = fetch('monthly', month_url)
            link = json_stock_link(monthly, session)
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
            pdf_requests=0)
        frozen._seal(folder / 'discovery.json', receipt)
        return receipt
    except Exception as e:
        frozen._seal(folder / 'failure.json', dict(reason=str(e), automatic_retry=False, pdf_requests=0))
        raise


def validate_pdf(response):
    if response.status in STOP_STATUSES:
        raise ValueError(STOP_STATUSES[response.status])
    if response.status != 200 or response.content_type() != 'application/pdf':
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


def _policy(root):
    path = root / 'config/jpx_daily_acquisition_config.json'
    policy = json.loads(path.read_bytes())['historical_direct']
    if (policy['acquisition_method'] != METHOD or policy['scope'] != 'HISTORICAL_CATCH_UP_ONLY'
            or policy['listing_url'] != LISTING or set(policy['allowed_hosts']) != HOSTS
            or policy['automatic_retry'] or policy['scheduled']):
        raise ValueError('HISTORICAL_DIRECT_POLICY_INVALID')
    return policy, core.sha256(path)


def _already_local(root, session):
    inbox = root / 'data/outcome_input/manual_jpx'
    manifest_path = inbox / 'retention_manifest.json'
    entries = json.loads(manifest_path.read_bytes())['entries'] if manifest_path.exists() else []
    for entry in entries:
        if entry['session_date'] == session:
            retained = inbox / entry['filename']
            if retained.is_file() and core.sha256(retained) != entry.get('sha256'):
                raise ValueError('LOCAL_RAW_HASH_CONFLICT_REVIEW_REQUIRED')
            return True
    if (inbox / ('stq_' + session.replace('-', '') + '.pdf')).exists():
        return True
    for path in (root / 'data/outcome_parsed/jpx').glob('*.json'):
        if json.loads(path.read_bytes()).get('session_date') == session:
            return True
    known = {(inbox / e['filename']).resolve() for e in entries}
    if any(p.resolve() not in known for p in inbox.rglob('*.pdf')):
        raise ValueError('UNREGISTERED_LOCAL_RAW_REVIEW_REQUIRED')
    return False


def acquire_one(root, session, *, transport):
    """Exactly one session, serialized; no automatic retry even after interruption.

    Bundles are candidates: this function never changes Manifest, Runtime,
    Historical Bridge, Forecast, Fresh Close, or Outcome history.
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
        if (store / 'STOP.json').exists():
            raise ValueError('STOP_FOR_REVIEW_NO_AUTOMATIC_RETRY')
        if _already_local(root, session):
            return dict(status='SKIP_EXISTING_LOCAL', session=session, web_requests=0)
        folder = store / session
        folder.mkdir()  # Existing attempt, success or interrupted, is never retried.
        frozen._seal(folder / 'start.json', dict(session=session, acquisition_method=METHOD,
                                                started_at=now.isoformat(), policy_sha256=policy_hash))
        try:
            listing, listing_chain = _request(transport, LISTING, policy['max_listing_bytes'], policy['max_redirects'])
            with (folder / 'listing.html').open('xb') as stream:
                stream.write(listing.body)
            dom = ListingHTML(listing.body.decode('utf-8-sig'))
            # Observed JPX JSON program takes priority over the legacy row fixtures.
            if any('BUCKNUMBER_JSON' in ''.join(c for c in n.children if isinstance(c, str))
                   for n in dom.root.descendants('script')):
                discovery_receipt = discover_json_one(root, session, transport=transport,
                    evidence_folder=folder / 'json_discovery', _listing=(listing, listing_chain))
                link = discovery_receipt['link']
            else:
                link = listing_link(listing, session)
            observed_at = listing_chain[-1]['completed_at']
            if link is None:
                result = dict(status='NOT_YET_AVAILABLE', session=session, acquisition_method=METHOD,
                              listing_url=LISTING, listing_observed_at=observed_at,
                              listing_response_metadata=listing_chain)
                frozen._seal(folder / 'result.json', result)
                return result
            response, pdf_chain = _request(transport, link['resolved_pdf_url'], policy['max_pdf_bytes'], policy['max_redirects'])
            validate_pdf(response)
            # Local naming is independent of URL discovery; this is never used to construct a URL.
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
                           publication_time=None, terms_status='UNRESOLVED')
            if datetime.fromisoformat(receipt['completed_at']) < datetime.fromisoformat(observed_at) or datetime.fromisoformat(observed_at) < now:
                raise ValueError('ACQUISITION_CLOCK_INVALID')
            frozen._seal(folder / 'acquisition.json', receipt)  # Keep original evidence even if parsing fails.
            parsed = parser.parse(raw, session, acquisition_method=METHOD)
            frozen._seal(folder / 'parsed.json', parsed)
            entry = dict(session_date=session, filename=raw.name, sha256=receipt['raw_sha256'],
                         downloaded_at=receipt['completed_at'], parsed_at=parsed['retrieved_at'],
                         acquisition_type='BACKFILL', acquisition_origin=METHOD, parser_status='PASS',
                         core6_count=6, schema_validation='PASS', normalized_output_path=str(folder / 'parsed.json'),
                         normalized_output_sha256=core.sha256(folder / 'parsed.json'),
                         source_metadata=receipt, manual_review=True, eligible_for_trash=False,
                         scoring_status='NOT_RUN', forecast_ids=[], corporate_action_flag='UNKNOWN')
            # Existing Raw/Parsed/Core6/OHLC/hash validation, without touching the real Manifest.
            gap.stage_existing_pdf(folder / 'parsed.json', dict(entries=[entry]), calendar,
                                   folder, folder / 'validated_layer.json', allowed_sessions=[session])
            result = dict(status='VALIDATED_PENDING_EXPLICIT_REGISTRATION', session=session,
                          acquisition_method=METHOD, acquisition_sha256=core.sha256(folder / 'acquisition.json'),
                          manifest_entry_candidate=entry, automatic_registration=False)
            frozen._seal(folder / 'result.json', result)
            return result
        except Exception as e:
            failure = dict(status='REVIEW_REQUIRED', session=session, acquisition_method=METHOD,
                           failed_at=_now().isoformat(), reason=str(e), automatic_retry=False)
            frozen._seal(folder / 'failure.json', failure)
            frozen._seal(store / 'STOP.json', failure)
            raise

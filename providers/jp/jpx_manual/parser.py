"""Offline, fail-closed parser for locally acquired JPX auction PDF tables."""
import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

import pdfplumber
from pypdf import PdfReader
from jsonschema import Draft202012Validator, FormatChecker

CORE = {
    '6857': 'Advantest', '7203': 'Toyota Motor',
    '8306': 'Mitsubishi UFJ Financial Group', '1605': 'INPEX',
    '9433': 'KDDI', '3197': 'Skylark Holdings',
}
VERSION = 'jpx-local-0.2.2'
ROOT = Path(__file__).resolve().parents[3]
ALLOWED_BROWSER_ACTORS = ('CHATGPT_DESKTOP_BUILT_IN_BROWSER', 'GOOGLE_CHROME_VISIBLE_RDC')
DIRECT_METHOD = 'OFFICIAL_LISTING_DIRECT_HTTPS'


def number(value):
    if not re.fullmatch(r'(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?', value):
        raise ValueError('MISSING_OR_INVALID_NUMERIC: ' + repr(value))
    return Decimal(value.replace(',', ''))


def source_date(text, expected):
    matches = set(re.findall(r'(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日', text))
    if len(matches) != 1:
        raise ValueError('MISSING_OR_AMBIGUOUS_SESSION_DATE')
    actual = date(*map(int, next(iter(matches)))).isoformat()
    if actual != expected:
        raise ValueError('DATA_CONFLICT: session date ' + actual)
    return actual


def validate_prices(values):
    o, h, l, c = (values[k] for k in ('open', 'high', 'low', 'close'))
    if min(o, h, l, c) <= 0 or not (l <= o <= h and l <= c <= h):
        raise ValueError('DATA_CONFLICT: invalid OHLC order')


def unique(records):
    tickers = [r['ticker'] for r in records]
    if len(tickers) != len(set(tickers)):
        raise ValueError('DUPLICATE_TICKER')
    if set(tickers) != set(CORE):
        raise ValueError('MISSING_CORE_TICKER')


def column_bounds(page, word):
    x = (word['x0'] + word['x1']) / 2
    y = (word['top'] + word['bottom']) / 2
    lines = sorted({e['x0'] for e in page.edges
                    if e['orientation'] == 'v' and e['top'] <= y <= e['bottom']})
    left = [v for v in lines if v < x]
    right = [v for v in lines if v > x]
    if not left or not right:
        raise ValueError('HEADER_COLUMN_BOUNDARY_MISSING')
    return max(left), min(right)


def cell(page, bounds, row):
    left, right = bounds
    chars = [c for c in page.chars if left <= (c['x0'] + c['x1']) / 2 < right
             and abs(c['top'] - row['top']) < 2]
    return ''.join(c['text'] for c in sorted(chars, key=lambda c: c['x0'])).strip()


def extract_page(page, expected, page_number, verified_section=False):
    words = page.extract_words()
    text = page.extract_text()
    compact = re.sub(r'\s+', '', text)
    if 'AuctionTradesRegularWay' not in compact and not verified_section:
        raise ValueError('UNSUPPORTED_TRADING_SECTION')
    session = source_date(text, expected)
    opens = sorted([w for w in words if w['text'] == '始値'], key=lambda w: w['x0'])
    if len(opens) != 2:
        raise ValueError('UNSUPPORTED_HEADER: expected AM/PM columns')
    y = opens[0]['top']
    headers = {}
    for label in ('始値', '高値', '安値', '終値'):
        found = sorted([w for w in words if w['text'] == label and abs(w['top'] - y) < 2],
                       key=lambda w: w['x0'])
        if len(found) != 2:
            raise ValueError('MISSING_HEADER: ' + label)
        headers[label] = [column_bounds(page, w) for w in found]
    for label in ('コード', '銘柄名', '単位', '売買高', '売買代金'):
        found = [w for w in words if w['text'] == label and abs(w['top'] - y) < 20]
        if len(found) != 1:
            raise ValueError('AMBIGUOUS_HEADER: ' + label)
        headers[label] = column_bounds(page, found[0])
    for label, bounds in (('午前', headers['始値'][0]), ('午後', headers['始値'][1])):
        found = [w for w in words if w['text'] == label and 0 < y - w['top'] < 40]
        if len(found) != 1 or not bounds[0] <= found[0]['x0'] < bounds[1]:
            raise ValueError('SESSION_GROUP_HEADER_MISMATCH')
    for unit, bounds in (('千株[thous.shs.]', headers['売買高']),
                         ('千円[￥thous.]', headers['売買代金'])):
        if not any(w['text'] == unit and y < w['top'] < y + 60
                   and bounds[0] <= w['x0'] < bounds[1] for w in words):
            raise ValueError('UNKNOWN_UNIT: ' + unit)
    for bounds in headers['始値'] + headers['高値'] + headers['安値'] + headers['終値']:
        if not any(w['text'] == '円[￥]' and y < w['top'] < y + 60
                   and bounds[0] <= w['x0'] < bounds[1] for w in words):
            raise ValueError('UNKNOWN_PRICE_UNIT')
    out = []
    for row in words:
        ticker = row['text']
        if ticker not in CORE or row['top'] < y + 40:
            continue
        if not headers['コード'][0] <= row['x0'] < headers['コード'][1]:
            continue
        halves = []
        for half in range(2):
            values = {key: number(cell(page, headers[label][half], row))
                      for key, label in zip(('open', 'high', 'low', 'close'),
                                            ('始値', '高値', '安値', '終値'))}
            validate_prices(values)
            halves.append(values)
        daily = dict(open=halves[0]['open'], high=max(h['high'] for h in halves),
                     low=min(h['low'] for h in halves), close=halves[1]['close'])
        validate_prices(daily)
        volume_raw = number(cell(page, headers['売買高'], row))
        shares = volume_raw * 1000
        if shares != shares.to_integral_value():
            raise ValueError('NON_INTEGER_SHARE_VOLUME')
        trading_unit = number(cell(page, headers['単位'], row))
        if trading_unit <= 0 or trading_unit != trading_unit.to_integral_value():
            raise ValueError('INVALID_TRADING_UNIT')
        out.append(dict(ticker=ticker, company_name=CORE[ticker],
                        source_company_name=cell(page, headers['銘柄名'], row),
                        session_date=session, **{k: float(v) for k, v in daily.items()},
                        volume=int(shares), source_volume_thousand_shares=str(volume_raw),
                        trading_unit=int(trading_unit),
                        trading_value=float(number(cell(page, headers['売買代金'], row)) * 1000),
                        source_page=page_number, source_row_top=row['top'],
                        source_columns={k: v for k, v in headers.items()},
                        morning={k: float(v) for k, v in halves[0].items()},
                        afternoon={k: float(v) for k, v in halves[1].items()},
                        corporate_action_status='UNKNOWN',
                        flags=['ZERO_VOLUME'] if shares == 0 else []))
    return out


def parse(path, expected, acquisition_method='HUMAN_MANUAL_DOWNLOAD', browser_actor=None):
    if acquisition_method not in ('HUMAN_MANUAL_DOWNLOAD', 'BROWSER_ASSISTED_SINGLE_FILE', DIRECT_METHOD):
        raise ValueError('UNKNOWN_ACQUISITION_METHOD')
    if acquisition_method == 'BROWSER_ASSISTED_SINGLE_FILE' and browser_actor not in ALLOWED_BROWSER_ACTORS:
        raise ValueError('BROWSER_ACTOR_REQUIRED')
    if acquisition_method != 'BROWSER_ASSISTED_SINGLE_FILE' and browser_actor is not None:
        raise ValueError('BROWSER_ACTOR_CONFLICT')
    path = path.resolve()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    records = []
    scanned_candidates = []
    reader = PdfReader(path)
    auction_sections = set()
    with pdfplumber.open(path) as pdf:
        for i, p in enumerate(reader.pages):
            text = p.extract_text()
            compact = re.sub(r'\s+', '', text)
            section_match = re.search(r'\d{4}年\s*\d+月\s*\d+日[^\n]*?\s(\d+)\s*-\s*\d+', text)
            section = section_match.group(1) if section_match else None
            if 'AuctionTradesRegularWay' in compact and section:
                auction_sections.add(section)
            if section not in auction_sections:
                continue
            if not re.search(r'(?m)^\s*(?:' + '|'.join(CORE) + r')\s', text):
                continue
            scanned_candidates.append(i + 1)
            records.extend(extract_page(pdf.pages[i], expected, i + 1, verified_section=True))
    unique(records)
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    if before != after:
        raise ValueError('ORIGINAL_HASH_CHANGED')
    now = datetime.now(timezone.utc).isoformat()
    schema = json.loads((ROOT / 'config/normalized_market_schema.json').read_text(encoding='utf-8-sig'))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    for r in records:
        official_acquisition = acquisition_method != 'HUMAN_MANUAL_DOWNLOAD'
        r.update(provider='JPX_DAILY_REPORT' if official_acquisition else 'JPX_DAILY_REPORT_MANUAL',
                 acquisition=acquisition_method if official_acquisition else 'MANUAL_DOWNLOAD_ONLY',
                 acquisition_method=acquisition_method, browser_actor=browser_actor,
                 series_type='RAW_OHLC', retrieved_at=now, source_file_hash=before,
                 quality_state='PARTIAL_DATA', open_semantics='OFFICIAL_SESSION_OPEN',
                 close_semantics='OFFICIAL_SESSION_CLOSE', semantics_status='CANDIDATE',
                 volume_semantics='SESSION_CUMULATIVE_VOLUME')
        observation = dict(symbol=r['ticker'], issuer_id=None, exchange='XTKS', currency='JPY',
                           session_date=expected, **{k: r[k] for k in ('open', 'high', 'low', 'close', 'volume', 'trading_value')},
                           dividend=None, split_factor=None, series_type='RAW_OHLC', provider=r['provider'],
                           observation_time=None, published_time=None, available_at=None, retrieved_at=now,
                           quality_state='PARTIAL_DATA', primary_price_direction_eligible=False,
                           quality_flags=['TERMS_UNVERIFIED', 'CORPORATE_ACTION_UNKNOWN', 'SOURCE_TIMESTAMPS_UNKNOWN'] + r['flags'],
                           source_reference=dict(reference=str(path) + '#page=' + str(r['source_page']) + ';sha256=' + before,
                                                 provider_timestamp=None, price_basis_status='NOT_VALIDATED',
                                                 notes=['Local import time is not download/publication time.',
                                                        'AM open, PM close, max/min both halves; no price adjustment.',
                                                        'Official-session semantics candidate; source admission pending.']),
                           normalization=dict(basis_session=expected, method='AM_PM_AGGREGATION_NO_PRICE_ADJUSTMENT', dividend_included=False))
        validator.validate(observation)
        r['normalized_observation'] = observation
    return dict(parser_version=VERSION, source_file=str(path), source_file_hash=before,
                source_file_hash_after=after, manual_file_integrity='PASS', page_count=len(reader.pages),
                candidate_pages=scanned_candidates, session_date=expected, core6_extraction='6/6',
                acquisition_method=acquisition_method, browser_actor=browser_actor,
                duplicate_count=0, normalized_schema_validation='PASS', records=records,
                retrieved_at=now, retrieved_at_meaning='LOCAL_IMPORT_TIME_NOT_DOWNLOAD_TIME',
                original_download_time=None, OUTCOME_SCORING_SOURCE_VALIDATED=False,
                normal_scoring_llm_calls_expected=0, web_fetch_calls=0)


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument('source', type=Path)
    cli.add_argument('--expected-session', required=True)
    cli.add_argument('--output', required=True, type=Path)
    cli.add_argument('--acquisition-method', choices=['HUMAN_MANUAL_DOWNLOAD', 'BROWSER_ASSISTED_SINGLE_FILE', DIRECT_METHOD],
                     default='HUMAN_MANUAL_DOWNLOAD')
    cli.add_argument('--browser-actor', choices=list(ALLOWED_BROWSER_ACTORS))
    args = cli.parse_args()
    date.fromisoformat(args.expected_session)
    if args.output.exists():
        raise FileExistsError('Refusing to overwrite output')
    result = parse(args.source, args.expected_session, args.acquisition_method, args.browser_actor)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({k: result[k] for k in ('session_date', 'core6_extraction', 'manual_file_integrity', 'normalized_schema_validation')}))


if __name__ == '__main__':
    main()

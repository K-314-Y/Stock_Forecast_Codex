"""Research-only adapter. No forecasts, scoring, scheduling or automatic admission."""
import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[3]
CODES = ('6857', '7203', '8306', '1605', '9433', '3197')
CACHE = ROOT / 'analysis/provider_test_results/yfinance_cache_2026-09-13'


def read_cache(code):
    if code not in CODES:
        raise ValueError('Only six Tokyo Core codes allowed')
    meta = json.loads((CACHE / (code + '_probe.json')).read_text(encoding='utf-8'))
    path = CACHE / (code + '_auto_adjust_false.csv')
    frame = pd.read_csv(path, index_col=0, parse_dates=True)
    return frame, meta


def validate(frame, expected_sessions=None):
    """Return flags without sorting, removing rows, repairing or filling values."""
    flags = {}
    dates = frame.index.strftime('%Y-%m-%d').tolist()
    flags['duplicate_count'] = int(pd.Index(dates).duplicated().sum())
    flags['ordered'] = frame.index.is_monotonic_increasing
    flags['missing_sessions'] = None if expected_sessions is None else sorted(set(expected_sessions)-set(dates))
    flags['unexpected_sessions'] = None if expected_sessions is None else sorted(set(dates)-set(expected_sessions))
    flags['timezone_missing'] = frame.index.tz is None
    problems = []
    for index, row in frame.iterrows():
        errors = []
        for key in ('Open', 'High', 'Low', 'Close', 'Volume'):
            if key not in row or not math.isfinite(float(row[key])):
                errors.append('MISSING_OR_NONFINITE_' + key.upper())
            elif row[key] < 0:
                errors.append('NEGATIVE_' + key.upper())
        if not errors:
            if row['Low'] > min(row['Open'], row['Close']) or row['High'] < max(row['Open'], row['Close']) or row['High'] < row['Low']:
                errors.append('INVALID_OHLC')
            if row['Volume'] == 0:
                errors.append('ZERO_VOLUME_REVIEW')
        if errors:
            problems.append({'session': index.strftime('%Y-%m-%d'), 'flags': errors})
    flags['row_problems'] = problems
    return flags


def normalize(code, frame, metadata, total_return=False):
    if metadata['metadata'].get('symbol') != code + '.T' or metadata['metadata'].get('currency') != 'JPY' or metadata['metadata'].get('exchangeTimezoneName') != 'Asia/Tokyo':
        raise ValueError('Tokyo identity/currency/timezone mismatch')
    if frame.empty:
        return []
    if frame.index.tz is None:
        raise ValueError('Naive session labels must not be guessed')
    checked = validate(frame)
    if checked['duplicate_count'] or not checked['ordered']:
        raise ValueError('Unsorted/duplicate input; no silent repair')
    converted = yf.utils.auto_adjust(frame) if total_return else frame.copy()
    problems = {p['session']: p['flags'] for p in checked['row_problems']}
    validator = Draft202012Validator(json.loads((ROOT / 'config/normalized_market_schema.json').read_text()), format_checker=FormatChecker())
    records = []
    for stamp, row in converted.iterrows():
        session = stamp.strftime('%Y-%m-%d')
        later = frame.loc[frame.index > stamp, 'Stock Splits']
        divisor = float(later[later != 0].prod())
        issues = problems.get(session, [])
        record = dict(symbol=code, issuer_id=None, exchange='XTKS', currency='JPY', session_date=session,
            **{name: (float(row[key]) if math.isfinite(float(row[key])) else None) for name, key in [('open','Open'),('high','High'),('low','Low'),('close','Close'),('volume','Volume')]},
            trading_value=None, dividend=float(row['Dividends']), split_factor=divisor,
            series_type='TOTAL_RETURN_REFERENCE' if total_return else 'SPLIT_NORMALIZED_OHLC', provider='YAHOO_FINANCE_YFINANCE',
            observation_time=stamp.isoformat(), published_time=None, available_at=None, retrieved_at=metadata['retrieved_at'],
            quality_state='DATA_CONFLICT' if issues else 'PARTIAL_DATA',
            source_reference=dict(reference=str(CACHE.relative_to(ROOT) / (code + '_auto_adjust_false.csv')),
                provider_timestamp=stamp.isoformat(), provider_as_of=metadata['metadata'].get('regularMarketTime'),
                price_basis_status='NOT_VALIDATED', notes=[
                    'Observation timestamp is provider session label, NOT an intraday quote or close timestamp.',
                    'auto_adjust=false OHLC appears backward split-normalized; no second split applied.',
                    'Split divisor is metadata derived from supplied events; native RAW_OHLC was not observed.',
                    'Historical availability, finality and complete adjustment factors are unverified.',
                    'TOTAL_RETURN_REFERENCE is a provider dividend-adjusted reference, not an audited reinvestment portfolio.']),
            normalization=dict(basis_session=frame.index[-1].strftime('%Y-%m-%d'),
                               method='yfinance.utils.auto_adjust cached input' if total_return else 'provider backward split basis, passed through unchanged',
                               dividend_included=total_return), primary_price_direction_eligible=False,
            quality_flags=issues + ['HISTORICAL_PIT_UNVERIFIED', 'PRICE_BASIS_CANDIDATE', 'FRESHNESS_PENDING_LIVE_OBSERVATION'])
        validator.validate(record)
        records.append(record)
    return records


def fetch_update(code, end_exclusive, *, allow_network=False):
    """Explicit opt-in incremental research fetch. No old cache overwrite or merging."""
    if code not in CODES:
        raise ValueError('Only six Core codes allowed')
    frame, meta = read_cache(code)
    start = frame.index[-1].date() + timedelta(days=1)
    end = date.fromisoformat(end_exclusive)
    if end <= start:
        return {'status': 'CACHE_HIT', 'network_requests': 0}
    if end > date.today() or (end-start).days > 31:
        raise ValueError('Only completed dates and bounded incremental requests allowed')
    dest = CACHE / 'increments' / f'{code}_{start}_{end}'
    if (dest / 'metadata.json').exists():
        return {'status': 'CACHE_HIT', 'network_requests': 0, 'path': str(dest)}
    if not allow_network:
        raise PermissionError('Explicit research acquisition authorization required')
    if (CACHE / 'increments').exists() and any((CACHE / 'increments').glob(code + '_*')):
        raise PermissionError('Previous increment requires review before another overlapping update')
    runtime = json.loads((ROOT / 'config/runtime_status.json').read_text())
    if runtime['run_mode'] != 'FREE_FORWARD_TRIAL_PREP' or runtime['forward_trial_enabled']:
        raise PermissionError('PREP-only prototype')
    dest.mkdir(parents=True, exist_ok=False)
    ticker = yf.Ticker(code + '.T')
    yf.config.debug.hide_exceptions = False
    new = ticker.history(start=str(start), end=str(end), interval='1d', auto_adjust=False, actions=True, repair=False, keepna=True, timeout=15)
    new.to_csv(dest / 'auto_adjust_false.csv', mode='x')
    payload = {'retrieved_at': datetime.now(timezone.utc).isoformat(), 'start': str(start), 'end_exclusive': str(end),
               'quality': validate(new), 'merge_status': 'NOT_MERGED_REVIEW_REQUIRED',
               'note': 'New actions/corrections can revise prior adjustment bases. Never concatenate adjusted vintages blindly.'}
    with (dest / 'metadata.json').open('x') as file:
        json.dump(payload, file, indent=2)
    return {'status': 'EMPTY_RESPONSE' if new.empty else 'RESEARCH_INCREMENT_ONLY', 'network_requests': 1, 'path': str(dest)}


def summary_only(records):
    """Small downstream payload, without raw history or unapproved baseline formulas."""
    return {'record_count': len(records), 'latest_completed': records[-1] if records else None,
            'conflict_count': sum(r['quality_state'] == 'DATA_CONFLICT' for r in records),
            'admission': 'RESEARCH_ONLY', 'forecast_enabled': False,
            'derived_statistics': None, 'statistics_note': 'ATR/threshold/baseline conventions require separate approval.'}

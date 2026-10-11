"""Offline JPX gap staging; never fetches or modifies source PDFs."""
import json
import math
import statistics
from pathlib import Path

from engine import minimal_trial as base

ROOT = base.ROOT
MISSING_ALLOWLIST = ('2026-09-14', '2026-09-16', '2026-09-17', '2026-09-24')
EXISTING_SESSIONS = ('2026-09-15', '2026-09-18')
GAP_START = '2026-09-11'
GAP_THROUGH = '2026-09-24'


def expected_gap_sessions(calendar):
    sessions = calendar['jp_sessions']
    if sessions != sorted(set(sessions)):
        raise ValueError('JPX_CALENDAR_INVALID')
    expected = [day for day in sessions if GAP_START < day <= GAP_THROUGH]
    if expected != sorted(MISSING_ALLOWLIST + EXISTING_SESSIONS):
        raise ValueError('JPX_CALENDAR_GAP_CONFLICT')
    return expected


def plan_one_shot_backfill(session, calendar, manifest, inbox, successes_today=0,
                           backfills_today=0, stopped=False, published_sessions=()):
    expected_gap_sessions(calendar)
    if session not in MISSING_ALLOWLIST:
        raise ValueError('SESSION_NOT_IN_EXACT_ALLOWLIST')
    if session not in published_sessions:
        raise ValueError('SESSION_NOT_VISIBLY_PUBLISHED')
    if stopped:
        raise ValueError('STOP_FOR_THE_DAY')
    if successes_today >= 2 or backfills_today >= 1:
        raise ValueError('DAILY_OR_BACKFILL_CAP')
    registered = {entry['session_date'] for entry in manifest['entries']}
    raw = Path(inbox) / ('stq_' + session.replace('-', '') + '.pdf')
    if session in registered or raw.exists():
        raise ValueError('DUPLICATE_DOWNLOAD_BLOCKED')
    if any(day < session and day in MISSING_ALLOWLIST and day not in registered and
           not (Path(inbox) / ('stq_' + day.replace('-', '') + '.pdf')).exists()
           for day in MISSING_ALLOWLIST):
        raise ValueError('OLDEST_MISSING_FIRST')
    return {'session_date': session, 'acquisition_type': 'BACKFILL',
            'mode': 'EXPLICIT_ONE_SHOT_ONLY'}


def stage_existing_pdf(parsed_path, manifest, calendar, inbox, output, *, allowed_sessions=None):
    """Create a separate candidate layer from an already parsed official file."""
    allowed = expected_gap_sessions(calendar) if allowed_sessions is None else list(allowed_sessions)
    if (allowed != sorted(set(allowed)) or not allowed
            or any(day not in calendar['jp_sessions'] for day in allowed)):
        raise ValueError('JPX_CALENDAR_GAP_CONFLICT')
    parsed_path = Path(parsed_path)
    parsed = json.loads(parsed_path.read_text(encoding='utf-8'))
    day = parsed.get('session_date')
    if (day not in allowed or parsed.get('normalized_schema_validation') != 'PASS'
            or parsed.get('manual_file_integrity') != 'PASS' or parsed.get('duplicate_count') != 0):
        raise ValueError('UNAPPROVED_OR_UNVALIDATED_PARSED_SESSION')
    entries = [entry for entry in manifest['entries'] if entry['session_date'] == day]
    if len(entries) != 1 or entries[0]['parser_status'] != 'PASS':
        raise ValueError('MANIFEST_NOT_VALIDATED')
    entry = entries[0]
    source = Path(inbox) / entry['filename']
    if (not source.is_file() or base.sha256(source) != entry['sha256']
            or parsed['source_file_hash'] != entry['sha256']
            or parsed.get('source_file_hash_after') != entry['sha256']):
        raise ValueError('RAW_PDF_HASH_CONFLICT')
    records = parsed['records']
    if len(records) != len(base.CORE) or {r['ticker'] for r in records} != set(base.CORE):
        raise ValueError('CORE6_OR_DUPLICATE_CONFLICT')
    rows = []
    for record in records:
        if record['session_date'] != day:
            raise ValueError('SESSION_CONFLICT')
        o, h, l, c = (record[key] for key in ('open', 'high', 'low', 'close'))
        v = record['volume']
        if (not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (o, h, l, c, v))
                or l <= 0 or v < 0 or h < l or not l <= o <= h or not l <= c <= h):
            raise ValueError('OHLCV_INVALID')
        rows.append({'session_date': day, 'ticker': record['ticker'],
                     'open': o, 'high': h, 'low': l, 'close': c, 'volume': v,
                     'source': 'JPX_DAILY_REPORT', 'source_hash': entry['sha256'],
                     'parsed_at': parsed.get('retrieved_at') or entry.get('parsed_at'),
                     'quality_state': 'PARSED_SCHEMA_PASS_ADJUSTMENT_BASIS_UNVERIFIED'})
    layer = {'status': 'OFFICIAL_GAP_CANDIDATE_NOT_FORECAST_INPUT',
             'session_date': day, 'source': 'JPX_DAILY_REPORT',
             'source_file': entry['filename'], 'source_hash': entry['sha256'],
             'parsed_output': str(parsed_path), 'parsed_output_hash': base.sha256(parsed_path),
             'core6_count': 6, 'records': sorted(rows, key=lambda row: row['ticker'])}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        json.dump(layer, stream, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return layer


def continuity(calendar, layers, through=GAP_THROUGH):
    expected = [day for day in expected_gap_sessions(calendar) if day <= through]
    seen = {}
    for layer in layers:
        day = layer['session_date']
        if day in seen or day not in expected or layer['source'] != 'JPX_DAILY_REPORT' or layer['core6_count'] != 6:
            raise ValueError('GAP_LAYER_DUPLICATE_OR_SOURCE_CONFLICT')
        seen[day] = layer
    missing = [day for day in expected if day not in seen]
    return {'status': 'GAP_REMAINS' if missing else 'CONTINUOUS',
            'through': through, 'expected_sessions': expected,
            'present_sessions': sorted(seen), 'missing_sessions': missing,
            'interpolation': False,
            'provenance': {day: seen[day]['source_hash'] for day in sorted(seen)}}


def merge_price_series(history_rows, layers, calendar, ticker, *, basis_compatible=False):
    """Virtual merge only; never writes into or interpolates historical CSV."""
    if ticker not in base.CORE or len(history_rows) < 61 or history_rows[-1]['session_date'] != GAP_START:
        raise ValueError('CACHE_BOUNDARY_INVALID')
    if any(row['issues'] for row in history_rows[-61:]):
        raise ValueError('CURRENT_HISTORY_WINDOW_INVALID')
    state = continuity(calendar, layers)
    if state['status'] != 'CONTINUOUS':
        return {'status': 'GAP_REMAINS', 'missing_sessions': state['missing_sessions'],
                'rows': [], 'provenance': state['provenance']}
    if not basis_compatible:
        return {'status': 'SOURCE_ADJUSTMENT_BASIS_UNVERIFIED', 'missing_sessions': [],
                'rows': [], 'provenance': state['provenance']}
    merged = [{'session_date': row['session_date'], 'close': row['values']['close'],
               'source': 'HISTORICAL_LOCAL_CACHE'} for row in history_rows[-61:]]
    for layer in sorted(layers, key=lambda entry: entry['session_date']):
        matches = [row for row in layer['records'] if row['ticker'] == ticker]
        if len(matches) != 1 or matches[0]['source_hash'] != layer['source_hash']:
            raise ValueError('GAP_TICKER_PROVENANCE_CONFLICT')
        merged.append({'session_date': layer['session_date'], 'close': matches[0]['close'],
                       'source': 'JPX_DAILY_REPORT', 'source_hash': layer['source_hash']})
    return {'status': 'CONTINUOUS', 'missing_sessions': [], 'rows': merged,
            'provenance': state['provenance']}


def candidate_features(history_rows, layers, calendar, ticker, threshold):
    """Offline arithmetic only; unverified cross-source basis is never forecast eligible."""
    if ticker not in base.CORE or len(history_rows) < 61:
        raise ValueError('CACHE_WINDOW_TOO_SHORT')
    if history_rows[-1]['session_date'] != GAP_START or any(r['issues'] for r in history_rows[-61:]):
        raise ValueError('CACHE_BOUNDARY_OR_QUALITY_INVALID')
    if threshold['ticker'] != ticker or threshold['training_end_date'] != GAP_START:
        raise ValueError('THRESHOLD_CACHE_MISMATCH')
    state = continuity(calendar, layers)
    if state['status'] != 'CONTINUOUS':
        raise ValueError('GAP_REMAINS')
    ordered = sorted(layers, key=lambda item: item['session_date'])
    dates = [r['session_date'] for r in history_rows[-61:]] + state['present_sessions']
    closes = [r['values']['close'] for r in history_rows[-61:]]
    for layer in ordered:
        matches = [r for r in layer['records'] if r['ticker'] == ticker]
        if len(matches) != 1 or matches[0]['source_hash'] != layer['source_hash']:
            raise ValueError('GAP_TICKER_PROVENANCE_CONFLICT')
        closes.append(matches[0]['close'])
    if dates != sorted(set(dates)) or any(not isinstance(c, (int, float)) or
                                          not math.isfinite(c) or c <= 0 for c in closes):
        raise ValueError('VIRTUAL_SERIES_INVALID')
    returns = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - 20, len(closes))]
    ma20, ma60 = statistics.mean(closes[-20:]), statistics.mean(closes[-60:])
    features = {'ret_1d': returns[-1], 'ret_5d': closes[-1] / closes[-6] - 1,
                'momentum_20d': closes[-1] / closes[-21] - 1,
                'momentum_60d': closes[-1] / closes[-61] - 1,
                'realized_vol_20d': statistics.stdev(returns), 'MA20': ma20, 'MA60': ma60,
                'MA_state': 'BULL' if ma20 > ma60 else 'BEAR' if ma20 < ma60 else 'NEUTRAL',
                'threshold': threshold['T_final']}
    return {'ticker': ticker, 'as_of_session': dates[-1], 'features': features,
            'status': 'CANDIDATE_SOURCE_BASIS_UNVERIFIED', 'forecast_eligible': False,
            'historical_cache_last_session': GAP_START,
            'official_gap_sessions': state['present_sessions'],
            'official_source_hashes': state['provenance'],
            'threshold_version': threshold['threshold_version'],
            'boundary_return_09_11_to_09_14': closes[-6] / closes[-7] - 1}

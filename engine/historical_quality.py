"""Offline quarantine and candidate training for existing Japan Core cache."""
import csv
import json
import math
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from engine import minimal_trial as engine

ROOT = engine.ROOT
POLICY_PATH = ROOT / 'config/historical_data_quality_policy.json'
OHLC_REASONS = {'MISSING_OHLC', 'NONFINITE_PRICE_OR_VOLUME', 'NEGATIVE_PRICE',
                'NONPOSITIVE_OPEN_OR_CLOSE', 'HIGH_BELOW_LOW', 'OPEN_ABOVE_HIGH',
                'OPEN_BELOW_LOW', 'CLOSE_ABOVE_HIGH', 'CLOSE_BELOW_LOW'}


def policy():
    return json.loads(POLICY_PATH.read_text(encoding='utf-8'))


def _number(raw, field, issues):
    value = raw.get(field)
    if value is None or value.strip() == '':
        issues.add('MISSING_VOLUME' if field == 'Volume' else 'MISSING_OHLC')
        return None
    try:
        number = float(value)
    except ValueError:
        issues.add('NONFINITE_PRICE_OR_VOLUME')
        return None
    if not math.isfinite(number):
        issues.add('NONFINITE_PRICE_OR_VOLUME')
        return None
    if number < 0:
        issues.add('NEGATIVE_VOLUME' if field == 'Volume' else 'NEGATIVE_PRICE')
    return number


def audit(ticker, cache=engine.CACHE, detected_at=None, rules=None):
    if ticker not in engine.CORE:
        raise ValueError('CORE_ONLY')
    rules = policy() if rules is None else rules
    detected_at = datetime.now(timezone.utc).isoformat() if detected_at is None else detected_at
    path = Path(cache) / f'{ticker}_auto_adjust_false.csv'
    meta_path = Path(cache) / f'{ticker}_probe.json'
    meta = json.loads(meta_path.read_text(encoding='utf-8'))
    identity = meta['metadata']
    if (identity['symbol'] != ticker + '.T' or identity['currency'] != 'JPY'
            or identity['exchangeTimezoneName'] != 'Asia/Tokyo'):
        raise ValueError('CACHE_IDENTITY_CONFLICT')
    source_hash = engine.sha256(path)
    records = []
    previous_date = None
    with path.open(newline='', encoding='utf-8') as stream:
        for line_number, raw in enumerate(csv.DictReader(stream), 2):
            issues, warnings = set(), set()
            stamp = None
            try:
                stamp = datetime.fromisoformat(raw.get('Date') or '')
                if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 9 * 3600:
                    issues.add('TIMEZONE_MISMATCH')
                if stamp.weekday() >= 5:
                    issues.add('WEEKEND_SESSION')
            except ValueError:
                issues.add('DATE_INVALID')
            session = stamp.date().isoformat() if stamp else None
            if session and previous_date and session < previous_date:
                issues.add('DATE_ORDER')
            if session:
                previous_date = session
            values = {name.lower(): _number(raw, name, issues)
                      for name in ('Open', 'High', 'Low', 'Close', 'Volume')}
            if all(values[k] is not None for k in ('open', 'high', 'low', 'close')):
                o, h, l, c = (values[k] for k in ('open', 'high', 'low', 'close'))
                tolerance = rules['numeric_tolerance']
                if h < l:
                    issues.add('HIGH_BELOW_LOW')
                if o > h + tolerance:
                    issues.add('OPEN_ABOVE_HIGH')
                if o < l - tolerance:
                    issues.add('OPEN_BELOW_LOW')
                if c > h + tolerance:
                    issues.add('CLOSE_ABOVE_HIGH')
                if c < l - tolerance:
                    issues.add('CLOSE_BELOW_LOW')
                if o <= 0 or c <= 0:
                    issues.add('NONPOSITIVE_OPEN_OR_CLOSE')
            if values['volume'] == 0:
                issues.add('ZERO_VOLUME')
            try:
                split = float(raw.get('Stock Splits') or 0)
            except ValueError:
                split = None
                warnings.add('SPLIT_FIELD_UNPARSEABLE')
            records.append({'ticker': ticker, 'session_date': session, 'line_number': line_number,
                            'raw': {key: raw.get(key) for key in ('Date', 'Open', 'High', 'Low', 'Close', 'Volume')},
                            'values': values, 'issues': issues, 'warnings': warnings,
                            'split_event': split is not None and split != 0})
    counts = Counter(r['session_date'] for r in records if r['session_date'])
    for row in records:
        if row['session_date'] and counts[row['session_date']] > 1:
            row['issues'].add('DUPLICATE_SESSION')
    split_indices = [i for i, row in enumerate(records) if row['split_event']]
    for i, row in enumerate(records):
        if any(abs(i-action) <= rules['corporate_action_proximity_observed_sessions'] for action in split_indices):
            row['warnings'].add('SPLIT_PROXIMITY')
        if i and not row['issues'] and not records[i-1]['issues']:
            prior = records[i-1]['values']['close']
            current = row['values']['close']
            if prior and current and abs(current/prior-1) >= rules['extreme_close_to_close_change_candidate_abs']:
                row['warnings'].add('EXTREME_DISCONTINUITY_CANDIDATE')
    invalid = [r for r in records if r['issues']]
    valid = [r for r in records if not r['issues']]
    quarantine = []
    for row in invalid:
        state = ('INVALID_OHLC' if row['issues'] & OHLC_REASONS else
                 'DUPLICATE_SESSION' if 'DUPLICATE_SESSION' in row['issues'] else 'INVALID_DATA')
        quarantine.append({'ticker': ticker, 'session_date': row['session_date'],
                           'quality_state': state, 'training_eligibility': 'EXCLUDED',
                           'reason': sorted(row['issues']), 'raw_ohlcv': row['raw'],
                           'source': 'YAHOO_FINANCE_YFINANCE_LOCAL_CACHE',
                           'source_file': str(path), 'source_file_sha256': source_hash,
                           'line_number': row['line_number'], 'detected_at': detected_at,
                           'external_verification': 'CANDIDATE_NOT_EXECUTED'})
    last_window = records[-rules['current_feature_window_rows']:]
    current_valid = len(last_window) == rules['current_feature_window_rows'] and all(not r['issues'] for r in last_window)
    source_review = (len(invalid) > 1 or any(r in last_window for r in invalid)
                     or len(valid) < rules['minimum_valid_training_rows'])
    summary = {'ticker': ticker, 'total_rows': len(records), 'valid_rows': len(valid),
               'excluded_rows': len(invalid), 'excluded_ratio': len(invalid)/len(records) if records else None,
               'excluded_dates': sorted(set(r['session_date'] for r in invalid if r['session_date'])),
               'earliest_valid_session': valid[0]['session_date'] if valid else None,
               'latest_valid_session': valid[-1]['session_date'] if valid else None,
               'current_60_session_window': 'PASS' if current_valid else 'FEATURE_INPUT_BLOCKED',
               'current_atr14_window': 'PASS' if len(records) >= 15 and all(not r['issues'] for r in records[-15:]) else 'FEATURE_INPUT_BLOCKED',
               'duplicate_rows': sum('DUPLICATE_SESSION' in r['issues'] for r in records),
               'missing_ohlc_rows': sum('MISSING_OHLC' in r['issues'] for r in records),
               'missing_volume_rows': sum('MISSING_VOLUME' in r['issues'] for r in records),
               'extreme_discontinuity_candidates': sum('EXTREME_DISCONTINUITY_CANDIDATE' in r['warnings'] for r in records),
               'split_proximity_rows': sum('SPLIT_PROXIMITY' in r['warnings'] for r in records),
               'calendar_coverage_status': rules['full_historical_jpx_calendar_status'],
               'source_sha256': source_hash,
               'data_gate': 'HISTORICAL_SOURCE_REVIEW_REQUIRED' if source_review else 'PASS_OBSERVED_ROWS',
               'training_policy': 'EXCLUDED_FROM_TRAINING_DUE_TO_DATA_QUALITY' if invalid else 'NO_EXCLUSIONS'}
    return {'ticker': ticker, 'records': records, 'quarantine': quarantine,
            'summary': summary, 'source_file': str(path), 'source_hash': source_hash,
            'source_metadata_hash': engine.sha256(meta_path), 'detected_at': detected_at}


def _valid_row(row):
    return dict(session_date=row['session_date'], **row['values'])


def threshold_candidate(result, rules=None, training_end_date=None):
    """Existing pure threshold calculation; caller controls purpose/persistence."""
    rules = policy() if rules is None else rules
    rows = result['records']
    if training_end_date is not None:
        if training_end_date not in {r['session_date'] for r in rows}:
            raise ValueError('TRAINING_END_NOT_CACHED_SESSION')
        rows = [r for r in rows if r['session_date'] and r['session_date'] <= training_end_date]
    invalid = [r for r in rows if r['issues']]
    if (len(invalid) > 1 or len(rows) < rules['current_feature_window_rows']
            or any(r['issues'] for r in rows[-rules['current_feature_window_rows']:])):
        raise ValueError('HISTORICAL_SOURCE_REVIEW_REQUIRED')
    valid = [_valid_row(r) for r in rows if not r['issues']]
    if len(valid) < rules['minimum_valid_training_rows']:
        raise ValueError('THRESHOLD_TRAINING_SAMPLE_INSUFFICIENT')
    last = [_valid_row(r) for r in rows[-rules['current_feature_window_rows']:]]
    recent_returns = engine.close_returns(last)
    recent_vol = statistics.stdev(recent_returns[-20:])
    rolling = []
    for i in range(20, len(rows)):
        window = rows[i-20:i+1]
        if any(r['issues'] for r in window):
            continue
        rolling.append(statistics.stdev(engine.close_returns([_valid_row(r) for r in window])))
    if not rolling:
        raise ValueError('NORMAL_VOLATILITY_UNAVAILABLE')
    normal_vol = statistics.median(rolling)
    if normal_vol <= 0:
        raise ValueError('NORMAL_VOLATILITY_INVALID')
    volatility_ratio = recent_vol/normal_vol
    adjust = abs(volatility_ratio-1) >= .30
    base = engine.percentile([abs(r['close']/r['open']-1) for r in valid], .33)
    atr = engine.atr14_pct(last) if adjust else None
    final = (base+atr)/2 if adjust else base
    if final <= 0:
        raise ValueError('THRESHOLD_INVALID')
    excluded_dates = sorted(set(r['session_date'] for r in invalid if r['session_date']))
    limit = {'ticker': result['ticker'], 'threshold_version': 'short-oc-p33-v1',
             'training_start_date': rows[0]['session_date'], 'training_end_date': rows[-1]['session_date'],
             'historical_window': [rows[0]['session_date'], rows[-1]['session_date']],
             'total_training_rows': len(rows), 'excluded_rows': len(invalid),
             'excluded_dates': excluded_dates, 'valid_training_rows': len(valid),
             'training_rows': len(valid), 'T_base': base, 'T_final': final,
             'recent_volatility': recent_vol, 'normal_volatility': normal_vol,
             'volatility_ratio': volatility_ratio, 'volatility_adjustment_used': adjust,
             'ATR_component': atr, 'normal_rolling_windows_used': len(rolling),
             'target': 'OPEN_TO_CLOSE', 'boundary': 'UP_GE_POSITIVE_DOWN_LE_NEGATIVE',
             'source_file_sha256': result['source_hash'],
             'quality_policy_sha256': engine.sha256(POLICY_PATH),
             'status': 'TRIAL_CANDIDATE_NOT_ACTIVATED'}
    return limit


def training_candidates(result, rules=None, training_end_date=None):
    rules = policy() if rules is None else rules
    limit = threshold_candidate(result, rules, training_end_date)
    rows = result['records']
    if training_end_date is not None:
        rows = [r for r in rows if r['session_date'] and r['session_date'] <= training_end_date]
    invalid = [r for r in rows if r['issues']]
    excluded_dates = sorted(set(r['session_date'] for r in invalid if r['session_date']))
    valid = [_valid_row(r) for r in rows if not r['issues']]
    last = [_valid_row(r) for r in rows[-rules['current_feature_window_rows']:]]
    final = limit['T_final']
    feature = engine.features(last, limit)
    labels = [engine.classify(r['close']/r['open']-1, final) for r in valid]
    base_rate = engine.empirical(labels)
    momentum_groups = {key: [] for key in ('POSITIVE', 'NEGATIVE', 'FLAT')}
    trend_groups = {key: [] for key in ('BULL', 'BEAR', 'NEUTRAL')}
    skipped_state_windows = 0
    for i in range(61, len(rows)):
        if rows[i]['issues']:
            continue
        prior = rows[i-61:i]
        if any(r['issues'] for r in prior):
            skipped_state_windows += 1
            continue
        state_rows = [_valid_row(r) for r in prior]
        target = _valid_row(rows[i])
        label = engine.classify(target['close']/target['open']-1, final)
        momentum = engine.momentum_state(state_rows[-1]['close']/state_rows[-21]['close']-1)
        momentum_groups[momentum].append(label)
        trend_groups[engine.trend_state(state_rows)].append(label)
    current_momentum = engine.momentum_state(last[-1]['close']/last[-21]['close']-1)
    current_trend = engine.trend_state(last)
    minimum = 30
    def conditional(groups, state):
        sample = groups[state]
        return {'probabilities': engine.empirical(sample) if len(sample) >= minimum else base_rate,
                'sample_count': len(sample), 'state': state, 'fallback': len(sample) < minimum,
                'excluded_target_rows': len(invalid), 'excluded_target_dates': excluded_dates}
    baselines = {'ticker': result['ticker'],
                 'base_rate': {'probabilities': base_rate, 'sample_count': len(labels), 'fallback': False},
                 'momentum': conditional(momentum_groups, current_momentum),
                 'ma_trend': conditional(trend_groups, current_trend),
                 'min_conditional_sample': minimum, 'target': 'OPEN_TO_CLOSE',
                 'training_end_date': rows[-1]['session_date'],
                 'excluded_target_rows': len(invalid),
                 'excluded_target_dates': excluded_dates,
                 'conditional_state_windows_skipped': skipped_state_windows,
                 'threshold_version': limit['threshold_version'],
                 'quality_policy_sha256': engine.sha256(POLICY_PATH),
                 'status': 'TRIAL_CANDIDATE_NOT_ACTIVATED'}
    return limit, baselines, feature


def write_candidate(path, payload):
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open('xb') as handle:
        handle.write(engine.canonical_bytes(payload))
    return dest

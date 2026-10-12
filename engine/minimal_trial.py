"""Offline, test-only Japan Short engine. No network, LLM, or trial entry point."""
import copy
import csv
import hashlib
import json
import math
import os
import re
import statistics
from collections import Counter
from datetime import date, datetime
from pathlib import Path, PureWindowsPath
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / 'analysis/provider_test_results/yfinance_cache_2026-09-13'
CORE = ('6857', '7203', '8306', '1605', '9433', '3197')
CLASSES = ('UP', 'FLAT', 'DOWN')
MARKET_FIELDS = ('japan_previous_market_move', 'sp500_overnight',
                 'nasdaq_overnight', 'usdjpy_change', 'major_rate_move',
                 'material_commodity_move')
REQUIRED_FEATURES = ('ret_1d', 'ret_5d', 'momentum_20d', 'momentum_60d',
                     'realized_vol_20d', 'MA20', 'MA60', 'MA_state', 'threshold')
ALLOWED_FEATURES = set(REQUIRED_FEATURES) | {
    'threshold_version', 'momentum_state',
    'previous_session', 'previous_open', 'previous_high', 'previous_low',
    'previous_close', 'previous_volume'}
FAILURE_TYPES = ('FEATURE_OMISSION', 'WEIGHTING', 'DIRECTION', 'TIMING_LAG',
                 'REGIME', 'EXPECTATION_GAP', 'NEW_INFORMATION', 'DATA_QUALITY',
                 'THRESHOLD', 'RESIDUAL_UNCERTAINTY', 'UNKNOWN')


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def available_by(value, freeze):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError('NAIVE_AVAILABILITY_TIME')
    return stamp <= freeze


def load_history(ticker, cache=CACHE):
    if ticker not in CORE:
        raise ValueError('CORE_ONLY')
    path = Path(cache) / f'{ticker}_auto_adjust_false.csv'
    meta_path = Path(cache) / f'{ticker}_probe.json'
    meta = json.loads(meta_path.read_text(encoding='utf-8'))
    identity = meta['metadata']
    if (identity['symbol'] != ticker + '.T' or identity['currency'] != 'JPY'
            or identity['exchangeTimezoneName'] != 'Asia/Tokyo'):
        raise ValueError('CACHE_IDENTITY_CONFLICT')
    rows = []
    with path.open(newline='', encoding='utf-8') as stream:
        for raw in csv.DictReader(stream):
            stamp = datetime.fromisoformat(raw['Date'])
            if stamp.utcoffset().total_seconds() != 9 * 3600:
                raise ValueError('CACHE_TIMEZONE_CONFLICT')
            row = {'session_date': stamp.date().isoformat()}
            for key in ('Open', 'High', 'Low', 'Close', 'Volume'):
                value = float(raw[key])
                if not math.isfinite(value) or value < 0:
                    raise ValueError(f'CACHE_INVALID_{ticker}_{row["session_date"]}_{key}')
                row[key.lower()] = value
            if row['open'] <= 0 or row['close'] <= 0 or row['volume'] <= 0:
                raise ValueError(f'CACHE_MISSING_PRICE_OR_VOLUME_{ticker}_{row["session_date"]}')
            if (row['low'] > min(row['open'], row['close']) + 1e-6
                    or row['high'] + 1e-6 < max(row['open'], row['close'])
                    or row['high'] < row['low']):
                raise ValueError(f'CACHE_OHLC_CONFLICT_{ticker}_{row["session_date"]}')
            if rows and row['session_date'] <= rows[-1]['session_date']:
                raise ValueError('CACHE_DUPLICATE_OR_UNORDERED')
            rows.append(row)
    if len(rows) < 100:
        raise ValueError('INSUFFICIENT_HISTORY')
    return rows, {'provider': 'YAHOO_FINANCE_YFINANCE', 'series_type': 'SPLIT_NORMALIZED_OHLC_CANDIDATE',
                  'eligibility': 'RESEARCH_ONLY', 'terms_status': 'UNRESOLVED',
                  'csv_sha256': sha256(path), 'metadata_sha256': sha256(meta_path),
                  'retrieved_at': meta['retrieved_at'], 'latest_cache_session': rows[-1]['session_date']}


def percentile(values, fraction):
    if not values:
        raise ValueError('EMPTY_DISTRIBUTION')
    ordered = sorted(values)
    point = (len(ordered) - 1) * fraction
    low = math.floor(point)
    return ordered[low] + (ordered[math.ceil(point)] - ordered[low]) * (point - low)


def close_returns(rows):
    return [rows[i]['close'] / rows[i-1]['close'] - 1 for i in range(1, len(rows))]


def atr14_pct(rows):
    if len(rows) < 15:
        raise ValueError('ATR_HISTORY_MISSING')
    ranges = [max(rows[i]['high'] - rows[i]['low'],
                  abs(rows[i]['high'] - rows[i-1]['close']),
                  abs(rows[i]['low'] - rows[i-1]['close']))
              for i in range(len(rows)-14, len(rows))]
    return statistics.mean(ranges) / rows[-1]['close']


def threshold(rows, version='short-oc-p33-v1'):
    returns = close_returns(rows)
    if len(returns) < 20:
        raise ValueError('VOLATILITY_HISTORY_MISSING')
    base = percentile([abs(r['close']/r['open']-1) for r in rows], .33)
    rolling = [statistics.stdev(returns[i-19:i+1]) for i in range(19, len(returns))]
    recent, normal = rolling[-1], statistics.median(rolling)
    if normal <= 0:
        raise ValueError('NORMAL_VOLATILITY_INVALID')
    adjusted = abs(recent / normal - 1) >= .30
    atr = atr14_pct(rows) if adjusted else None
    final = (base + atr) / 2 if adjusted else base
    if final <= 0:
        raise ValueError('THRESHOLD_INVALID')
    return {'status': 'ENGINE_TEST_ONLY',
            'threshold_version': version, 'training_end_date': rows[-1]['session_date'],
            'historical_window': [rows[0]['session_date'], rows[-1]['session_date']],
            'training_rows': len(rows), 'T_base': base, 'T_final': final,
            'recent_volatility_20d': recent, 'normal_volatility_median_20d': normal,
            'volatility_adjustment_used': adjusted, 'ATR_component': atr,
            'target': 'OPEN_TO_CLOSE', 'boundary': 'UP_GE_POSITIVE_DOWN_LE_NEGATIVE'}


def classify(ret, limit):
    if not math.isfinite(ret) or not math.isfinite(limit) or limit <= 0:
        raise ValueError('INVALID_RETURN_OR_THRESHOLD')
    return 'UP' if ret >= limit else 'DOWN' if ret <= -limit else 'FLAT'


def validate_frozen_threshold(frozen_threshold, as_of_session, ticker=None):
    """Completed-session PIT check; a frozen research candidate is not trial approval."""
    spec = json.loads((ROOT / 'config/threshold_spec.json').read_text(encoding='utf-8'))
    if frozen_threshold.get('threshold_version') != spec['version']:
        raise ValueError('THRESHOLD_VERSION_MISSING_OR_UNREGISTERED')
    if date.fromisoformat(frozen_threshold['training_end_date']) > date.fromisoformat(as_of_session):
        raise ValueError('THRESHOLD_TRAINING_LOOKAHEAD')
    limit = frozen_threshold.get('T_final')
    if type(limit) not in (int, float) or not math.isfinite(limit) or limit <= 0:
        raise ValueError('THRESHOLD_INVALID')
    if frozen_threshold.get('status') not in ('ENGINE_TEST_ONLY', 'TRIAL_CANDIDATE_NOT_ACTIVATED'):
        raise ValueError('THRESHOLD_NOT_FROZEN_RESEARCH_OR_TEST')
    if ticker is not None and frozen_threshold['status'] != 'ENGINE_TEST_ONLY':
        bundle = json.loads((ROOT / spec['candidate_bundle']).read_text(encoding='utf-8'))
        if (bundle.get('status') != 'CANDIDATE_FROZEN_PRETRIAL_NOT_ACTIVATED'
                or bundle.get('automatic_reestimation') is not False
                or bundle.get('threshold_version') != frozen_threshold['threshold_version']
                or bundle.get('thresholds', {}).get(ticker) != frozen_threshold):
            raise ValueError('FROZEN_THRESHOLD_BUNDLE_MISMATCH')


def features(rows, frozen_threshold):
    if len(rows) < 61:
        raise ValueError('FEATURE_HISTORY_OR_THRESHOLD_MISMATCH')
    validate_frozen_threshold(frozen_threshold, rows[-1]['session_date'])
    closes = [r['close'] for r in rows]
    returns = close_returns(rows)
    ma20, ma60 = statistics.mean(closes[-20:]), statistics.mean(closes[-60:])
    last = rows[-1]
    return {'previous_session': last['session_date'],
            **{f'previous_{key}': last[key] for key in ('open', 'high', 'low', 'close', 'volume')
               if key in last}, 'ret_1d': returns[-1],
            'ret_5d': closes[-1]/closes[-6]-1,
            'momentum_20d': closes[-1]/closes[-21]-1,
            'momentum_60d': closes[-1]/closes[-61]-1,
            'realized_vol_20d': statistics.stdev(returns[-20:]),
            'MA20': ma20, 'MA60': ma60,
            'MA_state': 'BULL' if ma20 > ma60 else 'BEAR' if ma20 < ma60 else 'NEUTRAL',
            'threshold': frozen_threshold['T_final'],
            'threshold_version': frozen_threshold['threshold_version']}


def momentum_state(value):
    return 'POSITIVE' if value > 0 else 'NEGATIVE' if value < 0 else 'FLAT'


def trend_state(rows):
    a = statistics.mean(r['close'] for r in rows[-20:])
    b = statistics.mean(r['close'] for r in rows[-60:])
    return 'BULL' if a > b else 'BEAR' if a < b else 'NEUTRAL'


def empirical(labels):
    counts = Counter(labels)
    return {key: counts[key]/len(labels) for key in CLASSES}


def baselines(rows, frozen_threshold, min_conditional=30):
    """Offline empirical training helper; forward use selects the frozen state tables."""
    if min_conditional < 1 or not rows:
        raise ValueError('BASELINE_TRAINING_MISMATCH')
    validate_frozen_threshold(frozen_threshold, rows[-1]['session_date'])
    outcomes = [classify(r['close']/r['open']-1, frozen_threshold['T_final']) for r in rows]
    base = empirical(outcomes)
    momentum_groups = {key: [] for key in ('POSITIVE', 'NEGATIVE', 'FLAT')}
    trend_groups = {key: [] for key in ('BULL', 'BEAR', 'NEUTRAL')}
    for i in range(61, len(rows)):
        prior = rows[:i]
        momentum_groups[momentum_state(prior[-1]['close']/prior[-21]['close']-1)].append(outcomes[i])
        trend_groups[trend_state(prior)].append(outcomes[i])
    current_momentum = momentum_state(rows[-1]['close']/rows[-21]['close']-1)
    current_trend = trend_state(rows)
    def conditional(groups, state):
        labels = groups[state]
        return {'probabilities': empirical(labels) if len(labels) >= min_conditional else base,
                'sample_count': len(labels), 'state': state,
                'fallback': len(labels) < min_conditional}
    return {'base_rate': {'probabilities': base, 'sample_count': len(outcomes), 'fallback': False},
            'momentum': conditional(momentum_groups, current_momentum),
            'ma_trend': conditional(trend_groups, current_trend),
            'min_conditional_sample': min_conditional, 'target': 'OPEN_TO_CLOSE',
            'threshold_version': frozen_threshold['threshold_version'],
            'training_end_date': rows[-1]['session_date']}


def compact_snapshot(target_session, expected_previous_session, freeze_time, stock_inputs,
                     market=None, news=None, metrics=None, *, allow_partial=False):
    """Validate complete stocks; partial_ticker owns partial admission and its ledger."""
    date.fromisoformat(target_session)
    date.fromisoformat(expected_previous_session)
    freeze = datetime.fromisoformat(freeze_time)
    if (freeze.tzinfo is None or freeze.astimezone(ZoneInfo('Asia/Tokyo')).date().isoformat() != target_session
            or freeze.astimezone(ZoneInfo('Asia/Tokyo')).time().isoformat() > '08:45:00'):
        raise ValueError('INVALID_SNAPSHOT_FREEZE_TIME')
    if expected_previous_session >= target_session:
        raise ValueError('INVALID_PREVIOUS_SESSION')
    if set(stock_inputs) - set(CORE) or (not allow_partial and set(stock_inputs) != set(CORE)):
        raise ValueError('CORE6_REQUIRED')
    market = {} if market is None else market
    news = {} if news is None else news
    if set(market) - set(MARKET_FIELDS) or set(news) - set(CORE):
        raise ValueError('UNKNOWN_MARKET_OR_NEWS_FIELD')
    common = {key: market.get(key) for key in MARKET_FIELDS}
    for datum in common.values():
        if datum is None:
            continue
        if (not isinstance(datum, dict) or set(datum) != {'value', 'available_at', 'source'}
                or not isinstance(datum['value'], (int, float)) or not math.isfinite(datum['value'])
                or not available_by(datum['available_at'], freeze)):
            raise ValueError('INVALID_OR_POST_FREEZE_MARKET_VALUE')
    compact = {}
    for ticker in CORE:
        if allow_partial and ticker not in stock_inputs:
            continue
        item = stock_inputs[ticker]
        if set(item) != {'features', 'threshold', 'baselines', 'provenance'}:
            raise ValueError('RAW_ROWS_OR_UNKNOWN_STOCK_INPUT')
        if set(item['features']) - ALLOWED_FEATURES:
            raise ValueError('RAW_ROWS_OR_UNKNOWN_FEATURE')
        if any(key not in item['features'] or item['features'][key] is None for key in REQUIRED_FEATURES):
            raise ValueError('MISSING_REQUIRED_FEATURE')
        if not item.get('provenance'):
            raise ValueError('MISSING_FEATURE_PROVENANCE')
        validate_frozen_threshold(item['threshold'], expected_previous_session, ticker)
        if item['features'].get('threshold_version') != item['threshold']['threshold_version']:
            raise ValueError('FEATURE_THRESHOLD_VERSION_MISMATCH')
        if item['features']['threshold'] != item['threshold']['T_final']:
            raise ValueError('FEATURE_THRESHOLD_VALUE_MISMATCH')
        if item['baselines'].get('threshold_version') != item['threshold']['threshold_version']:
            raise ValueError('BASELINE_THRESHOLD_VERSION_MISMATCH')
        if (item['features'].get('previous_session') != expected_previous_session
                or date.fromisoformat(item['baselines']['training_end_date'])
                > date.fromisoformat(expected_previous_session)):
            raise ValueError('LOOKAHEAD_STALE_OR_BASELINE_MISMATCH')
        if len(news.get(ticker, [])) > 3:
            raise ValueError('NEWS_BUDGET_EXCEEDED')
        event_keys = set()
        for event in news.get(ticker, []):
            if set(event) != {'event_type', 'event_time', 'available_at', 'summary', 'source', 'materiality'}:
                raise ValueError('NEWS_FIELDS_INVALID')
            if not event['summary'] or len(event['summary']) > 300:
                raise ValueError('NEWS_SUMMARY_INVALID')
            if not available_by(event['available_at'], freeze):
                raise ValueError('POST_FREEZE_NEWS')
            key = (event['event_type'], event['event_time'], event['summary'])
            if key in event_keys:
                raise ValueError('DUPLICATE_NEWS_EVENT')
            event_keys.add(key)
        compact[ticker] = copy.deepcopy(item)
    return {'status': 'ENGINE_TEST_ONLY', 'target_session': target_session,
            'expected_previous_session': expected_previous_session,
            'freeze_time': freeze_time,
            'market_common': common, 'stocks': compact,
            'news': {ticker: news.get(ticker, []) for ticker in compact},
            'metrics': metrics or {'llm_calls': 0, 'input_tokens': 'UNKNOWN',
                                   'output_tokens': 'UNKNOWN', 'browser_actions': 0,
                                   'provider_calls': 0, 'news_items': sum(len(x) for x in news.values()),
                                   'cross_checks': 0}}


def validate_forecast(record, snapshot):
    probe = record.get('status') == 'PIPELINE_PROBE'
    if record.get('status') not in ('ENGINE_TEST_ONLY', 'PIPELINE_PROBE') or record.get('ticker') not in CORE:
        raise ValueError('TEST_ONLY_CORE_FORECAST')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', record.get('forecast_id', '')) or not record.get('forecast_version'):
        raise ValueError('INVALID_FORECAST_ID_OR_VERSION')
    if record.get('target_session') != snapshot['target_session']:
        raise ValueError('FORECAST_SESSION_MISMATCH')
    if record.get('input_snapshot_hash') != hashlib.sha256(canonical_bytes(snapshot)).hexdigest():
        raise ValueError('SNAPSHOT_HASH_MISMATCH')
    stock = snapshot['stocks'][record['ticker']]
    if record.get('threshold_version') != stock['threshold']['threshold_version']:
        raise ValueError('THRESHOLD_VERSION_MISSING_OR_MISMATCH')
    if probe:
        required = {'schema_version', 'created_at', 'forecast_cutoff', 'feature_cutoff',
                    'feature_end', 'model_version', 'baseline_version', 'input_hashes', 'threshold'}
        if (not required <= record.keys()
                or record['schema_version'] != 'one-session-pipeline-probe-v1'
                or record['feature_end'] != snapshot['expected_previous_session']
                or record['threshold'] != stock['threshold']
                or record['baseline_version'] != stock['baselines'].get('baseline_version')
                or not record['input_hashes']
                or any(not re.fullmatch('[a-f0-9]{64}', value) for value in record['input_hashes'].values())
                or set(record) & {'outcome', 'actual_class', 'actual_open', 'actual_close', 'score'}):
            raise ValueError('PROBE_FROZEN_METADATA_REQUIRED')
        cutoff, feature_cutoff = (datetime.fromisoformat(record[k])
                                  for k in ('forecast_cutoff', 'feature_cutoff'))
        if (cutoff.tzinfo is None or feature_cutoff.tzinfo is None
                or datetime.fromisoformat(record['created_at']).tzinfo is None
                or cutoff.astimezone(ZoneInfo('Asia/Tokyo')).isoformat()
                != record['target_session'] + 'T08:45:00+09:00'
                or feature_cutoff.astimezone(ZoneInfo('Asia/Tokyo')).date().isoformat() != record['feature_end']
                or datetime.fromisoformat(record['freeze_time']) > cutoff):
            raise ValueError('PROBE_CUTOFF_INVALID')
        if (record.get('eligibility') != 'NON_FORMAL'
                or record.get('exclude_from_trial_metrics') is not True
                or record.get('model_id') != 'BASE_RATE_PIPELINE_PROBE'
                or record.get('model_version') != 'base-rate-pipeline-probe-v1'):
            raise ValueError('PROBE_ISOLATION_REQUIRED')
        probabilities = {k: record.get('p_' + k.lower()) for k in CLASSES}
        if (any(type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1
                for p in probabilities.values())
                or not math.isclose(sum(probabilities.values()), 1, abs_tol=1e-12)
                or probabilities != stock['baselines']['base_rate']['probabilities']):
            raise ValueError('INVALID_PROBE_BASE_RATE')
        probs = [100 * probabilities[k] for k in CLASSES]
        if any(record.get(k) != p for k, p in zip(CLASSES, probs)):
            raise ValueError('PROBE_PROBABILITY_UNITS')
    else:
        probs = [record.get(key) for key in CLASSES]
        if any(type(p) is not int or p < 5 or p > 95 or p % 5 for p in probs) or sum(probs) != 100:
            raise ValueError('INVALID_PROBABILITIES')
    order = ('FLAT', 'UP', 'DOWN')
    winner = max(order, key=lambda key: (record[key], -order.index(key)))
    if record.get('most_likely_class') != winner:
        raise ValueError('CLASS_PROBABILITY_MISMATCH')
    if record.get('confidence') != ('HIGH' if max(probs) >= 60 else 'MEDIUM' if max(probs) >= 45 else 'LOW'):
        raise ValueError('CONFIDENCE_MISMATCH')
    if len(record.get('top_factors', [])) > 3:
        raise ValueError('TOO_MANY_FACTORS')
    freeze = datetime.fromisoformat(record['freeze_time'])
    if record['freeze_time'] != snapshot['freeze_time']:
        raise ValueError('SNAPSHOT_FORECAST_FREEZE_MISMATCH')
    for factor in record.get('top_factors', []):
        if (not factor.get('label') or factor.get('direction') not in CLASSES
                or datetime.fromisoformat(factor['evidence_timestamp']) > freeze):
            raise ValueError('INVALID_OR_POST_FREEZE_FACTOR')
    if (datetime.fromisoformat(record['created_at']) > datetime.fromisoformat(record['freeze_time'])
            or datetime.fromisoformat(record['freeze_time']).date().isoformat() != record['target_session']):
        raise ValueError('INVALID_FREEZE_TIME')


def write_frozen(record, snapshot, directory):
    validate_forecast(record, snapshot)
    dest = Path(directory) / (record['forecast_id'] + '.json')
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_bytes(record)
    with dest.open('xb') as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    seal = dest.with_suffix('.sha256')
    with seal.open('x', encoding='ascii') as handle:
        handle.write(hashlib.sha256(payload).hexdigest() + '\n')
        handle.flush()
        os.fsync(handle.fileno())
    return dest


def write_test_snapshot(snapshot, directory):
    if snapshot.get('status') != 'ENGINE_TEST_ONLY' or set(snapshot.get('stocks', {})) != set(CORE):
        raise ValueError('TEST_ONLY_CORE6_SNAPSHOT_REQUIRED')
    dest = Path(directory) / 'snapshot.json'
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_bytes(snapshot)
    with dest.open('xb') as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    with dest.with_suffix('.sha256').open('x', encoding='ascii') as handle:
        handle.write(hashlib.sha256(payload).hexdigest() + '\n')
    return dest


def write_test_threshold_bundle(thresholds, directory):
    if set(thresholds) != set(CORE):
        raise ValueError('CORE6_THRESHOLD_FREEZE_REQUIRED')
    versions = {entry['threshold_version'] for entry in thresholds.values()}
    dates = {entry['training_end_date'] for entry in thresholds.values()}
    if len(versions) != 1 or len(dates) != 1:
        raise ValueError('THRESHOLD_VERSION_OR_TRAINING_DATE_CONFLICT')
    payload = {'status': 'ENGINE_TEST_ONLY', 'thresholds': thresholds}
    dest = Path(directory) / 'threshold_bundle.json'
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open('xb') as handle:
        handle.write(canonical_bytes(payload))
        handle.flush()
        os.fsync(handle.fileno())
    return dest


def read_frozen(path):
    path = Path(path)
    if sha256(path) != path.with_suffix('.sha256').read_text(encoding='ascii').strip():
        raise ValueError('FORECAST_TAMPER_DETECTED')
    return json.loads(path.read_text(encoding='utf-8'))


def score_frozen(forecast_path, snapshot_path, outcome):
    snapshot = read_frozen(snapshot_path)
    forecast = read_frozen(forecast_path)
    validate_forecast(forecast, snapshot)
    return score(forecast, outcome, snapshot['stocks'][forecast['ticker']]['baselines'])


def outcome_from_jpx(parsed, ticker, forecast, limit):
    if parsed.get('normalized_schema_validation') != 'PASS' or parsed.get('core6_extraction') != '6/6':
        raise ValueError('PARSER_OR_CORE6_FAILURE')
    if parsed['session_date'] != forecast['target_session']:
        raise ValueError('OUTCOME_SESSION_MISMATCH')
    matches = [r for r in parsed['records'] if r['ticker'] == ticker]
    if len(matches) != 1 or matches[0]['session_date'] != forecast['target_session']:
        raise ValueError('OUTCOME_TICKER_MISSING_OR_DUPLICATE')
    row = matches[0]
    if row.get('series_type') != 'RAW_OHLC' or row.get('source_file_hash') != parsed.get('source_file_hash'):
        raise ValueError('OUTCOME_PROVENANCE_INVALID')
    if row['open'] <= 0 or row['close'] <= 0:
        raise ValueError('OUTCOME_PRICE_INVALID')
    ret = row['close']/row['open']-1
    return {'status': 'PIPELINE_PROBE' if forecast.get('status') == 'PIPELINE_PROBE' else 'ENGINE_TEST_ONLY',
            'forecast_id': forecast['forecast_id'],
            'ticker': ticker, 'target_session': parsed['session_date'],
            'actual_open': row['open'], 'actual_close': row['close'],
            'actual_return': ret, 'actual_class': classify(ret, limit),
            'source_file_hash': parsed['source_file_hash']}


def _resolve_jpx_raw_source(parsed):
    reference = parsed['source_file']
    expected_hash = parsed['source_file_hash']
    source = Path(reference)
    if source.is_file():
        if sha256(source) != expected_hash:
            raise ValueError('RAW_SOURCE_HASH_MISMATCH')
        return source
    if not PureWindowsPath(reference).is_absolute():
        raise ValueError('RAW_SOURCE_HASH_MISMATCH')

    # Relocated Windows references must be backed by the retained raw manifest.
    inbox = (ROOT / 'data/outcome_input/manual_jpx').resolve()
    manifest_path = inbox / 'retention_manifest.json'
    if not manifest_path.is_file():
        raise ValueError('RAW_SOURCE_HASH_MISMATCH')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    candidates = set()
    for entry in manifest['entries']:
        if (entry.get('sha256') != expected_hash
                or entry.get('session_date') != parsed.get('session_date')):
            continue
        metadata = entry.get('source_metadata') or {}
        for local_file in (entry.get('filename'), metadata.get('local_file')):
            if not isinstance(local_file, str) or not local_file:
                continue
            local_path = Path(local_file)
            if local_path.is_absolute() or PureWindowsPath(local_file).drive:
                continue
            candidate = (inbox / local_path).resolve()
            if candidate.is_relative_to(inbox) and candidate.is_file():
                candidates.add(candidate)
    matches = [candidate for candidate in candidates if sha256(candidate) == expected_hash]
    if len(matches) > 1:
        raise ValueError('RAW_SOURCE_AMBIGUOUS')
    if not matches:
        raise ValueError('RAW_SOURCE_HASH_MISMATCH')
    return matches[0]


def load_parsed_jpx(path):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError('MISSING_OUTCOME_PDF_OR_PARSED_OUTPUT')
    parsed = json.loads(path.read_text(encoding='utf-8'))
    _resolve_jpx_raw_source(parsed)
    return parsed


def score(forecast, outcome, baseline_snapshot):
    if (forecast['status'] not in ('ENGINE_TEST_ONLY', 'PIPELINE_PROBE')
            or outcome['status'] != forecast['status']
            or forecast['forecast_id'] != outcome['forecast_id']
            or forecast['target_session'] != outcome['target_session']
            or forecast['ticker'] != outcome['ticker']):
        raise ValueError('FORECAST_OUTCOME_LINK_MISMATCH')
    if forecast['status'] == 'PIPELINE_PROBE' and (
            forecast.get('eligibility') != 'NON_FORMAL'
            or forecast.get('exclude_from_trial_metrics') is not True):
        raise ValueError('PROBE_ISOLATION_REQUIRED')
    actual = outcome['actual_class']
    def brier(probabilities):
        return sum((probabilities[k]-(1 if k == actual else 0))**2 for k in CLASSES)
    model = {key: forecast['p_' + key.lower()] if forecast['status'] == 'PIPELINE_PROBE'
             else forecast[key]/100 for key in CLASSES}
    base_scores = {key: brier(baseline_snapshot[key]['probabilities'])
                   for key in ('base_rate', 'momentum', 'ma_trend')}
    bs = brier(model)
    result = {'status': forecast['status'], 'forecast_id': forecast['forecast_id'],
            'ticker': forecast['ticker'], 'target_session': outcome['target_session'],
            'actual_class': actual, 'brier': bs,
            'accuracy': int(forecast['most_likely_class'] == actual),
            'log_loss': -math.log(model[actual]) if model[actual] > 0 else None, 'baseline_brier': base_scores,
            'brier_skill_vs_base_rate': 1-bs/base_scores['base_rate'] if base_scores['base_rate'] else None}
    if model[actual] == 0:
        result['log_loss_reason'] = 'ZERO_PROBABILITY_NO_CLIPPING'
    if forecast['status'] == 'PIPELINE_PROBE':
        result.update(eligibility='NON_FORMAL', exclude_from_trial_metrics=True)
    return result


def failure_trigger(forecast, scored, baseline_snapshot, data_quality=False,
                    repeated_type_count=0, failure_type='UNKNOWN'):
    if failure_type not in FAILURE_TYPES:
        raise ValueError('INVALID_FAILURE_TYPE')
    missed = scored['accuracy'] == 0
    flags = []
    if missed and max(forecast[k] for k in CLASSES) >= 60:
        flags.append('HIGH_CONFIDENCE_MISS')
    if missed and scored['brier'] >= 1.0:
        flags.append('LARGE_BRIER')
    if missed and failure_type != 'UNKNOWN' and repeated_type_count >= 3:
        flags.append('RECURRING_TYPE')
    if missed and any(max(CLASSES, key=lambda k: baseline_snapshot[name]['probabilities'][k]) == scored['actual_class']
                      and scored['brier']-scored['baseline_brier'][name] >= .25
                      for name in ('base_rate', 'momentum', 'ma_trend')):
        flags.append('BASELINE_WIN_AI_MISS')
    if data_quality:
        flags.append('DATA_QUALITY_SUSPECTED')
    return {'status': 'ENGINE_TEST_ONLY', 'forecast_id': forecast['forecast_id'],
            'correctness': 'INCORRECT' if missed else 'CORRECT',
            'failure_type': failure_type if missed else None,
            'detailed_review_candidate': bool(flags), 'candidate_reasons': flags,
            'llm_review_executed': False}

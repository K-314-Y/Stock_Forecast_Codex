"""Offline, fail-closed Japan fresh-feature bridge; no forecast entry point."""
import hashlib
import hmac
import json
import math
import os
import statistics
import shutil
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from engine import historical_quality as quality
from engine import minimal_trial as base

ROOT = base.ROOT
FEATURE_NAMES = ('ret_1d', 'ret_5d', 'momentum_20d', 'momentum_60d',
                 'realized_vol_20d', 'MA20', 'MA60',
                 'MA_state', 'threshold')
MARKET_NAMES = ('japan_previous_market_move', 'sp500_overnight',
                'nasdaq_overnight', 'usdjpy_change', 'major_rate_move')
MATERIAL_EVENTS = {'EARNINGS', 'EARNINGS_REVISION', 'GUIDANCE', 'PROFIT_WARNING',
                   'MAJOR_CORPORATE_ACTION', 'MAJOR_FINANCING', 'REGULATORY',
                   'LITIGATION', 'M_AND_A', 'OPERATIONAL_INCIDENT'}
JST = ZoneInfo('Asia/Tokyo')
# Provider identities from the existing, audited Core6 connector responses.
CORE_ISINS = {'6857': 'JP3122400009', '7203': 'JP3633400001',
              '8306': 'JP3902900004', '1605': 'JP3294460005',
              '9433': 'JP3496400007', '3197': 'JP3396210001'}

# Independent checkpoints of the already audited acquisition records. These are
# not hashes supplied by a forecast caller or recomputed from mutable metadata.
LEGACY_RECEIPTS = {
    '2026-09-29_0820_0835.json': (
        'bigdata_freeze_time_observation_2026-09-29.json',
        'ed6f5b8b851955b607fe81e907e841e61ee1ba46953f0d15332e6b4da100a90f',
        'source_record_hash'),
    '2026-09-30_0820_0835.json': (
        'bigdata_freeze_time_observation_2_2026-09-30.json',
        '4d03d571159dbd1e72597873a0ab37eef191d6235eaf3593b75682c2325f3e35',
        'raw_record_hash'),
    '2026-10-01_live_dry_run_raw.json': (
        'one_session_live_dry_run_2026-10-01.json',
        '990092f573d221198056e95b9e6542fb4a81963546d88dd5fa636b44ce3fbe0b',
        'raw_file_sha256'),
}
CACHE_RECEIPT_HASHES = {
    '1605': 'c78c8dfde75b6f211e664816aa9083f1ff81685b95098cd9f8ca743d8f8511c9',
    '3197': '2bc4f1f5152568d3ab120fbc4f4610d5f8178bf2c350cc89956de416b9bd4c37',
    '6857': 'cd7739482a30295edf70a40f875dca9dc2b5f1a4f06e16032f2bbe834a45bc65',
    '7203': '1b5ea17fbef63bec43e0feab379f41e2181130f2a2f0893a64f23af8b624be85',
    '8306': '93a91d46192e26c802b5bd613492dc6b8132f5624c93975d8afac058713e3ead',
    '9433': '394f9cb1a03dde8fdaa8fadd81f03d7c0cf53ee9127aca09e61a8cdbb492f694',
}


def _receipt_now():
    return datetime.now(timezone.utc)


def _receipt_time(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError('NAIVE_BRIDGE_RECEIPT_TIME')
    return stamp


def _receipt_root():
    return base.ROOT / 'data/fresh_bridge/jp/acquisition_evidence'


def _receipt_key(create=False):
    """Producer-private local authentication key; never a caller argument.

    Trust boundary: the acquisition process's key, OS clock and code checkpoints.
    This does not defend against an administrator replacing the key/code/clock.
    Verification never creates a key or receipt, including for old observations.
    """
    root = _receipt_root()
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / '.producer-key'
    if create and not path.exists():
        # Publish a complete private key atomically. Parallel Core6 acquisitions
        # must never read a zero-length key created by another worker.
        fd, pending = tempfile.mkstemp(prefix='.pending-key-', dir=root)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(os.urandom(32))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(pending, path, follow_symlinks=False)
            except FileExistsError:
                pass
        finally:
            os.unlink(pending)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        if os.fstat(stream.fileno()).st_mode & 0o077:
            raise ValueError('BRIDGE_KEY_PERMISSIONS')
        key = stream.read()
    if len(key) != 32:
        raise ValueError('BRIDGE_KEY_INVALID')
    return key


def _overview(response):
    # Same Bigdata company_tearsheet/company_overview response used by the
    # existing provider test. Neither a new provider nor a price interpretation.
    if response.get('isError'):
        raise ValueError('BRIDGE_PROVIDER_RESPONSE_ERROR')
    return response['structuredContent']['company_overview']


def _bridge_source(overview, receipt):
    return dict(timestamp=overview['timestamp'], price=overview['price'],
                volume=overview.get('volume'), provider_id=overview['source']['provider_id'],
                exchange=overview['exchange'], currency=overview['currency'],
                retrieved_at=receipt.isoformat())


def capture_fresh_close(ticker, expected_session, fetch_existing):
    """Opt-in wrapper for the EXISTING ticker-bound connector invocation.

    The acquisition controller supplies the existing tool callable, not a
    timestamp. It must bind that call to ticker/entity as in the provider test.
    No scheduler, automatic call, retroactive receipt import or forecast here.
    Only this producer can authenticate new receipts. Files are create-only.
    """
    if ticker not in base.CORE:
        raise ValueError('CORE_ONLY')
    key = _receipt_key(create=True)
    started = _receipt_now()
    response = fetch_existing()
    completed = _receipt_now()
    if started.tzinfo is None or completed.tzinfo is None or completed < started:
        raise ValueError('BRIDGE_ACQUISITION_CLOCK_INVALID')
    # Copy exactly the response that completed, before extracting any prices.
    payload = base.canonical_bytes(response)
    overview = _overview(json.loads(payload))
    if overview.get('isin') != CORE_ISINS[ticker]:
        raise ValueError('BRIDGE_PROVIDER_TICKER_MISMATCH')
    source = _bridge_source(overview, completed)
    observation = normalize_observation(ticker, source, expected_session, completed.isoformat())
    session_close = _receipt_time(expected_session + 'T15:30:00+09:00')
    if (observation['freshness_state'] != 'FRESH' or completed < session_close
            or _receipt_time(overview['timestamp']) < session_close):
        raise ValueError('BRIDGE_UNVALIDATED_OR_STALE')
    receipt = dict(schema_version='fresh-bridge-receipt-v1', source_role='FORECAST_FEATURE',
                   source='BIGDATA/FMP', source_tool='bigdata_company_tearsheet',
                   source_section='company_overview', ticker=ticker, session=expected_session,
                   close=overview['price'], provider_timestamp=overview['timestamp'],
                   retrieval_started_at=started.isoformat(),
                   retrieval_completed_at=completed.isoformat(),
                   retrieved_at=completed.isoformat(), source_data_sha256=hashlib.sha256(payload).hexdigest())
    signature = hmac.new(key, base.canonical_bytes(receipt), hashlib.sha256).hexdigest()
    envelope = dict(receipt=receipt, hmac_sha256=signature)
    identifier = hashlib.sha256(base.canonical_bytes(envelope)).hexdigest()
    destination = _receipt_root() / identifier
    stage = Path(tempfile.mkdtemp(prefix='.pending-', dir=_receipt_root()))
    try:
        for name, data in [('source.json', payload), ('receipt.json', base.canonical_bytes(envelope))]:
            with (stage / name).open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        # Serialize commits using an exclusive reservation, including concurrent
        # same-content receipts. A partial .pending-* package is never admitted.
        reservation = _receipt_root() / (identifier + '.reserved')
        with reservation.open('xb'):
            if destination.exists():
                raise FileExistsError('BRIDGE_RECEIPT_CREATE_ONLY')
            os.rename(stage, destination)
        fd = os.open(_receipt_root(), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return dict(source_role='FORECAST_FEATURE', source_kind='FRESH_CLOSE_BRIDGE',
                path=str(destination / 'source.json'), sha256=receipt['source_data_sha256'],
                receipt=str(destination / 'receipt.json'), session_end=expected_session)


def verify_fresh_evidence(evidence, ticker, created, cutoff):
    """Return source-derived closes + all public hashes to seal in the snapshot."""
    path = Path(evidence['path']).resolve()
    raw = path.read_bytes()
    source_hash = hashlib.sha256(raw).hexdigest()
    if source_hash != evidence['sha256']:
        raise ValueError('INPUT_HASH_MISMATCH')
    hashes = {str(path): source_hash}
    if 'receipt' in evidence:
        receipt_path = Path(evidence['receipt']).resolve()
        package = receipt_path.parent
        if (package.parent != _receipt_root().resolve() or receipt_path.name != 'receipt.json'
                or path != package / 'source.json'):
            raise ValueError('UNTRUSTED_BRIDGE_RECEIPT')
        envelope_bytes = receipt_path.read_bytes()
        envelope = json.loads(envelope_bytes)
        receipt = envelope['receipt']
        expected = hmac.new(_receipt_key(), base.canonical_bytes(receipt), hashlib.sha256).hexdigest()
        if (not hmac.compare_digest(expected, envelope['hmac_sha256'])
                or package.name != hashlib.sha256(base.canonical_bytes(envelope)).hexdigest()):
            raise ValueError('BRIDGE_RECEIPT_AUTHENTICATION_FAILED')
        hashes[str(receipt_path)] = hashlib.sha256(envelope_bytes).hexdigest()
        if (receipt['source_role'] != 'FORECAST_FEATURE' or receipt['source'] != 'BIGDATA/FMP'
                or receipt['source_data_sha256'] != source_hash):
            raise ValueError('BRIDGE_SOURCE_BINDING_MISMATCH')
        if receipt['ticker'] != ticker or receipt['session'] != evidence['session_end']:
            raise ValueError('BRIDGE_TICKER_SESSION_MISMATCH')
        completed = _receipt_time(receipt['retrieval_completed_at'])
        started = _receipt_time(receipt['retrieval_started_at'])
        if _receipt_time(receipt['retrieved_at']) != completed or started > completed:
            raise ValueError('BRIDGE_RECEIPT_TIME_MISMATCH')
        overview = _overview(json.loads(raw))
        if overview['price'] != receipt['close'] or overview['timestamp'] != receipt['provider_timestamp']:
            raise ValueError('BRIDGE_SOURCE_BINDING_MISMATCH')
    else:
        # Old acquisition logs were not signed. Their independently audited raw
        # hashes are pinned in code; a self-hash supplied by a caller is insufficient.
        if (path.parent != (base.ROOT / 'data/fresh_bridge/jp/live_observations').resolve()
                or path.name not in LEGACY_RECEIPTS):
            raise ValueError('UNTRUSTED_BRIDGE_RECEIPT')
        report_name, report_hash, hash_field = LEGACY_RECEIPTS[path.name]
        report_path = base.ROOT / 'analysis/provider_test_results' / report_name
        report_bytes = report_path.read_bytes()
        if hashlib.sha256(report_bytes).hexdigest() != report_hash:
            raise ValueError('BRIDGE_RECEIPT_AUTHENTICATION_FAILED')
        report, record = json.loads(report_bytes), json.loads(raw)
        canonical = dict(record)
        canonical.pop('record_hash', None)
        anchored = source_hash if hash_field == 'raw_file_sha256' else hashlib.sha256(base.canonical_bytes(canonical)).hexdigest()
        if anchored != report[hash_field]:
            raise ValueError('BRIDGE_SOURCE_BINDING_MISMATCH')
        hashes[str(report_path.resolve())] = report_hash
        if record['expected_previous_session'] != evidence['session_end']:
            raise ValueError('BRIDGE_TICKER_SESSION_MISMATCH')
        if 'observations' in record:
            matches = [r for r in record['observations'] if r['ticker'] == ticker]
            if len(matches) != 1:
                raise ValueError('BRIDGE_TICKER_SESSION_MISMATCH')
            row = matches[0]
            overview = dict(timestamp=row['provider_timestamp'], price=row['provider_price'],
                            volume=row.get('provider_volume'), exchange=row['exchange'], currency=row['currency'],
                            source=dict(provider_id=row.get('underlying_provider', row.get('source_provider_metadata', {}).get('provider_id'))))
        else:
            matches = [r['value'] for r in record['tool_results'] if r['status'] == 'fulfilled' and r['value']['ticker'] == ticker]
            if len(matches) != 1:
                raise ValueError('BRIDGE_TICKER_SESSION_MISMATCH')
            row = matches[0]
            overview = _overview(row['response'])
        # Existing observations used a whole-second clock. Use the upper bound,
        # never an optimistic truncation, exactly as the existing validator did.
        clock = lambda value: datetime.strptime(value, '%Y-%m-%d %H:%M:%S UTC').replace(tzinfo=timezone.utc)
        started = clock(row['retrieval_started_at'])
        completed = clock(row['retrieval_completed_at']) + timedelta(seconds=1)
        if started > completed:
            raise ValueError('BRIDGE_RECEIPT_TIME_MISMATCH')
    if completed > created or completed >= cutoff:
        raise ValueError('POST_GENERATION_INPUT_EVIDENCE')
    for field in ('available_at', 'retrieved_at'):
        if field in evidence and _receipt_time(evidence[field]) != completed:
            raise ValueError('CALLER_RECEIPT_TIME_MISMATCH')
    observation = normalize_observation(ticker, _bridge_source(overview, completed),
                                        evidence['session_end'], created.isoformat())
    if 'isin' in overview and overview['isin'] != CORE_ISINS[ticker]:
        raise ValueError('BRIDGE_PROVIDER_TICKER_MISMATCH')
    session_close = _receipt_time(evidence['session_end'] + 'T15:30:00+09:00')
    if (observation['freshness_state'] != 'FRESH' or completed < session_close
            or _receipt_time(overview['timestamp']) < session_close):
        raise ValueError('BRIDGE_UNVALIDATED_OR_STALE')
    config_path = base.ROOT / 'config/fresh_feature_bridge.json'
    config_bytes = config_path.read_bytes()
    config = json.loads(config_bytes)
    if (config['provider'] != 'BIGDATA' or config['underlying_provider'] != 'FMP'
            or config['BIGDATA_FRESH_BRIDGE_TECHNICALLY_VALIDATED'] is not True
            or config['price_semantics'] != 'VALIDATED_AS_PREVIOUS_SESSION_CLOSE_FOR_MINIMAL_TRIAL'):
        raise ValueError('BRIDGE_UNVALIDATED_OR_STALE')
    hashes[str(config_path.resolve())] = hashlib.sha256(config_bytes).hexdigest()
    return [(observation['session_date'], observation['close_bridge'])], hashes


def verify_cached_evidence(evidence, ticker, created, cutoff):
    """Reuse ONLY the registered, frozen historical cache and its audited receipt."""
    cache = base.ROOT / 'analysis/provider_test_results/yfinance_cache_2026-09-13'
    path = cache / (ticker + '_auto_adjust_false.csv')
    metadata = cache / (ticker + '_probe.json')
    candidate_path = base.ROOT / 'config/trial_thresholds_candidate.json'
    if Path(evidence['path']).resolve() != path.resolve():
        raise ValueError('UNTRUSTED_HISTORICAL_CACHE')
    hashes = {str(p.resolve()): base.sha256(p) for p in (path, metadata, candidate_path)}
    if hashes[str(metadata.resolve())] != CACHE_RECEIPT_HASHES[ticker]:
        raise ValueError('CACHE_RECEIPT_HASH_MISMATCH')
    threshold = json.loads(candidate_path.read_bytes())['thresholds'][ticker]
    if (hashes[str(path.resolve())] != threshold['source_file_sha256']
            or hashes[str(path.resolve())] != evidence['sha256']):
        raise ValueError('INPUT_HASH_MISMATCH')
    policy_path = base.ROOT / 'config/historical_data_quality_policy.json'
    rules = json.loads(policy_path.read_bytes())
    hashes[str(policy_path.resolve())] = base.sha256(policy_path)
    audited = quality.audit(ticker, cache=cache, rules=rules)
    # Reuse the existing bridge's 61-row clean window. Old quarantined OHLC
    # rows outside that window are neither reintroduced nor repaired here.
    rows = audited['records'][-61:]
    if len(rows) != 61 or any(row['issues'] for row in rows):
        raise ValueError('FEATURE_INPUT_BLOCKED')
    if rows[-1]['session_date'] != threshold['training_end_date'] or rows[-1]['session_date'] != evidence['session_end']:
        raise ValueError('INPUT_SESSION_LOOKAHEAD')
    completed = _receipt_time(json.loads(metadata.read_bytes())['retrieved_at'])
    if completed > created or completed >= cutoff:
        raise ValueError('POST_GENERATION_INPUT_EVIDENCE')
    for field in ('available_at', 'retrieved_at'):
        if field in evidence and _receipt_time(evidence[field]) != completed:
            raise ValueError('CALLER_RECEIPT_TIME_MISMATCH')
    return [(row['session_date'], row['values']['close']) for row in rows], hashes


def expected_previous(calendar, target_session):
    sessions = calendar['jp_sessions']
    if target_session not in sessions or sessions != sorted(set(sessions)):
        raise ValueError('TARGET_NOT_VALIDATED_JPX_SESSION')
    index = sessions.index(target_session)
    if index == 0:
        raise ValueError('PREVIOUS_SESSION_OUTSIDE_FIXTURE')
    return sessions[index - 1]


def normalize_observation(ticker, source, expected_session, freeze_time):
    """Store a quote-like price, never assert that it is an official close."""
    if ticker not in base.CORE:
        raise ValueError('CORE_ONLY')
    freeze = datetime.fromisoformat(freeze_time)
    if freeze.tzinfo is None:
        raise ValueError('NAIVE_FREEZE')
    record = {'ticker': ticker, 'session_date': None, 'expected_session': expected_session,
              'close_bridge': None, 'volume_bridge': None, 'provider': 'BIGDATA',
              'upstream_provider': 'FMP', 'provider_timestamp': None,
              'retrieved_at': None, 'freshness_state': 'MISSING',
              'official_crosscheck_status': 'UNKNOWN', 'quality_state': 'UNVERIFIED_PRICE_SEMANTICS',
              'price_semantics': 'UNKNOWN', 'volume_semantics': 'UNKNOWN',
              'open': None, 'high': None, 'low': None, 'FRESH_BRIDGE_VALID': False}
    if source is None:
        return record
    stamp = datetime.fromisoformat(source['timestamp'])
    receipt = datetime.fromisoformat(source['retrieved_at']) if source.get('retrieved_at') else None
    if stamp.tzinfo is None or (receipt is not None and receipt.tzinfo is None):
        raise ValueError('NAIVE_PROVIDER_TIME')
    session = stamp.astimezone(JST).date().isoformat()
    record.update(session_date=session, provider_timestamp=source['timestamp'],
                  retrieved_at=source['retrieved_at'], close_bridge=source.get('price'),
                  volume_bridge=source.get('volume'))
    if source.get('provider_id') != 'FMP' or source.get('exchange') != 'JPX' or source.get('currency') != 'JPY':
        record['freshness_state'] = 'CONFLICT'
    elif not isinstance(source.get('price'), (int, float)) or isinstance(source.get('price'), bool) or not math.isfinite(source['price']) or source['price'] <= 0:
        record['freshness_state'] = 'MISSING'
    elif receipt is None or receipt > freeze or stamp > freeze:
        record['freshness_state'] = 'UNKNOWN'
    elif session < expected_session:
        record['freshness_state'] = 'STALE'
    elif session > expected_session:
        record['freshness_state'] = 'CONFLICT'
    else:
        record['freshness_state'] = 'FRESH'
    return record


def compare_official(observation, official):
    if official is None or official.get('session_date') != observation['session_date']:
        return 'UNKNOWN'
    if observation['close_bridge'] == official.get('close'):
        return 'EXACT_MATCH'
    return 'MISMATCH'


def live_features(history_rows, bridge_rows, expected_sessions, threshold):
    """Require every missing JPX session, valid cache window, and proven bridge semantics."""
    if len(history_rows) < 61 or not all(not row['issues'] for row in history_rows[-61:]):
        raise ValueError('FEATURE_INPUT_BLOCKED')
    last_cache = history_rows[-1]['session_date']
    if threshold['training_end_date'] != last_cache:
        raise ValueError('FROZEN_THRESHOLD_CACHE_MISMATCH')
    gap = [day for day in expected_sessions if day > last_cache]
    if not gap or set(bridge_rows) != set(gap):
        raise ValueError('BRIDGE_SESSION_GAP_OR_EXTRA')
    rows = [dict(session_date=r['session_date'], close=r['values']['close'])
            for r in history_rows[-61:]]
    for day in gap:
        item = bridge_rows[day]
        if (item['session_date'] != day or item['freshness_state'] != 'FRESH'
                or item['quality_state'] != 'VALIDATED_BRIDGE_SEMANTICS'):
            raise ValueError('BRIDGE_UNVALIDATED_OR_STALE')
        rows.append({'session_date': day, 'close': item['close_bridge']})
    closes = [r['close'] for r in rows]
    if any(not isinstance(x, (int, float)) or x <= 0 or not math.isfinite(x)
           for x in closes[-61:]):
        raise ValueError('INVALID_BRIDGE_NUMERIC')
    returns = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - 20, len(closes))]
    ma20, ma60 = statistics.mean(closes[-20:]), statistics.mean(closes[-60:])
    result = {'previous_session': gap[-1], 'previous_close': closes[-1],
              'ret_1d': returns[-1],
              'ret_5d': closes[-1] / closes[-6] - 1,
              'momentum_20d': closes[-1] / closes[-21] - 1,
              'momentum_60d': closes[-1] / closes[-61] - 1,
              'realized_vol_20d': statistics.stdev(returns),
              'MA20': ma20, 'MA60': ma60,
              'MA_state': 'BULL' if ma20 > ma60 else 'BEAR' if ma20 < ma60 else 'NEUTRAL',
              'threshold': threshold['T_final'],
              'threshold_version': threshold['threshold_version'],
              'momentum_state': base.momentum_state(closes[-1] / closes[-21] - 1)}
    assert 'ATR14_pct' not in result and 'volume_ratio_20d' not in result
    return result


def select_baselines(features, tables):
    return {'base_rate': tables['base_rate'],
            'momentum': tables['momentum'][features['momentum_state']],
            'ma_trend': tables['ma_trend'][features['MA_state']]}


def training_state_tables(audit_result, threshold, frozen_candidate):
    """Reproduce the fixed conditional training rule for every future live state."""
    rows = audit_result['records']
    if threshold['training_end_date'] != rows[-1]['session_date']:
        raise ValueError('TRAINING_DATE_MISMATCH')
    groups = {'momentum': {x: [] for x in ('POSITIVE', 'NEGATIVE', 'FLAT')},
              'ma_trend': {x: [] for x in ('BULL', 'BEAR', 'NEUTRAL')}}
    for i in range(61, len(rows)):
        if rows[i]['issues'] or any(r['issues'] for r in rows[i - 61:i]):
            continue
        prior = [dict(session_date=r['session_date'], **r['values']) for r in rows[i - 61:i]]
        target = rows[i]['values']
        label = base.classify(target['close'] / target['open'] - 1, threshold['T_final'])
        momentum = base.momentum_state(prior[-1]['close'] / prior[-21]['close'] - 1)
        groups['momentum'][momentum].append(label)
        groups['ma_trend'][base.trend_state(prior)].append(label)
    common = frozen_candidate['base_rate']
    minimum = frozen_candidate['min_conditional_sample']
    result = {'base_rate': common}
    for kind in ('momentum', 'ma_trend'):
        result[kind] = {}
        for state, labels in groups[kind].items():
            result[kind][state] = {'state': state, 'sample_count': len(labels),
                                   'fallback': len(labels) < minimum,
                                   'probabilities': base.empirical(labels) if len(labels) >= minimum
                                   else common['probabilities']}
        selected = frozen_candidate[kind]
        if result[kind][selected['state']]['sample_count'] != selected['sample_count'] or any(
                abs(result[kind][selected['state']]['probabilities'][key] - selected['probabilities'][key]) > 1e-12
                for key in base.CLASSES):
            raise ValueError('CANDIDATE_BASELINE_REPRODUCTION_CONFLICT')
    return result


def write_state_table_bundle(path, thresholds, candidates):
    if set(thresholds) != set(base.CORE) or set(candidates) != set(base.CORE):
        raise ValueError('CORE6_CANDIDATES_REQUIRED')
    tables = {}
    for ticker in base.CORE:
        audit_result = quality.audit(ticker)
        if audit_result['source_hash'] != thresholds[ticker]['source_file_sha256']:
            raise ValueError('HISTORICAL_CACHE_CHANGED')
        tables[ticker] = training_state_tables(audit_result, thresholds[ticker], candidates[ticker])
    bundle = {'status': 'CANDIDATE_FROZEN_PRETRIAL_NOT_ACTIVATED',
              'training_end_date': next(iter(thresholds.values()))['training_end_date'],
              'threshold_version': next(iter(thresholds.values()))['threshold_version'],
              'baseline_version': 'minimal-baselines-v1',
              'quality_policy_sha256': base.sha256(quality.POLICY_PATH),
              'automatic_reestimation': False, 'tables': tables}
    return quality.write_candidate(path, bundle)


def compact_live_snapshot(target_session, calendar, observations, history_by_ticker,
                          bridge_by_ticker, thresholds, tables, freeze_time, market=None, news=None,
                          bridge_semantics_validated=False, cost_status='UNKNOWN',
                          current_access_available=False, payment_or_upgrade_required=None):
    previous = expected_previous(calendar, target_session)
    market = {} if market is None else market
    news = {} if news is None else news
    if set(market) - set(MARKET_NAMES) or set(news) - set(base.CORE):
        raise ValueError('UNKNOWN_MARKET_OR_NEWS_KEY')
    freeze = datetime.fromisoformat(freeze_time)
    if (freeze.tzinfo is None or freeze.astimezone(JST).date().isoformat() != target_session
            or freeze.astimezone(JST).time().isoformat() > '08:45:00'):
        raise ValueError('INVALID_FREEZE_TIME')
    if cost_status == 'ADDITIONAL_COST_REQUIRED' or payment_or_upgrade_required is True:
        raise ValueError('ADDITIONAL_COST_REQUIRED')
    result = {'run_type': 'PREP_VALIDATION_ONLY', 'target_session': target_session,
              'expected_previous_session': previous, 'freeze_time': freeze_time,
              'market_common': {name: market.get(name, 'UNKNOWN') for name in MARKET_NAMES},
              'stocks': {}, 'ticker_status': {}, 'news': {}, 'cost_status': cost_status,
              'current_access_available': current_access_available,
              'payment_or_upgrade_required': payment_or_upgrade_required,
              'threshold_version': None, 'baseline_version': None}
    for name, datum in result['market_common'].items():
        if datum != 'UNKNOWN' and (not isinstance(datum, dict) or
                                   not base.available_by(datum['available_at'], freeze)):
            raise ValueError('POST_FREEZE_MARKET_DATA')
    for ticker in base.CORE:
        obs = observations.get(ticker)
        reason = None
        if obs is None or obs['freshness_state'] != 'FRESH':
            reason = 'STALE_MISSING_CONFLICT_OR_UNKNOWN'
        elif not bridge_semantics_validated:
            reason = 'BRIDGE_SEMANTICS_UNVALIDATED'
        elif not current_access_available or payment_or_upgrade_required is not False:
            reason = 'ACCESS_OR_PAYMENT_STATUS_UNVERIFIED'
        else:
            try:
                history = history_by_ticker[ticker]
                feature = live_features(history['records'], bridge_by_ticker[ticker],
                                        [s for s in calendar['jp_sessions'] if s <= previous],
                                        thresholds[ticker])
                selected = select_baselines(feature, tables[ticker])
                if any(len(news_item) > 3 or any(item['event_type'] not in MATERIAL_EVENTS or
                       not base.available_by(item['available_at'], freeze) for item in news_item)
                       for news_item in [news.get(ticker, [])]):
                    raise ValueError('NEWS_POLICY_VIOLATION')
                result['stocks'][ticker] = {'features': feature, 'baselines': selected,
                                            'provenance': {'historical_cache_sha256': history['source_hash'],
                                                           'bridge_provider': 'BIGDATA',
                                                           'bridge_session': previous}}
                result['news'][ticker] = news.get(ticker, [])
            except (KeyError, ValueError, IndexError, ZeroDivisionError) as exc:
                reason = f'FEATURE_INPUT_BLOCKED:{exc}'
        result['ticker_status'][ticker] = {'freshness_state': obs['freshness_state'] if obs else 'MISSING',
                                           'FRESH_BRIDGE_VALID': reason is None,
                                           'decision': 'INCLUDED' if reason is None else 'SKIP_FRESH_FEATURE',
                                           'reason': reason}
    valid = len(result['stocks'])
    result['run_status'] = 'COMPLETE' if valid == len(base.CORE) else f'PARTIAL_{len(base.CORE) - valid}_SKIPPED'
    if valid == 0:
        result['run_status'] = 'NO_VALID_TICKERS'
    result['included_count'] = valid
    result['skipped_count'] = len(base.CORE) - valid
    result['threshold_version'] = next(iter(thresholds.values()))['threshold_version']
    result['baseline_version'] = 'minimal-baselines-v1'
    result['snapshot_hash'] = hashlib.sha256(base.canonical_bytes(result)).hexdigest()
    return result


def write_dry_run_record(record, snapshot, directory):
    if (record.get('run_type') != 'LIVE_DRY_RUN_ONLY' or
            record.get('exclude_from_trial_metrics') is not True or
            record.get('ticker') not in snapshot.get('stocks', {}) or
            record.get('input_snapshot_hash') != snapshot.get('snapshot_hash')):
        raise ValueError('DRY_RUN_RECORD_GATE')
    name = record.get('forecast_id', '')
    if not name or not name.replace('-', '').replace('_', '').isalnum():
        raise ValueError('INVALID_FORECAST_ID')
    path = Path(directory) / (name + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = base.canonical_bytes(record)
    with path.open('xb') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return path

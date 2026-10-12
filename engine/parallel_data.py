"""Allowlisted derived data and one common PIT snapshot. No acquisition or raw IO."""
import copy
import hashlib
import json
import math
import re
from datetime import date, datetime

from engine import minimal_trial as core

CLASSES = ('UP', 'FLAT', 'DOWN')
PRIMARY = 'equal-linear-pool-v1'
LOGISTIC = 'multinomial-logistic-v1'
LIGHTGBM = 'lightgbm-multiclass-v1'
MODEL_ROLES = {PRIMARY: 'PRIMARY', LOGISTIC: 'SHADOW', LIGHTGBM: 'SHADOW'}
PRICE9 = tuple(core.REQUIRED_FEATURES)


def rules():
    return json.loads((core.ROOT / 'config/parallel_forward_trial_v1.json').read_text())


def digest(value):
    return hashlib.sha256(core.canonical_bytes(value)).hexdigest()


def specification_hash():
    """Freeze model specification independently of later execution activation gates."""
    config = rules()
    gates = {'task_mode', 'live_generation_enabled', 'outcome_scoring_enabled'}
    return digest({k: v for k, v in config.items() if k not in gates})


def stamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('TIMEZONE_REQUIRED')
    return result


def hash_valid(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value) is not None


def probability_valid(probabilities):
    if (set(probabilities) != set(CLASSES)
            or any(type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1
                   for p in probabilities.values())
            or not math.isclose(sum(probabilities.values()), 1, abs_tol=1e-12)):
        raise ValueError('INVALID_MULTICLASS_PROBABILITIES')


def validate_numeric(item, name, feature_end, cutoff, created_at, source_registry):
    """Numerical values never pass through Jev. Return status, never fix/fill."""
    fields = {'value', 'source', 'source_hash', 'timestamp', 'available_at', 'session', 'currency'}
    if set(item) != fields:
        raise ValueError('NUMERIC_METADATA_ALLOWLIST')
    source = source_registry.get(item['source'])
    if not source or source != {'validated': True, 'derived_persistence_allowed': True}:
        raise ValueError('SOURCE_NOT_VALIDATED_FOR_DERIVED_USE')
    if not hash_valid(item['source_hash']):
        raise ValueError('SOURCE_HASH_REQUIRED')
    date.fromisoformat(item['session'])
    observed, available = stamp(item['timestamp']), stamp(item['available_at'])
    if (observed > available or available > stamp(created_at) or available >= stamp(cutoff)
            or item['session'] > feature_end):
        raise ValueError('FEATURE_PIT_VIOLATION')
    if name in PRICE9 and (item['session'] != feature_end or item['currency'] != 'JPY'):
        raise ValueError('FEATURE_SESSION_OR_CURRENCY_INVALID')
    if name in PRICE9 and (observed.astimezone(core.ZoneInfo('Asia/Tokyo')).date().isoformat() != feature_end
                          or observed < stamp(feature_end + 'T15:30:00+09:00')):
        raise ValueError('FEATURE_SESSION_TIMESTAMP_INVALID')
    if item['currency'] not in ('JPY', 'USD', 'PERCENT', 'INDEX', 'COUNT', 'NONE'):
        raise ValueError('CURRENCY_REQUIRED')
    value = item['value']
    if value is None:
        return 'MISSING'
    if name == 'MA_state':
        if value not in ('BULL', 'BEAR', 'NEUTRAL'):
            raise ValueError('MA_STATE_INVALID')
    elif type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('NONFINITE_FEATURE')
    elif ((name in ('MA20', 'MA60', 'threshold') and value <= 0)
          or (name == 'realized_vol_20d' and value < 0)
          or (name in ('ret_1d', 'ret_5d', 'momentum_20d', 'momentum_60d') and value <= -1)
          or (name.endswith('_count') and (value < 0 or value != int(value)))):
        raise ValueError('FEATURE_ANOMALY')
    return 'PASS'


def candidate_metadata(name, *, acquired=False, validated=False):
    config = rules()
    candidates = set(PRICE9) | set(config['lightgbm_optional_candidates'])
    active = name in PRICE9
    allowed = [LOGISTIC, LIGHTGBM] if active else []
    return dict(acquired=bool(acquired), validated=bool(validated),
                candidate_feature=name in candidates,
                model_input=bool(active and acquired and validated),
                model_versions_allowed=allowed if acquired and validated else [])


def validate_numeric_candidates(records, feature_end, cutoff, created_at, source_registry):
    """Validate an acquired derived candidate batch before converting it to a dict."""
    seen, result = set(), []
    for record in records:
        if set(record) != {'ticker', 'feature', 'data'} or record['ticker'] not in core.CORE:
            raise ValueError('NUMERIC_BATCH_GRAIN_INVALID')
        key = (record['ticker'], record['feature'], record['data']['session'])
        if key in seen:
            raise ValueError('DUPLICATE_NUMERIC_CANDIDATE')
        seen.add(key)
        status = validate_numeric(record['data'], record['feature'], feature_end, cutoff, created_at, source_registry)
        result.append(dict(ticker=record['ticker'], feature=record['feature'], status=status,
                           **candidate_metadata(record['feature'], acquired=True, validated=status == 'PASS')))
    return result


def build_common_snapshot(target_session, feature_end, created_at, feature_metadata,
                          thresholds, baseline_tables, source_hashes, source_registry,
                          *, synthetic=False):
    """One snapshot for all three models; candidate features remain inactive."""
    config = rules()
    cutoff = target_session + 'T08:45:00+09:00'
    date.fromisoformat(target_session)
    date.fromisoformat(feature_end)
    if (feature_end >= target_session or stamp(created_at) >= stamp(cutoff)
            or stamp(created_at).astimezone(core.ZoneInfo('Asia/Tokyo')).date().isoformat() != target_session):
        raise ValueError('SNAPSHOT_TIME_INVALID')
    if (set(feature_metadata) != set(core.CORE) or set(thresholds) != set(core.CORE)
            or set(baseline_tables['tables']) != set(core.CORE)):
        raise ValueError('COMMON_SNAPSHOT_CORE6_REQUIRED')
    if (baseline_tables['baseline_version'] != config['baseline_version']
            or baseline_tables['threshold_version'] != config['threshold_version']
            or baseline_tables['training_end_date'] > feature_end):
        raise ValueError('BASELINE_VERSION_OR_LOOKAHEAD')
    if not source_hashes or any(not hash_valid(v) for v in source_hashes.values()):
        raise ValueError('SOURCE_HASHES_REQUIRED')
    if not synthetic:
        path = core.ROOT / 'config/trial_baseline_state_tables_candidate.json'
        if (source_hashes.get(str(path)) != core.sha256(path)
                or baseline_tables != json.loads(path.read_bytes())):
            raise ValueError('FROZEN_BASELINE_BUNDLE_MISMATCH')
    universe = set(PRICE9) | set(config['lightgbm_optional_candidates'])
    stocks = {}
    for ticker in core.CORE:
        meta = feature_metadata[ticker]
        threshold = thresholds[ticker]
        if not synthetic and threshold.get('status') != 'TRIAL_CANDIDATE_NOT_ACTIVATED':
            raise ValueError('REAL_SNAPSHOT_REQUIRES_FROZEN_THRESHOLD')
        core.validate_frozen_threshold(threshold, feature_end, None if synthetic else ticker)
        if not set(PRICE9) <= set(meta) or not set(meta) <= universe:
            raise ValueError('FEATURE_UNIVERSE_INVALID')
        statuses = {name: validate_numeric(item, name, feature_end, cutoff, created_at, source_registry)
                    for name, item in meta.items()}
        if any(statuses[n] != 'PASS' for n in PRICE9):
            raise ValueError('REQUIRED_FEATURE_MISSING')
        if meta['threshold']['value'] != threshold['T_final']:
            raise ValueError('FROZEN_THRESHOLD_MISMATCH')
        features = {n: meta[n]['value'] for n in PRICE9}
        momentum = core.momentum_state(features['momentum_20d'])
        table = baseline_tables['tables'][ticker]
        selected = {name: copy.deepcopy(entry['probabilities']) for name, entry in
                    [('base_rate', table['base_rate']),
                     ('momentum', table['momentum'][momentum]),
                     ('ma_trend', table['ma_trend'][features['MA_state']])]}
        for p in selected.values():
            probability_valid(p)
        stocks[ticker] = dict(features=features,
                              candidate_values={n: item['value'] for n, item in meta.items() if n not in PRICE9},
                              feature_metadata=copy.deepcopy(meta), missing_status=statuses,
                              selection={n: candidate_metadata(n, acquired=True, validated=statuses[n] == 'PASS')
                                         for n in meta},
                              threshold=copy.deepcopy(threshold), baselines=selected, PIT_status='PASS')
    result = dict(schema_version=config['snapshot_version'], target_session=target_session,
                  feature_end=feature_end, created_at=created_at, cutoff=cutoff,
                  available_at_rule='timestamp <= available_at <= created_at < cutoff',
                  source_hashes=copy.deepcopy(source_hashes), source_registry=copy.deepcopy(source_registry),
                  baseline_version=config['baseline_version'],
                  baseline_training_end=baseline_tables['training_end_date'],
                  threshold_version=config['threshold_version'],
                  config_hash=specification_hash(), synthetic=bool(synthetic), stocks=stocks)
    result['common_snapshot_hash'] = digest(result)
    return result


def validate_snapshot(snapshot):
    config = rules()
    expected = {'schema_version', 'target_session', 'feature_end', 'created_at', 'cutoff',
                'available_at_rule', 'source_hashes', 'source_registry', 'baseline_version',
                'baseline_training_end', 'threshold_version', 'config_hash', 'synthetic',
                'stocks', 'common_snapshot_hash'}
    if set(snapshot) != expected or snapshot['config_hash'] != specification_hash():
        raise ValueError('SNAPSHOT_SCHEMA_OR_CONFIG_CHANGED')
    raw = {k: v for k, v in snapshot.items() if k != 'common_snapshot_hash'}
    if snapshot['common_snapshot_hash'] != digest(raw):
        raise ValueError('COMMON_SNAPSHOT_HASH_MISMATCH')
    # Rebuild from allowlisted metadata to verify values, PIT and frozen baselines.
    tables = {ticker: dict(base_rate=dict(probabilities=s['baselines']['base_rate']),
                           momentum={state: dict(probabilities=s['baselines']['momentum'])
                                     for state in ('POSITIVE', 'NEGATIVE', 'FLAT')},
                           ma_trend={state: dict(probabilities=s['baselines']['ma_trend'])
                                     for state in ('BULL', 'BEAR', 'NEUTRAL')})
              for ticker, s in snapshot['stocks'].items()}
    baseline_tables = dict(tables=tables, baseline_version=snapshot['baseline_version'],
                           threshold_version=snapshot['threshold_version'],
                           training_end_date=snapshot['baseline_training_end'])
    if not snapshot['synthetic']:
        baseline_tables = json.loads((core.ROOT / 'config/trial_baseline_state_tables_candidate.json').read_bytes())
    rebuilt = build_common_snapshot(snapshot['target_session'], snapshot['feature_end'], snapshot['created_at'],
                                    {t: s['feature_metadata'] for t, s in snapshot['stocks'].items()},
                                    {t: s['threshold'] for t, s in snapshot['stocks'].items()},
                                    baseline_tables,
                                    snapshot['source_hashes'], snapshot['source_registry'],
                                    synthetic=snapshot['synthetic'])
    if rebuilt != snapshot:
        raise ValueError('SNAPSHOT_DERIVED_VALUES_CHANGED')


def common_from_execution(inputs, scope, target_session):
    """Future entry consumes Task 10B one-use inputs. Never replays stored snapshots."""
    from engine import forward_trial_inputs, fresh_bridge
    snapshot, audit = forward_trial_inputs.consume_execution_inputs(inputs, scope, target_session)
    now = fresh_bridge._receipt_now().isoformat()
    previous = snapshot['previous_session']
    thresholds, hashes = forward_trial_inputs._frozen_candidates(previous)
    hashes.update(inputs._source_hashes)
    hashes['ephemeral-derived-core6'] = digest(snapshot)
    path = core.ROOT / 'config/trial_baseline_state_tables_candidate.json'
    tables = json.loads(path.read_bytes())
    meta = {}
    for ticker, values in snapshot['stocks'].items():
        observed = audit['stocks'][ticker]
        if observed['PIT_validation'] != 'PASS':
            raise ValueError('EXECUTION_PIT_NOT_VALIDATED')
        meta[ticker] = {n: dict(value=values[n], source='TASK10B_EPHEMERAL_DERIVED',
                               source_hash=observed['feature_snapshot_sha256'],
                               timestamp=previous + 'T15:30:00+09:00',
                               available_at=observed['retrieved_at'], session=previous, currency='JPY')
                        for n in PRICE9}
    return build_common_snapshot(target_session, previous, now, meta, thresholds, tables,
                                 hashes, {'TASK10B_EPHEMERAL_DERIVED':
                                          dict(validated=True, derived_persistence_allowed=True)})


def training_rows(samples, target_session, training_as_of, *, synthetic=False):
    """Dataset grain = Core6 ticker × target session; exclude target/future sessions first."""
    rows, sessions = [], set()
    for sample in samples:
        snap = sample['snapshot']
        day = snap['target_session']
        date.fromisoformat(day)
        if day >= target_session:
            continue
        if day in sessions:
            raise ValueError('DUPLICATE_TRAINING_SESSION')
        sessions.add(day)
        validate_snapshot(snap)
        if snap['synthetic'] != synthetic or set(sample['outcomes']) != set(core.CORE):
            raise ValueError('DATASET_MODE_OR_GRAIN_INVALID')
        for ticker in core.CORE:
            out = sample['outcomes'][ticker]
            if set(out) != {'return_oc', 'available_at', 'source_hash'} or not hash_valid(out['source_hash']):
                raise ValueError('OUTCOME_METADATA_INVALID')
            available = stamp(out['available_at'])
            if (available < stamp(day + 'T15:30:00+09:00')
                    or available >= stamp(training_as_of)):
                raise ValueError('OUTCOME_NOT_AVAILABLE_FOR_TRAINING')
            stock = snap['stocks'][ticker]
            label = core.classify(out['return_oc'], stock['features']['threshold'])
            rows.append(dict(session=day, ticker=ticker, features=copy.deepcopy(stock['features']),
                             label=label, outcome_available_at=out['available_at'],
                             outcome_hash=out['source_hash'], cutoff=snap['cutoff'],
                             common_snapshot_hash=snap['common_snapshot_hash'],
                             baselines=copy.deepcopy(stock['baselines'])))
    if not rows:
        raise ValueError('EMPTY_TRAINING_DATASET')
    return sorted(rows, key=lambda r: (r['session'], core.CORE.index(r['ticker'])))


def expanding_folds(rows, *, minimum_train_sessions=None, validation_sessions=None, folds=None):
    rows = sorted(rows, key=lambda r: (r['session'], core.CORE.index(r['ticker'])))
    config = rules()['walk_forward']
    minimum = config['minimum_train_sessions'] if minimum_train_sessions is None else minimum_train_sessions
    width = config['validation_sessions'] if validation_sessions is None else validation_sessions
    count = config['folds'] if folds is None else folds
    if min(minimum, width, count) < 1:
        raise ValueError('INVALID_FOLD_CONFIG')
    days = sorted({row['session'] for row in rows})
    pairs = [(r['session'], r['ticker']) for r in rows]
    if len(pairs) != len(set(pairs)):
        raise ValueError('DUPLICATE_TRAINING_GRAIN')
    if any({r['ticker'] for r in rows if r['session'] == d} != set(core.CORE) for d in days):
        raise ValueError('CORE6_DATE_GROUP_REQUIRED')
    start = len(days) - width * count
    if start < minimum:
        raise ValueError('INSUFFICIENT_PIT_TRAINING_SESSIONS')
    result = []
    for i in range(count):
        boundary = start + i * width
        validation_days = set(days[boundary:boundary + width])
        validation = [r for r in rows if r['session'] in validation_days]
        first_cutoff = min(stamp(r['cutoff']) for r in validation)
        excluded = {r['session'] for r in rows if r['session'] < days[boundary]
                    and stamp(r['outcome_available_at']) >= first_cutoff}
        train = [r for r in rows if r['session'] < days[boundary] and r['session'] not in excluded]
        if len({r['session'] for r in train}) < minimum:
            raise ValueError('INSUFFICIENT_LABEL_AVAILABLE_TRAINING_SESSIONS')
        result.append((train, validation))
    return result

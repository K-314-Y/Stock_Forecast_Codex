"""Offline Core6 preparation. Only absent required inputs permit ticker skipping.

No acquisition, training, prediction, scoring execution, or persistent writes.
The strict legacy entry points retain their defaults; use these session entry
points to prepare partial inputs and retain their explicit coverage ledger.
"""
import copy
import json
import math
import re
from datetime import date, datetime
from pathlib import Path

from engine import minimal_trial as core
from engine.fresh_bridge import select_baselines

SKIPPED = 'SKIPPED_MISSING_INPUT'
STAGES = ('feature_generation', 'baseline', 'scoring', 'forecast_input')
PRICE_FIELDS = ('open', 'high', 'low', 'close', 'volume')


def _mapping(value):
    if not isinstance(value, dict):
        raise ValueError('INVALID_PARTIAL_INPUT_STRUCTURE')
    if value.get('security_challenge') or value.get('status') == 'SECURITY_CHALLENGE':
        raise ValueError('SECURITY_CHALLENGE')
    conflicts = ('CONFLICT', 'HASH_CONFLICT', 'PROVENANCE_CONFLICT', 'MISMATCH')
    if value.get('conflict') or any(value.get(key) in conflicts for key in (
            'status', 'freshness_state', 'quality_state', 'official_crosscheck_status')):
        raise ValueError('PROVENANCE_CONFLICT')
    return value


def _absent(value):
    return value is None


def _number(value, *, positive=False):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or (positive and value <= 0)):
        raise ValueError('INVALID_PARTIAL_INPUT_NUMERIC')


def _identity(value, ticker, session=None):
    _mapping(value)
    if value.get('ticker') is not None and value['ticker'] != ticker:
        raise ValueError('TICKER_MISMATCH')
    if session is not None:
        for key in ('session', 'session_date'):
            if value.get(key) is not None and value[key] != session:
                raise ValueError('SESSION_MISMATCH')


def _provenance(value, ticker, previous, freeze, missing):
    if value is None or value == {}:
        missing.append('provenance')
        return
    _identity(value, ticker, previous)
    evidence = value.get('evidence', [])
    if not isinstance(evidence, list):
        raise ValueError('INVALID_PARTIAL_EVIDENCE')
    if 'evidence' in value and not evidence:
        missing.append('provenance.evidence')
    seen = {}
    for index, ref in enumerate(evidence):
        _identity(ref, ticker)
        for key in ('available_at', 'retrieved_at'):
            if ref.get(key) is not None and not core.available_by(ref[key], freeze):
                raise ValueError('POST_FREEZE_EVIDENCE')
        if ref.get('session_end') is not None and ref['session_end'] > previous:
            raise ValueError('EVIDENCE_SESSION_LOOKAHEAD')
        digest = ref.get('sha256')
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest)):
            raise ValueError('HASH_CONFLICT')
        if ref.get('path') is None and digest is not None:
            missing.append(f'provenance.evidence[{index}].path')
        if ref.get('path') is not None:
            path = Path(ref['path'])
            if digest is None:
                missing.append(f'provenance.evidence[{index}].sha256')
            else:
                identity = str(path.resolve())
                if identity in seen and seen[identity] != digest:
                    raise ValueError('HASH_CONFLICT')
                seen[identity] = digest
                if not path.exists():
                    missing.append(f'provenance.evidence[{index}].path')
                elif core.sha256(path) != digest:
                    raise ValueError('HASH_CONFLICT')


def _threshold(value, ticker, previous, missing):
    if value is None:
        missing.append('threshold')
        return
    _identity(value, ticker)
    fields = ('status', 'threshold_version', 'training_end_date', 'T_final')
    for key in fields:
        if _absent(value.get(key)):
            missing.append('threshold.' + key)
    # Validate every present field even when another field is absent.
    if value.get('T_final') is not None:
        _number(value['T_final'], positive=True)
    if value.get('training_end_date') is not None:
        if date.fromisoformat(value['training_end_date']) > date.fromisoformat(previous):
            raise ValueError('THRESHOLD_TRAINING_LOOKAHEAD')
    if value.get('status') is not None and value['status'] not in (
            'ENGINE_TEST_ONLY', 'TRIAL_CANDIDATE_NOT_ACTIVATED'):
        raise ValueError('THRESHOLD_NOT_FROZEN_RESEARCH_OR_TEST')
    if value.get('threshold_version') is not None:
        spec = json.loads((core.ROOT / 'config/threshold_spec.json').read_text(encoding='utf-8'))
        if value['threshold_version'] != spec['version']:
            raise ValueError('THRESHOLD_VERSION_MISSING_OR_UNREGISTERED')
    if value.get('status') == 'TRIAL_CANDIDATE_NOT_ACTIVATED':
        # An absent field cannot conceal a conflicting present frozen value.
        spec = json.loads((core.ROOT / 'config/threshold_spec.json').read_text(encoding='utf-8'))
        bundle = json.loads((core.ROOT / spec['candidate_bundle']).read_text(encoding='utf-8'))
        candidate = bundle.get('thresholds', {}).get(ticker)
        if (bundle.get('status') != 'CANDIDATE_FROZEN_PRETRIAL_NOT_ACTIVATED'
                or bundle.get('automatic_reestimation') is not False or not isinstance(candidate, dict)
                or any(candidate.get(key) != datum for key, datum in value.items() if datum is not None)):
            raise ValueError('FROZEN_THRESHOLD_BUNDLE_MISMATCH')
    if all(value.get(key) is not None for key in fields):
        core.validate_frozen_threshold(value, previous, ticker)


def _metadata(value, threshold, previous, missing, prefix='baselines', require_version=False):
    if value is None:
        missing.append(prefix)
        return
    _mapping(value)
    fields = ['training_end_date', 'threshold_version']
    if require_version:
        fields.append('baseline_version')
    for key in fields:
        if value.get(key) is None:
            missing.append(prefix + '.' + key)
    if value.get('training_end_date') is not None:
        if date.fromisoformat(value['training_end_date']) > date.fromisoformat(previous):
            raise ValueError('LOOKAHEAD_STALE_OR_BASELINE_MISMATCH')
    if (value.get('threshold_version') is not None and threshold is not None
            and threshold.get('threshold_version') is not None
            and value['threshold_version'] != threshold['threshold_version']):
        raise ValueError('BASELINE_THRESHOLD_VERSION_MISMATCH')
    if value.get('baseline_version') is not None:
        spec = json.loads((core.ROOT / 'config/baseline_spec.json').read_text(encoding='utf-8'))
        if value['baseline_version'] != spec['version']:
            raise ValueError('BASELINE_VERSION_CONFLICT')


def _distribution(value, prefix, missing):
    if value is None:
        missing.append(prefix)
        return
    _mapping(value)
    probs = value.get('probabilities')
    if probs is None:
        missing.append(prefix + '.probabilities')
        return
    _mapping(probs)
    if set(probs) - set(core.CLASSES):
        raise ValueError('INVALID_BASELINE_PROBABILITIES')
    for key in core.CLASSES:
        if probs.get(key) is None:
            missing.append(prefix + '.probabilities.' + key)
        else:
            _number(probs[key])
            if not 0 <= probs[key] <= 1:
                raise ValueError('INVALID_BASELINE_PROBABILITIES')
    if all(probs.get(key) is not None for key in core.CLASSES):
        if not math.isclose(sum(probs.values()), 1, abs_tol=1e-12):
            raise ValueError('INVALID_BASELINE_PROBABILITIES')


def _stock(item, ticker, previous, freeze):
    _mapping(item)
    if set(item) - {'features', 'threshold', 'baselines', 'provenance'}:
        raise ValueError('RAW_ROWS_OR_UNKNOWN_STOCK_INPUT')
    missing = []
    _provenance(item.get('provenance'), ticker, previous, freeze, missing)
    limit = item.get('threshold')
    _threshold(limit, ticker, previous, missing)
    baselines = item.get('baselines')
    _metadata(baselines, limit, previous, missing)
    if baselines is not None:
        for key in ('base_rate', 'momentum', 'ma_trend'):
            _distribution(baselines.get(key), 'baselines.' + key, missing)
    feature = item.get('features')
    if feature is None:
        missing.append('features')
        return missing
    _mapping(feature)
    if set(feature) - core.ALLOWED_FEATURES:
        raise ValueError('RAW_ROWS_OR_UNKNOWN_FEATURE')
    for key in (*core.REQUIRED_FEATURES, 'previous_session', 'threshold_version'):
        if feature.get(key) is None:
            missing.append('features.' + key)
    for key in core.REQUIRED_FEATURES:
        if feature.get(key) is not None:
            if key == 'MA_state':
                if feature[key] not in ('BULL', 'BEAR', 'NEUTRAL'):
                    raise ValueError('INVALID_MA_STATE')
            else:
                _number(feature[key], positive=key in ('MA20', 'MA60', 'threshold'))
                if key == 'realized_vol_20d' and feature[key] < 0:
                    raise ValueError('INVALID_PARTIAL_INPUT_NUMERIC')
    prices = {key: feature.get('previous_' + key) for key in PRICE_FIELDS}
    _prices(prices)
    if feature.get('previous_session') is not None and feature['previous_session'] != previous:
        raise ValueError('LOOKAHEAD_STALE_OR_BASELINE_MISMATCH')
    if limit is not None:
        for key, target, error in (
                ('threshold_version', 'threshold_version', 'FEATURE_THRESHOLD_VERSION_MISMATCH'),
                ('threshold', 'T_final', 'FEATURE_THRESHOLD_VALUE_MISMATCH')):
            if feature.get(key) is not None and limit.get(target) is not None and feature[key] != limit[target]:
                raise ValueError(error)
    return missing


def _prices(row):
    # Validate supplied values independently of missing neighboring fields.
    for field in PRICE_FIELDS:
        if row.get(field) is not None:
            _number(row[field], positive=field != 'volume')
            if field == 'volume' and row[field] < 0:
                raise ValueError('INVALID_VOLUME')
    if row.get('high') is not None and row.get('low') is not None and row['high'] < row['low']:
        raise ValueError('OHLC_INCONSISTENCY')
    for field in ('open', 'close'):
        if row.get(field) is not None and (
                (row.get('high') is not None and row[field] > row['high'] + 1e-6)
                or (row.get('low') is not None and row[field] < row['low'] - 1e-6)):
            raise ValueError('OHLC_INCONSISTENCY')


def _rows(rows, ticker, expected_sessions, required_fields, missing):
    if rows is None:
        missing.append('rows')
        return
    if not isinstance(rows, list):
        raise ValueError('INVALID_PARTIAL_HISTORY')
    seen = []
    for index, row in enumerate(rows):
        _identity(row, ticker)
        day = row.get('session_date')
        if day is None:
            missing.append(f'rows[{index}].session_date')
        else:
            date.fromisoformat(day)
            if day not in expected_sessions or (seen and day <= seen[-1]):
                raise ValueError('SESSION_MISMATCH_OR_DUPLICATE')
            seen.append(day)
        for field in required_fields:
            if row.get(field) is None:
                missing.append(f'rows[{index}].{field}')
        _prices(row)
        missing_issues = set()
        if any(row.get(field) is None for field in required_fields if field in PRICE_FIELDS[:4]):
            missing_issues.add('MISSING_OHLC')
        if 'volume' in required_fields and row.get('volume') is None:
            missing_issues.add('MISSING_VOLUME')
        if set(row.get('issues', [])) - missing_issues:
            raise ValueError('HISTORY_INTEGRITY_CONFLICT')
    missing.extend('rows.session:' + day for day in expected_sessions if day not in seen)
    if len(rows) < 61:
        missing.append('rows.minimum_61_sessions')


def _context(target, previous, freeze_time, inputs, market, news, metrics):
    _mapping(inputs)
    if set(inputs) - set(core.CORE):
        raise ValueError('CORE_ONLY')
    # Validate session/freezing/common data even for an empty session.
    core.compact_snapshot(target, previous, freeze_time, {}, market, news, metrics, allow_partial=True)
    freeze = datetime.fromisoformat(freeze_time)
    # News attached to a skipped ticker must not hide a PIT/integrity error.
    for events in (news or {}).values():
        if len(events) > 3:
            raise ValueError('NEWS_BUDGET_EXCEEDED')
        seen = set()
        for event in events:
            if set(event) != {'event_type', 'event_time', 'available_at', 'summary', 'source', 'materiality'}:
                raise ValueError('NEWS_FIELDS_INVALID')
            if not event['summary'] or len(event['summary']) > 300:
                raise ValueError('NEWS_SUMMARY_INVALID')
            if not core.available_by(event['available_at'], freeze):
                raise ValueError('POST_FREEZE_NEWS')
            key = (event['event_type'], event['event_time'], event['summary'])
            if key in seen:
                raise ValueError('DUPLICATE_NEWS_EVENT')
            seen.add(key)
    return freeze


def _result(target, previous, freeze_time, inputs, admitted, missing, market, news, metrics):
    snapshot = core.compact_snapshot(target, previous, freeze_time, admitted,
                                     market, news, metrics, allow_partial=True)
    available = [ticker for ticker in core.CORE if ticker in admitted]
    skipped = [ticker for ticker in core.CORE if ticker not in admitted]
    ledger = {}
    for ticker in core.CORE:
        refs = [{'input_ref': 'stock_inputs.' + ticker}]
        item = inputs.get(ticker)
        if isinstance(item, dict) and isinstance(item.get('provenance'), dict):
            refs.extend(copy.deepcopy(item['provenance'].get('evidence', [])))
        ledger[ticker] = dict(ticker=ticker, session=target,
                              status=SKIPPED if ticker in missing else 'PROCESSED',
                              missing_inputs=sorted(set(missing.get(ticker, []))),
                              reason='REQUIRED_INPUT_ABSENT' if ticker in missing else None,
                              evidence_refs=refs)
    stages = {stage: dict(expected_tickers=list(core.CORE), available_tickers=available.copy(),
                          processed_tickers=available.copy() if stage in STAGES[:2] else [],
                          skipped_tickers=skipped.copy(),
                          status='PREPARED' if stage in STAGES[:2] else 'NOT_RUN') for stage in STAGES}
    return dict(schema_version='partial-ticker-v1', session=target,
                status='COMPLETE' if not skipped else 'PARTIAL' if available else 'NO_PROCESSABLE_TICKERS',
                expected_tickers=list(core.CORE), processed_tickers=available,
                available_tickers=available.copy(), skipped_tickers=skipped,
                ticker_results=ledger, stages=stages, snapshot=snapshot)


def prepare_snapshot(target_session, expected_previous_session, freeze_time, stock_inputs,
                     market=None, news=None, metrics=None):
    """Admit complete prepared stocks; preserve the legacy all-valid snapshot exactly."""
    freeze = _context(target_session, expected_previous_session, freeze_time, stock_inputs, market, news, metrics)
    admitted, missing = {}, {}
    for ticker in core.CORE:
        item = stock_inputs.get(ticker)
        gaps = ['ticker'] if item is None else _stock(item, ticker, expected_previous_session, freeze)
        if gaps:
            missing[ticker] = gaps
        else:
            admitted[ticker] = item
    return _result(target_session, expected_previous_session, freeze_time, stock_inputs,
                   admitted, missing, market, news, metrics)


def prepare_session(target_session, expected_previous_session, freeze_time, stock_inputs,
                    expected_sessions, *, required_price_fields=('close',), market=None, news=None, metrics=None):
    """Preflight raw histories before existing features and frozen baseline selection.

    Each ticker supplies rows, threshold, baseline_tables, baseline_metadata and
    provenance. expected_sessions is the required ordered historical window ending
    at expected_previous_session. Close-only input remains supported; callers of
    an OHLC contract must explicitly require open/high/low/close.
    """
    freeze = _context(target_session, expected_previous_session, freeze_time, stock_inputs, market, news, metrics)
    if (not expected_sessions or list(expected_sessions) != sorted(set(expected_sessions))
            or expected_sessions[-1] != expected_previous_session):
        raise ValueError('INVALID_EXPECTED_HISTORY_SESSIONS')
    for day in expected_sessions:
        date.fromisoformat(day)
    if 'close' not in required_price_fields or set(required_price_fields) - set(PRICE_FIELDS):
        raise ValueError('INVALID_REQUIRED_PRICE_FIELDS')
    missing = {}
    # All integrity checks precede any downstream work, regardless of ticker order.
    for ticker in core.CORE:
        item = stock_inputs.get(ticker)
        if item is None:
            missing[ticker] = ['ticker']
            continue
        _mapping(item)
        if set(item) - {'rows', 'threshold', 'baseline_tables', 'baseline_metadata', 'provenance'}:
            raise ValueError('UNKNOWN_PARTIAL_SESSION_INPUT')
        gaps = []
        _provenance(item.get('provenance'), ticker, expected_previous_session, freeze, gaps)
        _threshold(item.get('threshold'), ticker, expected_previous_session, gaps)
        if item.get('baseline_metadata') is not None and set(_mapping(item['baseline_metadata'])) - {
                'training_end_date', 'threshold_version', 'baseline_version'}:
            raise ValueError('UNKNOWN_BASELINE_METADATA')
        _metadata(item.get('baseline_metadata'), item.get('threshold'), expected_previous_session,
                  gaps, 'baseline_metadata', require_version=True)
        tables = item.get('baseline_tables')
        if tables is None:
            gaps.append('baseline_tables')
        else:
            _mapping(tables)
            _distribution(tables.get('base_rate'), 'baseline_tables.base_rate', gaps)
            for kind, states in (('momentum', ('POSITIVE', 'NEGATIVE', 'FLAT')),
                                 ('ma_trend', ('BULL', 'BEAR', 'NEUTRAL'))):
                if tables.get(kind) is None:
                    gaps.append('baseline_tables.' + kind)
                else:
                    _mapping(tables[kind])
                    for state in states:
                        _distribution(tables[kind].get(state), f'baseline_tables.{kind}.{state}', gaps)
        _rows(item.get('rows'), ticker, expected_sessions, required_price_fields, gaps)
        if gaps:
            missing[ticker] = gaps
    admitted = {}
    for ticker in core.CORE:
        if ticker in missing:
            continue
        item = stock_inputs[ticker]
        feature = core.features(item['rows'], item['threshold'])
        feature['momentum_state'] = core.momentum_state(feature['momentum_20d'])
        baselines = dict(copy.deepcopy(select_baselines(feature, item['baseline_tables'])),
                         **item['baseline_metadata'])
        admitted[ticker] = dict(features=feature, threshold=item['threshold'], baselines=baselines,
                                provenance=item['provenance'])
    # Recheck source hashes after computations; a changing source is never a skip.
    for ticker, item in stock_inputs.items():
        if item is not None:
            gaps = []
            _provenance(item.get('provenance'), ticker, expected_previous_session, freeze, gaps)
            if gaps and ticker not in missing:
                raise ValueError('INPUT_CHANGED_DURING_PREPARATION')
    return _result(target_session, expected_previous_session, freeze_time, stock_inputs,
                   admitted, missing, market, news, metrics)


def inputs_for_stage(result, stage):
    """Return only admitted stocks with coverage; scoring/forecast remain NOT_RUN."""
    if stage not in STAGES:
        raise ValueError('UNKNOWN_PARTIAL_STAGE')
    coverage = copy.deepcopy(result['stages'][stage])
    if set(result['snapshot']['stocks']) != set(coverage['available_tickers']):
        raise ValueError('PARTIAL_TICKER_SET_CONFLICT')
    return dict(**coverage, stocks=copy.deepcopy(result['snapshot']['stocks']))

"""Observed prospective PIT ledger. No network, historical admission or ML training."""
import copy
import json
import math
import re
import uuid
from pathlib import Path

from engine import minimal_trial as core
from engine import fresh_bridge as fresh
from engine import execution_policy as policy
from engine import forward_storage as storage
from engine.parallel_data import digest, stamp, hash_valid
from engine.primary_readiness import config

PRE_FIELDS = set('target_session ticker feature_session feature_set_version features retrieved_at feature_available_at availability_status source_identity source_hashes PIT_validation common_snapshot_hash forecast_bundle_id code_commit'.split())
OUT_FIELDS = set('target_session ticker Open Close return_oc target_class outcome_retrieved_at outcome_available_at availability_status source_identity source_hash corporate_action_status threshold_version'.split())


def new_bundle_id(target_session):
    return 'primary_' + target_session.replace('-', '') + '_' + uuid.uuid4().hex


def validate_bundle_id(value, target_session):
    if not isinstance(value, str) or not re.fullmatch('primary_' + target_session.replace('-', '') + '_[a-f0-9]{32}', value):
        raise ValueError('BUNDLE_ID_INVALID')


def validate_features(features):
    if set(features) != set(core.REQUIRED_FEATURES):
        raise ValueError('FEATURE_ALLOWLIST')
    for name, value in features.items():
        if name == 'MA_state':
            if value not in ('BULL', 'BEAR', 'NEUTRAL'):
                raise ValueError('MA_STATE_INVALID')
        elif (type(value) not in (int, float) or not math.isfinite(value)
              or (name in ('MA20', 'MA60', 'threshold') and value <= 0)
              or (name == 'realized_vol_20d' and value < 0)
              or (name in ('ret_1d', 'ret_5d', 'momentum_20d', 'momentum_60d') and value <= -1)):
            raise ValueError('FEATURE_NUMERIC_INVALID')


def build_pre_session(snapshot, audit, source_hashes, code_commit):
    rows = []
    for ticker, features in snapshot['stocks'].items():
        observed = audit['stocks'][ticker]
        rows.append(dict(target_session=snapshot['target_session'], ticker=ticker,
            feature_session=snapshot['previous_session'], feature_set_version=config()['feature_set_version'],
            features=copy.deepcopy(features), retrieved_at=observed['retrieved_at'],
            feature_available_at=observed['feature_available_at'], availability_status='OBSERVED',
            source_identity={'provider': observed['provider'], 'underlying_provider': observed['underlying_provider'],
                             'source_tool': 'bigdata_company_tearsheet', 'source_section': 'company_overview'},
            source_hashes=dict(source_hashes, ephemeral_response=observed['source_response_sha256']),
            PIT_validation=observed['PIT_validation'], common_snapshot_hash=digest(snapshot),
            forecast_bundle_id=audit['forecast_bundle_id'], code_commit=code_commit))
    result = dict(schema_version='prospective-pre-session-v1', target_session=snapshot['target_session'],
                  rows=rows, ticker_statuses=copy.deepcopy(audit['ticker_statuses']))
    result['ledger_sha256'] = digest(result)
    validate_pre_session(result)
    return result


def validate_pre_session(value):
    if (set(value) != {'schema_version', 'target_session', 'rows', 'ticker_statuses', 'ledger_sha256'}
            or value['schema_version'] != 'prospective-pre-session-v1'
            or value['ledger_sha256'] != digest({k: v for k, v in value.items() if k != 'ledger_sha256'})
            or not value['rows'] or set(value['ticker_statuses']) != set(core.CORE)):
        raise ValueError('PROSPECTIVE_PRE_SCHEMA_OR_HASH')
    target = value['target_session']
    if target in policy.load_policy()['permanently_not_run_sessions']:
        raise ValueError('PERMANENTLY_NOT_RUN_SESSION')
    previous = fresh.expected_previous(policy.validated_calendar(), target)
    seen = set()
    common = set()
    for row in value['rows']:
        if (set(row) != PRE_FIELDS or row['ticker'] not in core.CORE or row['target_session'] != target
                or row['feature_session'] != previous or row['feature_set_version'] != 'prospective-price9-v1'
                or row['availability_status'] != 'OBSERVED' or row['PIT_validation'] != 'PASS'
                or row['source_identity'] != dict(provider='BIGDATA', underlying_provider='FMP',
                     source_tool='bigdata_company_tearsheet', source_section='company_overview')
                or not row['source_hashes'] or any(not hash_valid(h) for h in row['source_hashes'].values())
                or 'ephemeral_response' not in row['source_hashes'] or not hash_valid(row['common_snapshot_hash'])
                or not re.fullmatch('[a-f0-9]{40}', row['code_commit'])):
            raise ValueError('PROSPECTIVE_PRE_ROW_INVALID')
        key = (target, row['ticker'])
        if key in seen:
            raise ValueError('DUPLICATE_TARGET_TICKER')
        seen.add(key)
        common.add((row['common_snapshot_hash'], row['forecast_bundle_id'], row['code_commit']))
        validate_bundle_id(row['forecast_bundle_id'], target)
        validate_features(row['features'])
        retrieved, available = stamp(row['retrieved_at']), stamp(row['feature_available_at'])
        if (not stamp(target + 'T00:00:00+09:00') <= retrieved <= available < stamp(target + 'T08:45:00+09:00')):
            raise ValueError('PROSPECTIVE_PRE_PIT_TIME_INVALID')
    if len(common) != 1:
        raise ValueError('PROSPECTIVE_COMMON_BINDING_INVALID')
    for ticker, status in value['ticker_statuses'].items():
        if (target, ticker) in seen:
            if status != {'status': 'FORECAST_INPUT_READY'}:
                raise ValueError('TICKER_STATUS_CONFLICT')
        elif (status != dict(status='SKIPPED_MISSING_INPUT', reason='REQUIRED_INPUT_ABSENT',
                             missing_inputs=['fresh_retrieval'])):
            raise ValueError('TICKER_STATUS_CONFLICT')
    return value


def validate_outcomes(value, pre):
    validate_pre_session(pre)
    if (set(value) != {'schema_version', 'target_session', 'rows', 'ledger_sha256'}
            or value['schema_version'] != 'prospective-outcomes-v1' or value['target_session'] != pre['target_session']
            or value['ledger_sha256'] != digest({k: v for k, v in value.items() if k != 'ledger_sha256'})):
        raise ValueError('PROSPECTIVE_OUTCOME_SCHEMA_OR_HASH')
    thresholds = {r['ticker']: r['features']['threshold'] for r in pre['rows']}
    seen = set()
    for row in value['rows']:
        ticker, target = row['ticker'], row['target_session']
        if (set(row) != OUT_FIELDS or ticker not in thresholds or target != pre['target_session']
                or row['availability_status'] != 'OBSERVED' or row['threshold_version'] != 'short-oc-p33-v1'
                or not hash_valid(row['source_hash']) or set(row['source_identity']) != {'provider', 'source_tool', 'price_basis'}
                or not all(isinstance(v, str) and v for v in row['source_identity'].values())
                or row['source_identity']['price_basis'] != 'SAME_SESSION_OPEN_CLOSE'
                or row['corporate_action_status'] not in ('VALIDATED', 'NOT_APPLICABLE', 'UNKNOWN')):
            raise ValueError('PROSPECTIVE_OUTCOME_ROW_INVALID')
        if ticker in seen:
            raise ValueError('DUPLICATE_TARGET_TICKER')
        seen.add(ticker)
        if (any(type(row[k]) not in (int, float) or not math.isfinite(row[k]) or row[k] <= 0 for k in ('Open', 'Close'))
                or row['return_oc'] != row['Close'] / row['Open'] - 1
                or row['target_class'] != core.classify(row['return_oc'], thresholds[ticker])):
            raise ValueError('OUTCOME_NUMERIC_OR_CLASS_INVALID')
        if (row['outcome_retrieved_at'] != row['outcome_available_at']
                or stamp(row['outcome_available_at']) < stamp(target + 'T15:30:00+09:00')):
            raise ValueError('PROSPECTIVE_OUTCOME_TIME_INVALID')
    if seen != set(thresholds):
        raise ValueError('OUTCOME_ONE_TO_ONE_COVERAGE_REQUIRED')
    return value


def record_outcomes(target_session, fetch_existing, *, store=None):
    """Later-task infrastructure. Trusted source producer returns normalized data.

    No caller-supplied availability timestamp; the actual completion clock is
    observed here. Source admission is still a separate future training gate.
    """
    root = Path(store or core.ROOT / config()['prospective_store'])
    pre = core.read_frozen(root / 'pre_session' / target_session / 'prospective.json')
    validate_pre_session(pre)
    started = fresh._receipt_now()
    if started < stamp(target_session + 'T15:30:00+09:00') or not callable(fetch_existing):
        raise ValueError('COMPLETED_SESSION_OUTCOME_PRODUCER_REQUIRED')
    destination = root / 'outcomes' / target_session
    with storage.locked(destination.parent):
        if destination.exists():
            raise FileExistsError('OUTCOME_CREATE_ONLY')
        acquired = fetch_existing()
        observed = fresh._receipt_now()
        if observed < started:
            raise ValueError('OUTCOME_CLOCK_ROLLBACK')
        thresholds = {r['ticker']: r['features']['threshold'] for r in pre['rows']}
        if set(acquired) != set(thresholds):
            raise ValueError('OUTCOME_ONE_TO_ONE_COVERAGE_REQUIRED')
        rows = []
        for ticker, item in acquired.items():
            if set(item) != {'Open', 'Close', 'source_identity', 'source_hash', 'corporate_action_status'}:
                raise ValueError('OUTCOME_PRODUCER_ALLOWLIST')
            if any(type(item[k]) not in (int, float) or not math.isfinite(item[k]) or item[k] <= 0 for k in ('Open', 'Close')):
                raise ValueError('OUTCOME_NUMERIC_INVALID')
            ret = item['Close'] / item['Open'] - 1
            rows.append(dict(item, ticker=ticker, target_session=target_session, return_oc=ret,
                             target_class=core.classify(ret, thresholds[ticker]),
                             outcome_retrieved_at=observed.isoformat(), outcome_available_at=observed.isoformat(),
                             availability_status='OBSERVED', threshold_version='short-oc-p33-v1'))
        result = dict(schema_version='prospective-outcomes-v1', target_session=target_session, rows=rows)
        result['ledger_sha256'] = digest(result)
        validate_outcomes(result, pre)
        storage.commit(destination, lambda stage: storage.seal(stage / 'outcomes.json', result),
                       lambda stage: validate_outcomes(core.read_frozen(stage / 'outcomes.json'), pre))
    return destination


def join_rows(pre, outcomes, training_as_of):
    """One-to-one observed join, no estimator fitting or source admission upgrade."""
    validate_outcomes(outcomes, pre)
    by_ticker = {r['ticker']: r for r in outcomes['rows']}
    result = []
    for row in pre['rows']:
        outcome = by_ticker[row['ticker']]
        if stamp(outcome['outcome_available_at']) >= stamp(training_as_of):
            raise ValueError('OUTCOME_NOT_AVAILABLE_FOR_TRAINING')
        if outcome['corporate_action_status'] == 'UNKNOWN':
            raise ValueError('CORPORATE_ACTION_NOT_RESOLVED')
        result.append(dict(pre_session=copy.deepcopy(row), outcome=copy.deepcopy(outcome),
                           training_admission='PENDING_SEPARATE_SOURCE_AND_EVALUATION_REVIEW'))
    return result

"""PIT numerical training replay. Local inputs only; no Forecast/scoring entry."""
import copy
import json
import math
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from engine import historical_admission as admission
from engine import historical_quality as quality
from engine import minimal_trial as core
from engine import one_session_dry_run as storage
from engine.parallel_data import PRICE9, digest, hash_valid, rules, stamp

POLICY = core.ROOT / 'config/parallel_training_dataset_v1.json'
VERSION = 'pit-parallel-training-v1'
REPLAY = 'cv-threshold-replay-v1'


def policy():
    return json.loads(POLICY.read_bytes())


def availability(evidence, source, session):
    """Resolve a bound for this exact content vintage, never its filesystem time."""
    status = evidence.get('status', 'UNKNOWN')
    if status == 'UNKNOWN':
        return dict(status='UNKNOWN', available_at=None, rule_version=None)
    if evidence.get('source') != source or not hash_valid(evidence.get('evidence_sha256')):
        raise ValueError('AVAILABILITY_SOURCE_EVIDENCE_REQUIRED')
    if status == 'OBSERVED':
        if evidence.get('time_kind') not in ('RETRIEVAL', 'PUBLICATION'):
            raise ValueError('AVAILABILITY_TIME_KIND_NOT_ALLOWED')
        value = evidence['available_at']
        stamp(value)
        return dict(status=status, available_at=value, rule_version='observed-content-vintage-v1')
    if status != 'SOURCE_SCHEDULE_DERIVED':
        raise ValueError('AVAILABILITY_EVIDENCE_TYPE_INVALID')
    key = evidence['rule_version']
    rule = policy()['source_schedule_rules'].get(key)
    if (not rule or rule['source'] != source or rule['evidence_sha256'] != evidence['evidence_sha256']
            or not rule.get('guaranteed_upper_bound')
            or not rule['effective_start'] <= session <= rule['effective_end']):
        raise ValueError('SOURCE_SCHEDULE_NOT_PROVEN')
    # Only a guaranteed calendar-day upper bound is supported; an expected date
    # or acquisition schedule is deliberately insufficient. No active rule today.
    if rule['kind'] != 'CALENDAR_DAYS_AFTER_SESSION':
        raise ValueError('SOURCE_SCHEDULE_RULE_UNSUPPORTED')
    bound = date.fromisoformat(session) + timedelta(days=rule['days_after_session'])
    value = bound.isoformat() + 'T' + rule['latest_time_jst'] + '+09:00'
    stamp(value)
    return dict(status=status, available_at=value, rule_version=key)


def _key(row):
    ticker, session = row['ticker'], row['target_session']
    if ticker not in core.CORE or date.fromisoformat(session).isoformat() != session:
        raise ValueError('TRAINING_CORE6_SESSION_REQUIRED')
    return session, ticker


def _unique(rows, kind):
    result = {}
    for row in rows:
        key = _key(row)
        if key in result:
            raise ValueError('DUPLICATE_' + kind + '_GRAIN')
        result[key] = row
    return result


def _window_features(rows):
    # This dummy positive threshold only unlocks the existing price formula.
    # It never becomes a training label/input or a registered threshold candidate.
    limit = dict(status='ENGINE_TEST_ONLY', threshold_version='short-oc-p33-v1',
                 training_end_date=rows[-1]['session_date'], T_final=1.0)
    values = core.features(rows, limit)
    return {n: values[n] for n in PRICE9 if n != 'threshold'}


def replay_price_features(history, sessions, target_session, *, synthetic=False):
    """All nine inputs are pre-target, including the historical threshold input."""
    records = _unique(history, 'HISTORY')
    dates = sorted(set(s for s in sessions if s < target_session))
    output = []
    for i, day in enumerate(dates):
        prior = dates[max(0, i - 61):i]
        for ticker in core.CORE:
            window = [records.get((s, ticker)) for s in prior]
            reasons = []
            if len(prior) < 61 or any(r is None for r in window):
                reasons.append('SKIPPED_MISSING_FEATURE')
            known = [r for r in window if r is not None]
            if any(r['synthetic'] != synthetic for r in known):
                raise ValueError('TRAINING_DATASET_MODE_MISMATCH')
            bounds = [availability(r['availability'], r['source'], r['target_session']) for r in known]
            if any(b['status'] == 'UNKNOWN' for b in bounds):
                reasons.append('FEATURE_AVAILABILITY_UNKNOWN')
            latest = max((b['available_at'] for b in bounds if b['available_at']), key=stamp, default=None)
            cutoff = stamp(day + 'T08:45:00+09:00')
            if latest and stamp(latest) > cutoff:
                reasons.append('FEATURE_AFTER_CUTOFF')
            if any(not r['feature_integrity'] or r['quality_issues'] for r in known):
                reasons.append('SKIPPED_FEATURE_INTEGRITY')
            checks = admission.feature_checks(known, prior, day, bounds) if not synthetic else None
            if checks is not None:
                reasons.extend('ADMISSION_' + k.upper() + '_' + v for k, v in checks.items()
                               if v not in ('PASS', 'NOT_APPLICABLE'))
            features, input_threshold = None, None
            if not reasons:
                values = [dict(session_date=r['target_session'], **r['values']) for r in known]
                test_threshold = known[-1].get('synthetic_feature_threshold')
                if test_threshold is not None:
                    if not synthetic:
                        raise ValueError('SYNTHETIC_THRESHOLD_IN_REAL_FEATURES')
                    input_threshold = copy.deepcopy(test_threshold)
                else:
                    try:
                        input_threshold = cv_threshold_replay(history, ticker, prior[-1],
                            (cutoff - timedelta(microseconds=1)).isoformat(), 'feature-' + day,
                            synthetic=synthetic)
                        input_threshold['purpose'] = 'HISTORICAL_FEATURE_INPUT_ONLY'
                    except ValueError as error:
                        reasons.append('SKIPPED_FEATURE_THRESHOLD_REPLAY:' + str(error))
                if input_threshold is not None:
                    if (input_threshold['training_end'] > prior[-1]
                            or stamp(input_threshold['replay_as_of']) > cutoff):
                        raise ValueError('FEATURE_THRESHOLD_LOOKAHEAD')
                    latest = max((latest, input_threshold['input_available_at']), key=stamp)
                    features = dict(_window_features(values), threshold=input_threshold['threshold_value'])
            output.append(dict(target_session=day, ticker=ticker,
                feature_session=prior[-1] if prior else None,
                feature_available_at=latest,
                feature_availability_status=('UNKNOWN' if not bounds or any(b['status'] == 'UNKNOWN' for b in bounds)
                    else 'SOURCE_SCHEDULE_DERIVED' if any(b['status'] == 'SOURCE_SCHEDULE_DERIVED' for b in bounds)
                    else 'OBSERVED'),
                feature_availability_rules=sorted({b['rule_version'] for b in bounds if b['rule_version']}),
                feature_set_version={m: v for m, v in rules()['feature_set_versions'].items() if m != 'equal-linear-pool-v1'},
                features=features, feature_reasons=sorted(set(reasons)),
                **({'admission_checks': checks} if checks is not None else {}),
                feature_threshold_metadata=input_threshold,
                input_sessions=prior, cutoff=cutoff.isoformat(),
                source_refs=sorted({ref for r in known for ref in r['source_refs']}), synthetic=synthetic))
    return output


def build_dataset(features, outcomes, target_session, training_as_of, source_hashes, *, synthetic=False):
    """Strict one-to-one join, retaining exclusion metadata without raw prices."""
    asof = stamp(training_as_of)
    if not synthetic and asof > datetime.now(timezone.utc):
        raise ValueError('TRAINING_AS_OF_IN_FUTURE')
    # Future target rows are not inspected/used, including malformed future values.
    features = [r for r in features if r['target_session'] < target_session]
    outcomes = [r for r in outcomes if r['target_session'] < target_session]
    left, right = _unique(features, 'FEATURE'), _unique(outcomes, 'OUTCOME')
    if any(not hash_valid(h) for h in source_hashes.values()):
        raise ValueError('TRAINING_SOURCE_HASH_INVALID')
    eligible, excluded = [], []
    matched = 0
    for day, ticker in sorted(left.keys() | right.keys()):
        f, out = left.get((day, ticker)), right.get((day, ticker))
        reasons = list(f['feature_reasons']) if f else ['SKIPPED_MISSING_FEATURE']
        if not out:
            reasons.append('SKIPPED_MISSING_OUTCOME')
            bound = dict(status='UNKNOWN', available_at=None, rule_version=None)
        else:
            if out['synthetic'] != synthetic:
                raise ValueError('TRAINING_DATASET_MODE_MISMATCH')
            bound = availability(out['availability'], out['source'], day)
            if bound['status'] == 'UNKNOWN':
                reasons.append('OUTCOME_AVAILABILITY_UNKNOWN')
            elif stamp(bound['available_at']) >= asof:
                reasons.append('OUTCOME_NOT_AVAILABLE_FOR_TRAINING')
            if bound['available_at'] and stamp(bound['available_at']) < stamp(day + 'T15:30:00+09:00'):
                # Conservative throughout history (including older 15:00 closes).
                reasons.append('OUTCOME_AVAILABILITY_BEFORE_COMPLETED_SESSION')
            if not out['outcome_source_admitted']:
                reasons.append('OUTCOME_SOURCE_NOT_ADMITTED')
            if not out['outcome_integrity'] or out['quality_issues']:
                reasons.append('SKIPPED_OUTCOME_INTEGRITY')
            if not out['derived_training_persistence_allowed']:
                reasons.append('TRAINING_PERSISTENCE_NOT_ADMITTED')
        if f and out:
            matched += 1
            if f['synthetic'] != synthetic or not set(f['source_refs'] + out['source_refs']) <= set(source_hashes):
                raise ValueError('TRAINING_SOURCE_OR_MODE_MISMATCH')
        if f and (f['feature_availability_status'] == 'UNKNOWN' or f['feature_available_at'] is None):
            reasons.append('FEATURE_AVAILABILITY_UNKNOWN')
        checks = admission.row_checks(f, out, bound, asof) if not synthetic else None
        if checks is not None:
            if reasons and admission.classify(checks) == 'ADMITTED_PIT':
                checks['PIT'] = 'FAIL'
            reasons.extend('ADMISSION_' + k.upper() + '_' + v for k, v in checks.items()
                           if v not in ('PASS', 'NOT_APPLICABLE'))
        common = dict(target_session=day, ticker=ticker,
            **(dict(admission_checks=checks, admission_status=admission.classify(checks),
                    outcome_source=out['source'] if out else None,
                    outcome_admission=out.get('outcome_admission') if out else None,
                    outcome_selection_reason=out.get('selection_reason') if out else None) if checks is not None else {}),
            feature_session=f['feature_session'] if f else None,
            feature_available_at=f['feature_available_at'] if f else None,
            feature_availability_status=f['feature_availability_status'] if f else 'UNKNOWN',
            outcome_available_at=bound['available_at'], outcome_availability_status=bound['status'],
            outcome_availability_rule=bound['rule_version'],
            source_refs=sorted(set((f['source_refs'] if f else []) + (out['source_refs'] if out else []))))
        if reasons:
            excluded.append(dict(common, reasons=sorted(set(reasons)), PIT_status='EXCLUDED'))
            continue
        if (not f['input_sessions'] or max(f['input_sessions']) != f['feature_session']
                or f['feature_session'] >= day or len(set(f['input_sessions'])) != 61
                or f['input_sessions'] != sorted(f['input_sessions'])
                or f['feature_available_at'] is None
                or stamp(f['feature_available_at']) > stamp(f['cutoff'])
                or stamp(f['cutoff']) != stamp(day + 'T08:45:00+09:00')
                or f['feature_set_version'] != {m: v for m, v in rules()['feature_set_versions'].items()
                                               if m != 'equal-linear-pool-v1'}
                or set(f['features']) != set(PRICE9)
                or not f['feature_threshold_metadata']
                or f['feature_threshold_metadata']['training_end'] > f['feature_session']
                or stamp(f['feature_threshold_metadata']['replay_as_of']) > stamp(f['cutoff'])
                or f['features']['threshold'] != f['feature_threshold_metadata']['threshold_value']):
            raise ValueError('FEATURE_REPLAY_PIT_INVALID')
        values = out['values']
        if any(type(values.get(k)) not in (float, int) or not math.isfinite(values[k]) or values[k] <= 0
               for k in ('open', 'close')):
            raise ValueError('OUTCOME_OPEN_CLOSE_INVALID')
        eligible.append(dict(common, feature_set_version=f['feature_set_version'],
            features=f['features'], input_sessions=f['input_sessions'], cutoff=f['cutoff'],
            feature_threshold_metadata=f['feature_threshold_metadata'],
            target_open=values['open'], target_close=values['close'],
            target_return=values['close'] / values['open'] - 1,
            target_class=None, threshold_used_for_label=None, threshold_source='UNBOUND_UNTIL_CV_OR_FINAL_FIT',
            PIT_status='PASS_PRICE_FEATURES_AND_OUTCOME;THRESHOLD_FIT_PENDING'))
    audit = dict(grain=['target_session', 'ticker'], join_cardinality='ONE_TO_ONE',
        feature_rows=len(left), outcome_rows=len(right), matched_rows=matched,
        unmatched_feature_rows=len(left.keys() - right.keys()), unmatched_outcome_rows=len(right.keys() - left.keys()),
        duplicates=0, ticker_mismatches=0, session_mismatches=0,
        candidate_dates=len({d for d, _ in left.keys() | right.keys()}),
        candidate_rows=len(left.keys() | right.keys()),
        pit_eligible_dates=len({r['target_session'] for r in eligible}), eligible_rows=len(eligible),
        excluded_rows=len(excluded),
        excluded_by_reason=dict(sorted(Counter(reason for r in excluded for reason in r['reasons']).items())),
        core6_eligible_rows={t: sum(r['ticker'] == t for r in eligible) for t in core.CORE},
        feature_availability_types=dict(Counter(r['feature_availability_status'] for r in eligible + excluded)),
        outcome_availability_types=dict(Counter(r['outcome_availability_status'] for r in eligible + excluded)))
    dataset = dict(schema_version=VERSION, target_session=target_session, training_as_of=training_as_of,
        synthetic=synthetic, feature_set_versions=rules()['feature_set_versions'],
        production_threshold_sha256=core.sha256(core.ROOT / 'config/trial_thresholds_candidate.json') if not synthetic else None,
        source_hashes=dict(sorted(source_hashes.items())), eligible_rows=eligible, excluded_rows=excluded,
        pit_audit=audit, fold_definitions=[], cv_threshold_replays=[],
        readiness='DATASET_PASS_PENDING_CV' if eligible else 'BLOCKED', blockers=[], immutable=True)
    return dataset


def cv_threshold_replay(history, ticker, train_end, fit_as_of, fold_id, *, synthetic=False):
    """Same frozen threshold algorithm, prefix only, no candidate registration."""
    asof = stamp(fit_as_of)
    prefix = sorted([r for r in history if r['ticker'] == ticker and r['target_session'] <= train_end],
                    key=lambda r: r['target_session'])
    _unique(prefix, 'THRESHOLD_INPUT')
    if not prefix or prefix[-1]['target_session'] != train_end:
        raise ValueError('CV_THRESHOLD_TRAIN_END_MISSING')
    for r in prefix:
        b = availability(r['availability'], r['source'], r['target_session'])
        if (r['synthetic'] != synthetic or not r['outcome_source_admitted'] or not r['feature_integrity']
                or not r['outcome_integrity'] or b['status'] == 'UNKNOWN'
                or stamp(b['available_at']) >= asof):
            raise ValueError('CV_THRESHOLD_INPUT_NOT_PIT_SAFE')
    records = [dict(ticker=ticker, session_date=r['target_session'], values=r['values'],
                    issues=set(r['quality_issues'])) for r in prefix]
    limit = quality.threshold_candidate(dict(ticker=ticker, records=records,
        source_hash=digest(prefix)), training_end_date=train_end)
    return dict(threshold_algorithm_version='short-oc-p33-v1', replay_version=REPLAY,
        fold_id=fold_id, ticker=ticker, training_start=prefix[0]['target_session'], training_end=train_end,
        threshold_value=limit['T_final'], input_hash=digest(prefix), replay_as_of=fit_as_of,
        input_available_at=max((availability(r['availability'], r['source'], r['target_session'])['available_at']
                                for r in prefix), key=stamp),
        calculated_at=fit_as_of if synthetic else datetime.now(timezone.utc).isoformat(),
        purpose='CROSS_VALIDATION_ONLY', production_candidate=False,
        threshold_spec_sha256=core.sha256(core.ROOT / 'config/threshold_spec.json'),
        quality_policy_sha256=core.sha256(quality.POLICY_PATH),
        valid_input_rows=limit['valid_training_rows'], excluded_input_rows=limit['excluded_rows'])


def _materialize(rows, thresholds, label_scope):
    result = []
    for row in rows:
        value = thresholds[row['ticker']]
        limit = value['threshold_value']
        bound = copy.deepcopy(row)
        bound.update(target_class=core.classify(row['target_return'], limit),
            threshold_used_for_label=limit, threshold_source=label_scope,
            threshold_metadata=value, PIT_status='PASS')
        # Aliases consumed by the existing two estimators, no new predictors.
        bound.update(session=row['target_session'], label=bound['target_class'])
        result.append(bound)
    return result


def plan_folds(dataset, history):
    """All full expanding folds, with train-only threshold warmup and label embargo."""
    rows = dataset['eligible_rows']
    config = rules()['walk_forward']
    minimum, width = config['minimum_train_sessions'], config['validation_sessions']
    days = sorted({r['target_session'] for r in rows})
    existing = {(r['fold_id'], r['ticker']): r for r in dataset['cv_threshold_replays']}
    folds, skips = [], []
    for boundary in range(minimum, len(days) - width + 1, width):
        valid_days = set(days[boundary:boundary + width])
        validation = [r for r in rows if r['target_session'] in valid_days]
        cutoff = min(stamp(r['cutoff']) for r in validation)
        # Remove the entire date when any label on it is unavailable at fit time.
        embargo = {r['target_session'] for r in rows if r['target_session'] < days[boundary]
                   and stamp(r['outcome_available_at']) >= cutoff}
        train = [r for r in rows if r['target_session'] < days[boundary] and r['target_session'] not in embargo]
        train_days = sorted({r['target_session'] for r in train})
        if len(train_days) < minimum:
            skips.append(dict(validation_start=days[boundary], reason='INSUFFICIENT_LABEL_AVAILABLE_TRAINING_DATES'))
            continue
        # Train end must be the same cutoff for all ticker threshold estimates.
        end = train_days[-1]
        fit_time = (cutoff - timedelta(microseconds=1)).isoformat()
        fold_id = 'fold-' + str(len(folds) + 1)
        try:
            replays = {t: cv_threshold_replay(history, t, end, fit_time, fold_id,
                         synthetic=dataset['synthetic']) for t in core.CORE}
        except ValueError as error:
            # Preserve the existing threshold's warmup minimum; never lower it.
            skips.append(dict(validation_start=days[boundary], reason=str(error)))
            continue
        for t, value in replays.items():
            original = existing.get((fold_id, t))
            if original and _without_calculation_times(value) == _without_calculation_times(original):
                replays[t] = original
        descriptor = dict(fold_id=fold_id, training_start=train_days[0], training_end=end,
            validation_start=min(valid_days), validation_end=max(valid_days),
            training_dates=len(train_days), validation_dates=len(valid_days),
            train_rows=len(train), validation_rows=len(validation),
            threshold_fit_as_of=fit_time, embargoed_dates=sorted(embargo),
            cv_threshold_replays=list(replays.values()))
        folds.append(dict(definition=descriptor,
            train=_materialize(train, replays, 'CV_LABEL_THRESHOLD'),
            validation=_materialize(validation, replays, 'CV_LABEL_THRESHOLD')))
    dataset['fold_definitions'] = [f['definition'] for f in folds]
    dataset['cv_threshold_replays'] = [r for f in folds for r in f['definition']['cv_threshold_replays']]
    dataset['fold_skips'] = skips
    if len(folds) < config['minimum_validation_folds']:
        dataset['blockers'].append('BLOCKER_INSUFFICIENT_PIT_VALIDATION_HISTORY')
        dataset['readiness'] = 'BLOCKED'
    elif dataset['blockers']:
        dataset['readiness'] = 'BLOCKED'
    else:
        dataset['readiness'] = 'PASS'
    return folds


def final_training_rows(dataset):
    """Final fit only, using the unchanged production frozen label threshold."""
    if dataset['readiness'] != 'PASS' or dataset['blockers'] or dataset['synthetic']:
        raise ValueError('PIT_DATASET_NOT_READY_FOR_REAL_TRAINING')
    path = core.ROOT / 'config/trial_thresholds_candidate.json'
    if core.sha256(path) != dataset['production_threshold_sha256']:
        raise ValueError('PRODUCTION_THRESHOLD_CHANGED')
    production = json.loads(path.read_bytes())
    thresholds = {}
    for t in core.CORE:
        value = production['thresholds'][t]
        core.validate_frozen_threshold(value, dataset['target_session'], ticker=t)
        if value['training_end_date'] >= dataset['target_session']:
            raise ValueError('FINAL_THRESHOLD_TARGET_LEAKAGE')
        if stamp(value['training_end_date'] + 'T15:30:00+09:00') >= stamp(dataset['training_as_of']):
            raise ValueError('FINAL_THRESHOLD_AFTER_APPROVED_TRAINING_CUTOFF')
        thresholds[t] = dict(threshold_value=value['T_final'], threshold_version='short-oc-p33-v1',
            training_end=value['training_end_date'], production_sha256=dataset['production_threshold_sha256'],
            purpose='FINAL_MODEL_LABEL_THRESHOLD')
    return _materialize(dataset['eligible_rows'], thresholds, 'FINAL_MODEL_LABEL_THRESHOLD')


def dataset_sha256(dataset):
    """Deterministic data hash; separate file sidecar seals actual calculation times."""
    return digest(_without_calculation_times({k: v for k, v in dataset.items() if k != 'dataset_sha256'}))


def _without_calculation_times(value):
    if isinstance(value, dict):
        return {k: _without_calculation_times(v) for k, v in value.items() if k != 'calculated_at'}
    if isinstance(value, list):
        return [_without_calculation_times(v) for v in value]
    return value


def write_dataset(dataset, root):
    """Persist derived rows/exclusion metadata only; atomic and create-only."""
    checksum = dataset_sha256(dataset)
    destination = Path(root) / checksum
    def build(stage):
        detail = dataset.get('admission_audit')
        if detail:
            import hashlib
            matrix = admission.provenance_csv(detail['source_provenance_groups'])
            if hashlib.sha256(matrix).hexdigest() != detail['source_provenance_csv_sha256']:
                raise ValueError('PROVENANCE_MATRIX_HASH_MISMATCH')
            (stage / 'source_provenance.csv').write_bytes(matrix)
            storage._seal(stage / 'feature_revision_proofs.json', detail['feature_proofs'])
            storage._seal(stage / 'source_admission_audit.json', {k: v for k, v in detail.items()
                if k not in ('source_provenance_groups', 'feature_proofs')})
        storage._seal(stage / 'dataset.json', dict(dataset, dataset_sha256=checksum))
    with storage._locked(destination.parent):
        storage._commit(destination, build)
    return destination


def read_dataset(destination):
    value = core.read_frozen(Path(destination) / 'dataset.json')
    if value['dataset_sha256'] != dataset_sha256(value):
        raise ValueError('TRAINING_DATASET_HASH_MISMATCH')
    if value.get('admission_audit'):
        detail = value['admission_audit']
        if core.sha256(Path(destination) / 'source_provenance.csv') != detail['source_provenance_csv_sha256']:
            raise ValueError('PROVENANCE_MATRIX_HASH_MISMATCH')
        if core.read_frozen(Path(destination) / 'feature_revision_proofs.json') != detail['feature_proofs']:
            raise ValueError('FEATURE_PROOF_HASH_MISMATCH')
        if core.read_frozen(Path(destination) / 'source_admission_audit.json') != {k: v for k, v in detail.items()
                if k not in ('source_provenance_groups', 'feature_proofs')}:
            raise ValueError('SOURCE_ADMISSION_AUDIT_MISMATCH')
    return value


def require_training_ready(dataset):
    """Recheck eligibility/counts and source hashes at model-fit/Freeze boundaries."""
    if (dataset['readiness'] != 'PASS' or dataset['blockers'] or dataset['synthetic']
            or len(dataset['fold_definitions']) < rules()['walk_forward']['minimum_validation_folds']):
        raise ValueError('PIT_DATASET_NOT_READY_FOR_REAL_TRAINING')
    rows = dataset['eligible_rows']
    _unique(rows, 'TRAINING')
    if not rows or len(rows) != dataset['pit_audit']['eligible_rows']:
        raise ValueError('TRAINING_DATASET_AUDIT_MISMATCH')
    for name, expected in dataset['source_hashes'].items():
        if core.sha256(core.ROOT / name) != expected:
            raise ValueError('TRAINING_SOURCE_CHANGED')
    for row in rows:
        if (row.get('admission_status') != 'ADMITTED_PIT'
                or set(row.get('admission_checks', {})) != set(admission.policy()['required_gates'])
                or admission.classify(row['admission_checks']) != 'ADMITTED_PIT'):
            raise ValueError('TRAINING_ADMISSION_NOT_PROVEN')
        if (row['target_session'] >= dataset['target_session']
                or row['feature_session'] >= row['target_session']
                or row['feature_availability_status'] == 'UNKNOWN'
                or row['outcome_availability_status'] == 'UNKNOWN'
                or stamp(row['feature_available_at']) > stamp(row['cutoff'])
                or row['feature_threshold_metadata']['training_end'] >= row['target_session']
                or stamp(row['feature_threshold_metadata']['input_available_at']) > stamp(row['cutoff'])
                or stamp(row['outcome_available_at']) >= stamp(dataset['training_as_of'])):
            raise ValueError('TRAINING_DATASET_PIT_CHANGED')
    return dataset

"""Offline training/validation and model Freeze. No outcome/provider acquisition."""
import importlib.metadata
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from engine import minimal_trial as core
from engine import forward_artifacts as storage
from engine.parallel_data import (CLASSES, PRICE9, PRIMARY, LOGISTIC, LIGHTGBM, MODEL_ROLES,
                                  digest, expanding_folds, probability_valid, rules, stamp,
                                  training_rows, hash_valid)
from engine.parallel_data import specification_hash
from models.linear_pool import predict as primary_predict
from models.logistic import fit as fit_logistic
from models.lightgbm import fit as fit_lightgbm, search_space


def matrix(rows):
    state = {'BEAR': -1, 'NEUTRAL': 0, 'BULL': 1}
    return np.array([[state[r['features'][n]] if n == 'MA_state' else r['features'][n]
                      for n in PRICE9] for r in rows], dtype=float)


def labels(rows):
    return np.array([CLASSES.index(r['label']) for r in rows])


def predict_probabilities(model_id, estimator, rows):
    x = matrix(rows)
    if model_id == LOGISTIC:
        raw = estimator.predict_proba(x)
        if list(estimator.classes_) != [0, 1, 2]:
            raise ValueError('ALL_THREE_TRAINING_CLASSES_REQUIRED')
    elif model_id == LIGHTGBM:
        raw = estimator.predict(x, num_threads=1)
    else:
        raise ValueError('ONLY_TWO_SHADOW_ESTIMATORS')
    result = [{c: float(v) for c, v in zip(CLASSES, p)} for p in raw]
    for p in result:
        probability_valid(p)
    return result


def offline_metrics(actual, probabilities):
    """Training diagnostics only. Multiclass Brier sum, no division by 3/clipping."""
    if not actual or len(actual) != len(probabilities) or any(a not in CLASSES for a in actual):
        raise ValueError('OFFLINE_METRIC_GRAIN_INVALID')
    for p in probabilities:
        probability_valid(p)
    brier = sum(sum((p[c] - (a == c)) ** 2 for c in CLASSES)
                for a, p in zip(actual, probabilities)) / len(actual)
    values = [p[a] for a, p in zip(actual, probabilities)]
    log_loss = -sum(math.log(p) for p in values) / len(values) if all(values) else None
    order = ('FLAT', 'UP', 'DOWN')
    predictions = [max(order, key=lambda c: p[c]) for p in probabilities]
    accuracy = sum(a == p for a, p in zip(actual, predictions)) / len(actual)
    f1s = []
    for c in CLASSES:
        tp = sum(a == c and p == c for a, p in zip(actual, predictions))
        fp = sum(a != c and p == c for a, p in zip(actual, predictions))
        fn = sum(a == c and p != c for a, p in zip(actual, predictions))
        f1s.append(2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0)
    calibration = {}
    for c in CLASSES:
        bins = []
        for b in range(5):
            indices = [i for i, p in enumerate(probabilities) if min(4, int(p[c] * 5)) == b]
            bins.append(dict(lower=b / 5, upper=(b + 1) / 5, count=len(indices),
                             mean_probability=sum(probabilities[i][c] for i in indices) / len(indices) if indices else None,
                             observed_frequency=sum(actual[i] == c for i in indices) / len(indices) if indices else None))
        calibration[c] = bins
    return dict(brier=brier, log_loss=log_loss,
                log_loss_reason=None if log_loss is not None else 'ZERO_ACTUAL_CLASS_PROBABILITY',
                accuracy=accuracy, macro_f1=sum(f1s) / 3, calibration=calibration, rows=len(actual))


def _fit(model_id, rows, parameters):
    y = labels(rows)
    if set(y) != {0, 1, 2}:
        raise ValueError('ALL_THREE_TRAINING_CLASSES_REQUIRED')
    return fit_logistic(matrix(rows), y) if model_id == LOGISTIC else fit_lightgbm(matrix(rows), y, parameters)


def _fold_description(train, validation):
    return dict(training_start=train[0]['session'], training_end=train[-1]['session'],
                validation_start=validation[0]['session'], validation_end=validation[-1]['session'],
                training_sessions=len({r['session'] for r in train}),
                validation_sessions=len({r['session'] for r in validation}),
                train_rows=len(train), validation_rows=len(validation),
                label_availability='ALL_TRAIN_OUTCOMES_BEFORE_VALIDATION_CUTOFF')


def train_models(samples, target_session, created_at, code_commit, *, synthetic=False):
    """Fit two shadows, compare six series offline. No live forecasts are written."""
    config = rules()
    if not synthetic:
        raise ValueError('PIT_TRAINING_DATASET_REQUIRED; use train_pit_dataset')
    if not re.fullmatch('[a-f0-9]{40}', code_commit):
        raise ValueError('CODE_COMMIT_REQUIRED')
    if stamp(created_at) >= stamp(target_session + 'T08:45:00+09:00'):
        raise ValueError('MODEL_TRAINING_AFTER_FORECAST_CUTOFF')
    if not synthetic and stamp(created_at) > datetime.now(timezone.utc):
        raise ValueError('MODEL_TRAINING_AS_OF_IN_FUTURE')
    rows = training_rows(samples, target_session, created_at, synthetic=synthetic)
    folds = expanding_folds(rows)
    dataset_hash = digest(rows)
    result = {}
    # Train each fold's preprocessing from scratch; validation never touches fit.
    for model_id in (LOGISTIC, LIGHTGBM):
        candidates = [config['logistic']] if model_id == LOGISTIC else search_space()
        diagnostics = []
        for parameters in candidates:
            all_actual, all_probs, descriptions = [], [], []
            for train, validation in folds:
                estimator = _fit(model_id, train, parameters)
                probabilities = predict_probabilities(model_id, estimator, validation)
                actual = [r['label'] for r in validation]
                descriptions.append(dict(_fold_description(train, validation),
                                         metrics=offline_metrics(actual, probabilities)))
                all_actual.extend(actual)
                all_probs.extend(probabilities)
            diagnostics.append(dict(hyperparameters=parameters,
                                    metrics=offline_metrics(all_actual, all_probs), folds=descriptions))
        chosen = min(range(len(diagnostics)), key=lambda i: diagnostics[i]['metrics']['brier'])
        parameters = diagnostics[chosen]['hyperparameters']
        estimator = _fit(model_id, rows, parameters)
        metadata = dict(schema_version='parallel-model-freeze-v1', model_id=model_id,
                        model_version=model_id, model_role='SHADOW', target_session=target_session,
                        training_start=rows[0]['session'], training_end=rows[-1]['session'],
                        training_rows=len(rows), feature_set_version=config['feature_set_versions'][model_id],
                        features=list(PRICE9), hyperparameters=parameters,
                        effective_parameters=(estimator.named_steps['classifier'].get_params() if model_id == LOGISTIC
                                              else estimator.params),
                        threshold_version=config['threshold_version'], training_dataset_hash=dataset_hash,
                        walk_forward_summary=dict(status='MODEL_SELECTION_DIAGNOSTIC_NOT_UNBIASED_PERFORMANCE',
                                                  selected_candidate=chosen, candidates=diagnostics),
                        created_at=created_at, code_commit=code_commit, config_hash=specification_hash(),
                        synthetic=synthetic, seed=config['seed'], calibration='NONE_V1',
                        dependencies={n: importlib.metadata.version(n) for n in
                                      ('numpy', 'scipy', 'scikit-learn', 'lightgbm', 'joblib', 'threadpoolctl')})
        result[model_id] = dict(estimator=estimator, metadata=metadata)
    if not synthetic:
        completed = datetime.now(timezone.utc)
        if completed >= stamp(target_session + 'T08:45:00+09:00'):
            raise ValueError('MODEL_TRAINING_COMPLETED_AFTER_CUTOFF')
        for pack in result.values():
            pack['metadata']['created_at'] = completed.isoformat()
    validation = [r for _, group in folds for r in group]
    actual = [r['label'] for r in validation]
    benchmark_metrics = {}
    for series in ('base_rate', 'momentum', 'ma_trend', PRIMARY):
        probs = [{c: v / 100 for c, v in primary_predict(r['baselines'])[1].items()}
                 if series == PRIMARY else r['baselines'][series] for r in validation]
        benchmark_metrics[series] = offline_metrics(actual, probs)
    return result, dict(dataset_hash=dataset_hash, rows=len(rows),
                        folds=[_fold_description(t, v) for t, v in folds],
                        benchmark_metrics=benchmark_metrics, synthetic=synthetic)


def train_pit_dataset(destination, history, created_at, code_commit):
    """Real historical CV entry. No acquisition, Forecast ID, or Forward scoring."""
    from engine import parallel_training as training
    dataset = training.require_training_ready(training.read_dataset(destination))
    if not re.fullmatch('[a-f0-9]{40}', code_commit):
        raise ValueError('CODE_COMMIT_REQUIRED')
    target = dataset['target_session']
    if stamp(created_at) > datetime.now(timezone.utc) or stamp(created_at) >= stamp(target + 'T08:45:00+09:00'):
        raise ValueError('MODEL_TRAINING_AS_OF_INVALID')
    checksum = dataset['dataset_sha256']
    folds = training.plan_folds(dataset, history)
    # Supplied raw history must reproduce the exact persisted replay definitions.
    if training.dataset_sha256(dataset) != checksum:
        raise ValueError('CV_REPLAY_DATASET_CHANGED')
    final_rows = training.final_training_rows(dataset)
    config, packs = rules(), {}
    for model_id in (LOGISTIC, LIGHTGBM):
        candidates = [config['logistic']] if model_id == LOGISTIC else search_space()
        diagnostics = []
        for parameters in candidates:
            actual, probabilities, descriptions = [], [], []
            for fold in folds:
                train, validation = fold['train'], fold['validation']
                estimator = _fit(model_id, train, parameters)
                probs = predict_probabilities(model_id, estimator, validation)
                labels_ = [r['label'] for r in validation]
                descriptions.append(dict(_fold_description(train, validation),
                    fold_id=fold['definition']['fold_id'], label_threshold='CV_LABEL_THRESHOLD',
                    metrics=offline_metrics(labels_, probs)))
                actual.extend(labels_)
                probabilities.extend(probs)
            diagnostics.append(dict(hyperparameters=parameters,
                metrics=offline_metrics(actual, probabilities), folds=descriptions))
        chosen = min(range(len(diagnostics)), key=lambda i: diagnostics[i]['metrics']['brier'])
        parameters = diagnostics[chosen]['hyperparameters']
        estimator = _fit(model_id, final_rows, parameters)
        meta = dict(schema_version='parallel-model-freeze-v1', model_id=model_id,
            model_version=model_id, model_role='SHADOW', target_session=target,
            training_start=final_rows[0]['session'], training_end=final_rows[-1]['session'],
            training_rows=len(final_rows), feature_set_version=config['feature_set_versions'][model_id],
            features=list(PRICE9), hyperparameters=parameters,
            effective_parameters=(estimator.named_steps['classifier'].get_params() if model_id == LOGISTIC else estimator.params),
            threshold_version=config['threshold_version'], training_dataset_hash=checksum,
            walk_forward_summary=dict(status='MODEL_SELECTION_DIAGNOSTIC_NOT_UNBIASED_PERFORMANCE',
                selected_candidate=chosen, candidates=diagnostics), created_at=created_at, code_commit=code_commit,
            config_hash=specification_hash(), synthetic=False, seed=config['seed'], calibration='NONE_V1',
            dependencies={n: importlib.metadata.version(n) for n in
                ('numpy', 'scipy', 'scikit-learn', 'lightgbm', 'joblib', 'threadpoolctl')},
            training_provenance=dict(dataset_path=str(Path(destination).resolve()),
                dataset_metadata_sha256=core.sha256(Path(destination) / 'dataset.json'),
                training_dataset_sha256=checksum, cv_threshold_replay_version=training.REPLAY,
                CV_LABEL_THRESHOLD=dataset['cv_threshold_replays'],
                FINAL_MODEL_LABEL_THRESHOLD=dict(version=config['threshold_version'],
                    sha256=dataset['production_threshold_sha256']), immutable=True))
        packs[model_id] = dict(estimator=estimator, metadata=meta)
    completed = datetime.now(timezone.utc)
    if completed >= stamp(target + 'T08:45:00+09:00'):
        raise ValueError('MODEL_TRAINING_COMPLETED_AFTER_CUTOFF')
    for pack in packs.values():
        pack['metadata']['created_at'] = completed.isoformat()
        validate_model_metadata(pack['metadata'])
    # Existing frozen Primary tables were not known during early historical CV.
    # Avoid a retrospective Primary comparison; do not retrain its baselines.
    return packs, dict(dataset_sha256=checksum, folds=dataset['fold_definitions'],
        primary_comparison=dict(status='UNAVAILABLE', reason='FROZEN_PRIMARY_NOT_AVAILABLE_FOR_FULL_CV_WINDOW'),
        model_promotion=False)


def validate_model_metadata(meta):
    from engine.parallel_schema import validate
    validate(meta, 'model')
    config = rules()
    expected = {'schema_version', 'model_id', 'model_version', 'model_role', 'target_session',
                'training_start', 'training_end', 'training_rows', 'feature_set_version', 'features',
                'hyperparameters', 'effective_parameters', 'threshold_version', 'training_dataset_hash',
                'walk_forward_summary', 'created_at', 'code_commit', 'config_hash', 'synthetic', 'seed',
                'calibration', 'dependencies'}
    if 'training_provenance' in meta:
        expected.add('training_provenance')
        from engine import parallel_training as training
        proof = meta['training_provenance']
        dataset = training.require_training_ready(training.read_dataset(proof['dataset_path']))
        if (proof['training_dataset_sha256'] != dataset['dataset_sha256']
                or proof['dataset_metadata_sha256'] != core.sha256(Path(proof['dataset_path']) / 'dataset.json')
                or meta['training_dataset_hash'] != dataset['dataset_sha256']
                or proof['cv_threshold_replay_version'] != training.REPLAY
                or proof['CV_LABEL_THRESHOLD'] != dataset['cv_threshold_replays']
                or proof['FINAL_MODEL_LABEL_THRESHOLD'] != dict(version='short-oc-p33-v1', sha256=dataset['production_threshold_sha256'])
                or proof['immutable'] is not True or meta['synthetic']
                or meta['training_rows'] != len(dataset['eligible_rows'])
                or meta['target_session'] != dataset['target_session']):
            raise ValueError('MODEL_TRAINING_PROVENANCE_INVALID')
    elif meta['synthetic'] is not True:
        raise ValueError('PIT_TRAINING_PROVENANCE_REQUIRED')
    if set(meta) != expected or meta['config_hash'] != specification_hash():
        raise ValueError('MODEL_FREEZE_SCHEMA_OR_CONFIG_CHANGED')
    model_id = meta['model_id']
    if (model_id not in (LOGISTIC, LIGHTGBM) or meta['model_version'] != model_id
            or meta['model_role'] != 'SHADOW' or meta['features'] != list(PRICE9)
            or meta['feature_set_version'] != config['feature_set_versions'][model_id]
            or meta['threshold_version'] != config['threshold_version']
            or not hash_valid(meta['training_dataset_hash'])
            or not re.fullmatch('[a-f0-9]{40}', meta['code_commit'])
            or meta['seed'] != config['seed'] or meta['calibration'] != 'NONE_V1'
            or meta['training_end'] >= meta['target_session']
            or meta['training_start'] > meta['training_end']
            or stamp(meta['created_at']) >= stamp(meta['target_session'] + 'T08:45:00+09:00')):
        raise ValueError('MODEL_FREEZE_INVALID')
    params = meta['hyperparameters']
    if (model_id == LOGISTIC and params != config['logistic']) or (model_id == LIGHTGBM and params not in search_space()):
        raise ValueError('MODEL_PARAMETERS_NOT_FROZEN')
    effective = meta['effective_parameters']
    if model_id == LOGISTIC:
        if (any(effective.get(k) != v for k, v in params.items() if k != 'penalty')
                or effective.get('l1_ratio') != 0.0 or effective.get('random_state') != config['seed']):
            raise ValueError('LOGISTIC_EFFECTIVE_PARAMETERS_INVALID')
    elif (any(effective.get(k) != v for k, v in params.items())
          or effective.get('objective') != 'multiclass' or effective.get('num_class') != 3
          or effective.get('seed') != config['seed'] or effective.get('num_threads') != 1
          or effective.get('deterministic') is not True or effective.get('force_col_wise') is not True):
        raise ValueError('LIGHTGBM_EFFECTIVE_PARAMETERS_INVALID')
    selected = meta['walk_forward_summary']['selected_candidate']
    candidates = meta['walk_forward_summary']['candidates']
    if candidates[selected]['hyperparameters'] != params:
        raise ValueError('MODEL_SELECTION_MISMATCH')
    for fold in candidates[selected]['folds']:
        if not fold['training_end'] < fold['validation_start'] <= fold['validation_end'] < meta['target_session']:
            raise ValueError('MODEL_FOLD_LOOKAHEAD')


def freeze_model(pack, destination):
    """Create-only local model artifact; content hash and metadata bound together."""
    import joblib
    meta = pack['metadata']
    validate_model_metadata(meta)
    if not meta['synthetic'] and datetime.now(timezone.utc) >= stamp(meta['target_session'] + 'T08:45:00+09:00'):
        raise ValueError('MODEL_FREEZE_AFTER_TARGET_CUTOFF')
    estimator = pack['estimator']
    effective = (estimator.named_steps['classifier'].get_params() if meta['model_id'] == LOGISTIC else estimator.params)
    if effective != meta['effective_parameters']:
        raise ValueError('MODEL_ESTIMATOR_METADATA_MISMATCH')
    if meta['model_id'] == LOGISTIC:
        if (estimator.n_features_in_ != 9 or list(estimator.classes_) != [0, 1, 2]
                or estimator.named_steps['scaler'].n_samples_seen_ != meta['training_rows']):
            raise ValueError('LOGISTIC_TRAINING_SCHEMA_MISMATCH')
    elif estimator.num_feature() != 9:
        raise ValueError('LIGHTGBM_TRAINING_SCHEMA_MISMATCH')
    destination = Path(destination)
    name = 'estimator.joblib' if meta['model_id'] == LOGISTIC else 'estimator.txt'
    def build(stage):
        path = stage / name
        if meta['model_id'] == LOGISTIC:
            joblib.dump(pack['estimator'], path)
        else:
            pack['estimator'].save_model(str(path))
        storage._seal(stage / 'metadata.json', dict(meta, artifact_name=name, artifact_hash=core.sha256(path)))
    with storage._locked(destination.parent):
        storage._commit(destination, build)
    return destination


def load_model_freeze(destination):
    """Read trusted local artifacts only; never load externally supplied joblib files."""
    import joblib
    import lightgbm as lgb
    destination = Path(destination)
    record = core.read_frozen(destination / 'metadata.json')
    meta = {k: v for k, v in record.items() if k not in ('artifact_name', 'artifact_hash')}
    validate_model_metadata(meta)
    expected = 'estimator.joblib' if meta['model_id'] == LOGISTIC else 'estimator.txt'
    if record['artifact_name'] != expected or core.sha256(destination / expected) != record['artifact_hash']:
        raise ValueError('MODEL_ARTIFACT_HASH_MISMATCH')
    current = {n: importlib.metadata.version(n) for n in meta['dependencies']}
    if current != meta['dependencies']:
        raise ValueError('MODEL_DEPENDENCY_VERSION_MISMATCH')
    estimator = joblib.load(destination / expected) if meta['model_id'] == LOGISTIC else lgb.Booster(model_file=str(destination / expected))
    return dict(estimator=estimator, metadata=meta,
                freeze_ref=dict(path=str(destination), metadata_hash=core.sha256(destination / 'metadata.json'),
                                artifact_hash=record['artifact_hash']))

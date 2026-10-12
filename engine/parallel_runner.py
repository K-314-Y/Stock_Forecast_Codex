"""Same-snapshot forecast runner foundation. Live generation remains disabled in 10C-1."""
import json
from pathlib import Path

from engine import minimal_trial as core
from engine import one_session_dry_run as storage
from engine.parallel_data import (CLASSES, PRIMARY, LOGISTIC, LIGHTGBM, MODEL_ROLES,
                                  digest, rules, stamp, validate_snapshot, probability_valid)
from engine.parallel_models import load_model_freeze, predict_probabilities, validate_model_metadata
from models.linear_pool import predict as primary_predict


def run_forward(*args, **kwargs):
    """Reserved entry; cannot acquire, predict or freeze under the current Task mode."""
    raise ValueError('TASK10C1_LIVE_FORECAST_DISABLED')


def build_probe_bundle(snapshot, model_refs, generated_at, freeze_time):
    """Synthetic-only probe in memory, never a real Forecast ID or Forward entry."""
    validate_snapshot(snapshot)
    config = rules()
    if not snapshot['synthetic'] or snapshot['target_session'] == config['target_session']:
        raise ValueError('SYNTHETIC_NON_TARGET_PROBE_ONLY')
    if set(model_refs) != {LOGISTIC, LIGHTGBM}:
        raise ValueError('EXACTLY_TWO_SHADOWS_REQUIRED')
    created, frozen = stamp(generated_at), stamp(freeze_time)
    if not stamp(snapshot['created_at']) <= created <= frozen < stamp(snapshot['cutoff']):
        raise ValueError('BUNDLE_TIME_INVALID')
    loaded = {model_id: load_model_freeze(path) for model_id, path in model_refs.items()}
    rows = [dict(features=snapshot['stocks'][t]['features']) for t in core.CORE]
    outputs = {}
    for model_id, pack in loaded.items():
        meta = pack['metadata']
        if (meta['model_id'] != model_id or meta['target_session'] != snapshot['target_session']
                or not meta['synthetic'] or stamp(meta['created_at']) > created):
            raise ValueError('FROZEN_MODEL_NOT_ELIGIBLE')
        outputs[model_id] = predict_probabilities(model_id, pack['estimator'], rows)
    records, refs = [], {}
    for model_id, role in MODEL_ROLES.items():
        for index, ticker in enumerate(core.CORE):
            baselines = snapshot['stocks'][ticker]['baselines']
            if model_id == PRIMARY:
                raw, percent = primary_predict(baselines)
                probabilities = {c: percent[c] / 100 for c in CLASSES}
            else:
                raw = outputs[model_id][index]
                probabilities, percent = raw, None
            record = dict(schema_version='parallel-forecast-probe-v1', model_id=model_id,
                          model_version=model_id, model_role=role, ticker=ticker,
                          target_session=snapshot['target_session'],
                          common_snapshot_hash=snapshot['common_snapshot_hash'],
                          generated_at=generated_at, freeze_time=freeze_time,
                          probabilities=probabilities, raw_probabilities=raw, percent_probabilities=percent,
                          threshold_version=config['threshold_version'], baseline_version=config['baseline_version'],
                          feature_set_version=config['feature_set_versions'][model_id],
                          relative_strength=config['relative_strength'], synthetic=True,
                          exclude_from_trial_metrics=True)
            path = model_id + '_' + ticker + '.json'
            records.append(record)
            refs[path] = digest(record)
    manifest = dict(schema_version='parallel-forecast-bundle-v1', target_session=snapshot['target_session'],
                    common_snapshot_hash=snapshot['common_snapshot_hash'], primary_model=PRIMARY,
                    shadow_models=[LOGISTIC, LIGHTGBM], forecast_refs=refs,
                    model_freeze_refs={m: p['freeze_ref'] for m, p in loaded.items()},
                    generated_at=generated_at, freeze_time=freeze_time, synthetic=True,
                    exclude_from_trial_metrics=True, config_hash=digest(config),
                    promotion_allowed=False, outcome_status='NOT_ACQUIRED', scoring_status='NOT_RUN')
    bundle = dict(manifest=manifest, snapshot=snapshot, records=records)
    validate_bundle(bundle)
    return bundle


def validate_bundle(bundle):
    from engine.parallel_schema import validate
    validate(bundle, 'bundle')
    config = rules()
    snapshot, manifest, records = (bundle[k] for k in ('snapshot', 'manifest', 'records'))
    validate_snapshot(snapshot)
    if (manifest['schema_version'] != 'parallel-forecast-bundle-v1'
            or manifest['config_hash'] != digest(config)
            or manifest['primary_model'] != PRIMARY or manifest['shadow_models'] != [LOGISTIC, LIGHTGBM]
            or not snapshot['synthetic'] or manifest['synthetic'] is not True
            or manifest['target_session'] == config['target_session']
            or manifest['common_snapshot_hash'] != snapshot['common_snapshot_hash']
            or manifest['target_session'] != snapshot['target_session']
            or manifest['promotion_allowed'] is not False or manifest['outcome_status'] != 'NOT_ACQUIRED'
            or manifest['scoring_status'] != 'NOT_RUN' or manifest['exclude_from_trial_metrics'] is not True):
        raise ValueError('BUNDLE_SEMANTICS_INVALID')
    if not stamp(snapshot['created_at']) <= stamp(manifest['generated_at']) <= stamp(manifest['freeze_time']) < stamp(snapshot['cutoff']):
        raise ValueError('BUNDLE_TIME_INVALID')
    expected = {(m, t) for m in MODEL_ROLES for t in core.CORE}
    pairs = [(r['model_id'], r['ticker']) for r in records]
    if len(pairs) != 18 or set(pairs) != expected or len(set(pairs)) != len(pairs):
        raise ValueError('THREE_MODELS_CORE6_REQUIRED')
    refs = {}
    for r in records:
        m, ticker = r['model_id'], r['ticker']
        if (r['model_role'] != MODEL_ROLES[m] or r['model_version'] != m
                or r['target_session'] != manifest['target_session']
                or r['common_snapshot_hash'] != manifest['common_snapshot_hash']
                or r['generated_at'] != manifest['generated_at'] or r['freeze_time'] != manifest['freeze_time']
                or r['relative_strength'] != config['relative_strength']
                or r['feature_set_version'] != config['feature_set_versions'][m]
                or r['threshold_version'] != config['threshold_version']
                or r['baseline_version'] != config['baseline_version']
                or r['synthetic'] is not True or r['exclude_from_trial_metrics'] is not True
                or set(r) & {'outcome', 'actual_class', 'score', 'forecast_id', 'headline', 'body'}):
            raise ValueError('FORECAST_ROLE_OR_FREEZE_MISMATCH')
        probability_valid(r['probabilities'])
        probability_valid(r['raw_probabilities'])
        if m == PRIMARY:
            raw, percent = primary_predict(snapshot['stocks'][ticker]['baselines'])
            if (raw != r['raw_probabilities'] or percent != r['percent_probabilities']
                    or r['probabilities'] != {c: percent[c] / 100 for c in CLASSES}):
                raise ValueError('PRIMARY_FROZEN_POOL_MISMATCH')
        elif r['percent_probabilities'] is not None or r['probabilities'] != r['raw_probabilities']:
            raise ValueError('SHADOW_RAW_PROBABILITIES_REQUIRED')
        refs[m + '_' + ticker + '.json'] = digest(r)
    if manifest['forecast_refs'] != refs or set(manifest['model_freeze_refs']) != {LOGISTIC, LIGHTGBM}:
        raise ValueError('BUNDLE_FORECAST_REFERENCES_INVALID')


def freeze_probe_bundle(bundle, destination):
    """Atomic, create-only probe; never store synthetic entries in repo data/forecasts."""
    validate_bundle(bundle)
    destination = Path(destination).resolve()
    if destination.is_relative_to(core.ROOT.resolve()):
        raise ValueError('SYNTHETIC_PROBE_REQUIRES_EXTERNAL_TEMP_DIRECTORY')
    def build(stage):
        storage._seal(stage / 'snapshot.json', bundle['snapshot'])
        for r in bundle['records']:
            storage._seal(stage / (r['model_id'] + '_' + r['ticker'] + '.json'), r)
        storage._seal(stage / 'bundle.json', bundle['manifest'])
    with storage._locked(destination.parent):
        storage._commit(destination, build)
    return destination


def read_probe_bundle(destination):
    destination = Path(destination)
    manifest = core.read_frozen(destination / 'bundle.json')
    valid_names = {m + '_' + t + '.json' for m in MODEL_ROLES for t in core.CORE}
    if set(manifest['forecast_refs']) != valid_names:
        raise ValueError('BUNDLE_REFERENCE_ALLOWLIST')
    snapshot = core.read_frozen(destination / 'snapshot.json')
    records = [core.read_frozen(destination / name) for name in manifest['forecast_refs']]
    records.sort(key=lambda r: (list(MODEL_ROLES).index(r['model_id']), core.CORE.index(r['ticker'])))
    bundle = dict(manifest=manifest, snapshot=snapshot, records=records)
    validate_bundle(bundle)
    for model_id, ref in manifest['model_freeze_refs'].items():
        pack = load_model_freeze(ref['path'])
        if pack['metadata']['model_id'] != model_id or pack['freeze_ref'] != ref:
            raise ValueError('MODEL_FREEZE_REFERENCE_CHANGED')
    return bundle


def comparison_rows(bundle):
    """Prepare same-outcome join keys for six series, without outcomes or scores."""
    validate_bundle(bundle)
    result = [dict(target_session=r['target_session'], ticker=r['ticker'], series=r['model_id'],
                   probabilities=r['probabilities'], common_snapshot_hash=r['common_snapshot_hash'],
                   outcome_join_key=[r['target_session'], r['ticker'], r['threshold_version']],
                   outcome_status='PENDING_DATA', eligible=False) for r in bundle['records']]
    snap = bundle['snapshot']
    for ticker in core.CORE:
        for baseline, probs in snap['stocks'][ticker]['baselines'].items():
            result.append(dict(target_session=snap['target_session'], ticker=ticker, series=baseline,
                               probabilities=probs, common_snapshot_hash=snap['common_snapshot_hash'],
                               outcome_join_key=[snap['target_session'], ticker, snap['threshold_version']],
                               outcome_status='PENDING_DATA', eligible=False))
    return result

"""Real Primary Forward Trial entry. No acquisition, probe, ML, Outcome or scoring.

run_forward accepts only the Task 10B in-process capability. Final bundles are
create-only; runtime projections roll forward from sealed after-images after a
crash. recover_runtime generates no Forecast and observes no market data.
"""
import copy
import json
from pathlib import Path

from engine import execution_policy as policy
from engine import forward_trial_inputs as execution
from engine import forward_storage as storage
from engine import fresh_bridge as fresh
from engine import minimal_trial as core
from engine import prospective_pit as ledger
from engine.parallel_data import digest
from engine.primary_readiness import require_contract, config
from models.linear_pool import forward_predict, decision

RUNTIME_NAMES = ('current_state.json', 'recent_sessions.json', 'run_checklist.json', 'runtime_snapshot.json')
RECORD_FIELDS = set('schema_version forecast_id forecast_bundle_id target_session ticker model_id model_role model_version generated_at freeze_time scope terms_status feature_session feature_snapshot_hash common_input_hash threshold_version baseline_version raw_probabilities percent_probabilities probabilities predicted_class confidence relative_strength relative_strength_reason code_commit'.split())
RECEIPT_FIELDS = set('schema_version forecast_bundle_id target_session model_id model_version forecast_ids ticker_statuses common_input_hash feature_snapshot_hash baseline_version threshold_version generated_at freeze_time code_commit scope terms_status artifact_hashes bundle_sha256'.split())


def _store():
    return core.ROOT / config()['forecast_store']


def _forecast_id(bundle_id, ticker):
    return 'parallel_' + bundle_id.split('_')[1] + '_' + ticker + '_equal-linear-pool-v1_' + bundle_id.split('_')[2]


def _runtime_images(runtime_dir, receipt):
    before, after = {}, {}
    counts = set()
    for name in RUNTIME_NAMES:
        value = json.loads((runtime_dir / name).read_bytes())
        count = value.get('forecast_count', 0)
        if type(count) is not int or count < 0:
            raise ValueError('RUNTIME_FORECAST_COUNT_INVALID')
        counts.add(count)
        before[name] = core.sha256(runtime_dir / name)
        value.update(target_session=receipt['target_session'], forecast_generated=True, forecast_frozen=True,
            forecast_freeze=True, forecast_count=count + len(receipt['forecast_ids']),
            latest_forecast_id=receipt['forecast_ids'][-1], latest_forecast_bundle_id=receipt['forecast_bundle_id'],
            forecast_bundle_id=receipt['forecast_bundle_id'], forecast_generated_at=receipt['generated_at'],
            forecast_freeze_time=receipt['freeze_time'], formal_forecast_enabled=False, schedules_enabled=False,
            scoring_run=False, forecast_stage='FROZEN', scope=policy.SCOPE, terms_status=policy.TERMS)
        if isinstance(value.get('gates'), dict):
            value['gates'].update(formal_forecast_enabled=False, schedules_enabled=False,
                                  outcome_scoring_execution_enabled=False)
        after[name] = value
    if len(counts) != 1:
        raise ValueError('RUNTIME_FORECAST_COUNT_CONFLICT')
    return dict(schema_version='primary-runtime-transaction-v1',
                forecast_bundle_id=receipt['forecast_bundle_id'], before_hashes=before, after=after)


def _read_bundle(destination):
    """Verify every byte seal and cross-field relationship without activating gates."""
    destination = Path(destination)
    receipt = core.read_frozen(destination / 'receipt.json')
    if (set(receipt) != RECEIPT_FIELDS or receipt['schema_version'] != 'primary-forward-freeze-receipt-v1'
            or receipt['model_id'] != 'equal-linear-pool-v1' or receipt['model_version'] != 'equal-linear-pool-v1'
            or receipt['scope'] != policy.SCOPE or receipt['terms_status'] != policy.TERMS
            or receipt['baseline_version'] != 'minimal-baselines-v1' or receipt['threshold_version'] != 'short-oc-p33-v1'
            or receipt['bundle_sha256'] != digest({k: v for k, v in receipt.items() if k != 'bundle_sha256'})):
        raise ValueError('FREEZE_RECEIPT_INVALID')
    target = receipt['target_session']
    ledger.validate_bundle_id(receipt['forecast_bundle_id'], target)
    if target in policy.load_policy()['permanently_not_run_sessions']:
        raise ValueError('PERMANENTLY_NOT_RUN_SESSION')
    pre = core.read_frozen(destination / 'prospective.json')
    ledger.validate_pre_session(pre)
    rows = pre['rows']
    forecasts = []
    for row in rows:
        name = _forecast_id(receipt['forecast_bundle_id'], row['ticker']) + '.json'
        record = core.read_frozen(destination / name)
        if (set(record) != RECORD_FIELDS or record['schema_version'] != 'primary-forward-forecast-v1'
                or record['forecast_id'] + '.json' != name or record['model_role'] != 'PRIMARY'
                or record['ticker'] != row['ticker'] or record['target_session'] != target
                or record['feature_session'] != row['feature_session']
                or record['feature_snapshot_hash'] != row['common_snapshot_hash']
                or any(record[k] != receipt[k] for k in ('forecast_bundle_id', 'model_id', 'model_version', 'generated_at',
                     'freeze_time', 'scope', 'terms_status', 'common_input_hash', 'threshold_version', 'baseline_version', 'code_commit'))
                or record['relative_strength'] != 'missing_optional' or record['relative_strength_reason'] != 'RULE_NOT_FROZEN'):
            raise ValueError('FORECAST_RECORD_INVALID')
        percent = record['percent_probabilities']
        if (set(percent) != set(core.CLASSES) or sum(percent.values()) != 100
                or any(type(v) is not int or not 5 <= v <= 95 or v % 5 for v in percent.values())
                or record['probabilities'] != {c: percent[c] / 100 for c in core.CLASSES}
                or (record['predicted_class'], record['confidence']) != decision(percent)):
            raise ValueError('FORECAST_PROBABILITY_INVALID')
        from engine.parallel_data import probability_valid, stamp
        probability_valid(record['raw_probabilities'])
        if not stamp(row['feature_available_at']) <= stamp(record['generated_at']) <= stamp(record['freeze_time']) < stamp(target + 'T08:45:00+09:00'):
            raise ValueError('FORECAST_PIT_TIME_INVALID')
        if row['forecast_bundle_id'] != receipt['forecast_bundle_id'] or row['code_commit'] != receipt['code_commit']:
            raise ValueError('PROSPECTIVE_FORECAST_BINDING_INVALID')
        forecasts.append(record)
    if (receipt['forecast_ids'] != [r['forecast_id'] for r in forecasts]
            or len(set(receipt['forecast_ids'])) != len(forecasts)
            or receipt['ticker_statuses'] != pre['ticker_statuses']
            or receipt['feature_snapshot_hash'] != rows[0]['common_snapshot_hash']):
        raise ValueError('FREEZE_GRAIN_INVALID')
    names = {r['forecast_id'] + '.json' for r in forecasts} | {'prospective.json', 'input_audit.json', 'input_binding.json', 'baseline_snapshot.json', 'runtime_after.json'}
    if set(receipt['artifact_hashes']) != names:
        raise ValueError('BUNDLE_ARTIFACT_ALLOWLIST')
    expected_files = names | {'receipt.json'}
    expected_files |= {str(Path(n).with_suffix('.sha256')) for n in expected_files.copy()}
    if {p.name for p in destination.iterdir()} != expected_files:
        raise ValueError('BUNDLE_FILE_ALLOWLIST')
    for name, expected in receipt['artifact_hashes'].items():
        if core.sha256(destination / name) != expected:
            raise ValueError('BUNDLE_ARTIFACT_HASH_MISMATCH')
        core.read_frozen(destination / name)
    audit = core.read_frozen(destination / 'input_audit.json')
    source_hashes = core.read_frozen(destination / 'input_binding.json')
    snapshot = dict(schema_version='derived-fresh-features-v1', scope=policy.SCOPE,
                    target_session=target, previous_session=rows[0]['feature_session'],
                    stocks={r['ticker']: r['features'] for r in rows})
    if (pre['target_session'] != target or digest(snapshot) != receipt['feature_snapshot_hash']
            or set(audit) != {'schema_version', 'scope', 'target_session', 'stocks', 'ticker_statuses', 'forecast_bundle_id'}
            or audit['schema_version'] != 'ephemeral-fresh-audit-v1' or audit['scope'] != policy.SCOPE
            or audit['target_session'] != target or audit['ticker_statuses'] != pre['ticker_statuses']
            or audit['forecast_bundle_id'] != receipt['forecast_bundle_id']
            or set(audit['stocks']) != set(snapshot['stocks'])
            or digest(dict(snapshot=snapshot, audit=audit, source_hashes=source_hashes)) != receipt['common_input_hash']):
        raise ValueError('COMMON_INPUT_BINDING_INVALID')
    for row in rows:
        observed = audit['stocks'][row['ticker']]
        if (observed != dict(ticker=row['ticker'], previous_session=row['feature_session'], provider='BIGDATA',
               underlying_provider='FMP', retrieved_at=row['retrieved_at'], feature_available_at=row['feature_available_at'],
               provider_timestamp_present=True, session_validation='PASS', currency_validation='PASS', PIT_validation='PASS',
               source_response_sha256=row['source_hashes']['ephemeral_response'], target_session=target,
               feature_snapshot_sha256=receipt['feature_snapshot_hash'])):
            raise ValueError('OBSERVED_AUDIT_BINDING_INVALID')
    baselines = core.read_frozen(destination / 'baseline_snapshot.json')
    if set(baselines) != {r['ticker'] for r in rows}:
        raise ValueError('BASELINE_SNAPSHOT_GRAIN')
    # Verify pooling independently of any later model activation or ML state.
    from fractions import Fraction
    for record in forecasts:
        selected = baselines[record['ticker']]
        from engine.parallel_data import probability_valid
        if set(selected) != {'base_rate', 'momentum', 'ma_trend'}:
            raise ValueError('BASELINE_SNAPSHOT_INVALID')
        for p in selected.values():
            probability_valid(p)
        raw = {c: sum(Fraction(str(p[c])) for p in selected.values()) / 3 for c in core.CLASSES}
        final = {c: int(raw[c] * 100 // 5) * 5 for c in core.CLASSES}
        order = sorted(('FLAT', 'UP', 'DOWN'), key=lambda c: -(raw[c] * 100 - final[c]))
        for c in order[:(100 - sum(final.values())) // 5]:
            final[c] += 5
        if record['raw_probabilities'] != {c: float(raw[c]) for c in core.CLASSES} or record['percent_probabilities'] != final:
            raise ValueError('FROZEN_EQUAL_POOL_MISMATCH')
    transaction = core.read_frozen(destination / 'runtime_after.json')
    if (set(transaction) != {'schema_version', 'forecast_bundle_id', 'before_hashes', 'after'}
            or transaction['schema_version'] != 'primary-runtime-transaction-v1'
            or transaction['forecast_bundle_id'] != receipt['forecast_bundle_id']
            or set(transaction['before_hashes']) != set(RUNTIME_NAMES) or set(transaction['after']) != set(RUNTIME_NAMES)):
        raise ValueError('RUNTIME_TRANSACTION_INVALID')
    return receipt, transaction


def _project_runtime(destination, runtime_dir):
    receipt, transaction = _read_bundle(destination)
    # Preflight every file before making any projection. A crash may leave a
    # before/after mixture; unrelated runtime mutations are never overwritten.
    for name in RUNTIME_NAMES:
        actual = core.sha256(runtime_dir / name)
        if actual not in (transaction['before_hashes'][name], digest(transaction['after'][name])):
            raise ValueError('RUNTIME_RECOVERY_CONFLICT:' + name)
    for name in RUNTIME_NAMES:
        if core.sha256(runtime_dir / name) != digest(transaction['after'][name]):
            storage.replace_json(runtime_dir / name, transaction['after'][name])
    storage.replace_json(runtime_dir / 'primary_projection.json', dict(forecast_bundle_id=receipt['forecast_bundle_id'],
                          bundle_sha256=receipt['bundle_sha256'], target_session=receipt['target_session']))


def _recover_locked(store, runtime_dir):
    marker = runtime_dir / 'primary_projection.json'
    projected = json.loads(marker.read_bytes())['forecast_bundle_id'] if marker.exists() else None
    bundles = []
    for path in store.iterdir():
        if path.is_dir() and not path.name.startswith('.'):
            receipt, _ = _read_bundle(path)
            bundles.append((receipt['freeze_time'], receipt['forecast_bundle_id'], path))
    bundles.sort()
    if projected is not None:
        ids = [b[1] for b in bundles]
        if projected not in ids:
            raise ValueError('RUNTIME_PROJECTION_WITHOUT_FREEZE')
        bundles = bundles[ids.index(projected) + 1:]
    for _, _, path in bundles:
        _project_runtime(path, runtime_dir)


def recover_runtime(*, store=None, runtime_dir=None):
    store = Path(store or _store())
    runtime_dir = Path(runtime_dir or core.ROOT / 'state/runtime')
    if not store.exists():
        return
    with storage.locked(store), storage.locked(runtime_dir):
        _recover_locked(store, runtime_dir)


def run_forward(inputs, scope, target_session, *, store=None, runtime_dir=None, scheduled=False, formal=False):
    """Consume a fresh one-use token, freeze Primary only, then project runtime."""
    cfg = require_contract()
    policy.require_execution_scope(scope, target_session, scheduled=scheduled, formal=formal)
    policy.require_execution_clock(target_session)
    snapshot, audit, binding = execution.consume_execution_inputs(inputs, scope, target_session, with_binding=True)
    pre = core.read_frozen(binding['ledger_path'])
    ledger.validate_pre_session(pre)
    if (pre != ledger.build_pre_session(snapshot, audit,
             {k: v for k, v in binding['source_hashes'].items() if k not in
              {str(Path(binding['ledger_path']).parent / n) for n in ('features.json', 'audit.json', 'prospective.json')}}, pre['rows'][0]['code_commit'])
            or binding['bundle_id'] != audit['forecast_bundle_id'] or snapshot['scope'] != scope):
        raise ValueError('EXECUTION_LEDGER_BINDING_INVALID')
    commit = storage.code_commit()
    if commit != pre['rows'][0]['code_commit']:
        raise ValueError('CODE_CHANGED_AFTER_PREPARATION')
    thresholds, frozen_hashes = execution._frozen_candidates(snapshot['previous_session'])
    tables = json.loads((core.ROOT / 'config/trial_baseline_state_tables_candidate.json').read_bytes())['tables']
    generated = policy.require_execution_clock(target_session, binding['started']).isoformat()
    common_hash = digest(dict(snapshot=snapshot, audit=audit, source_hashes=binding['source_hashes']))
    baselines, forecasts = {}, []
    for row in pre['rows']:
        ticker, features = row['ticker'], row['features']
        if features['threshold'] != thresholds[ticker]['T_final']:
            raise ValueError('FROZEN_THRESHOLD_IDENTITY_MISMATCH')
        table = tables[ticker]
        selected = {name: copy.deepcopy(entry['probabilities']) for name, entry in (
            ('base_rate', table['base_rate']), ('momentum', table['momentum'][core.momentum_state(features['momentum_20d'])]),
            ('ma_trend', table['ma_trend'][features['MA_state']]))}
        raw, percent = forward_predict(selected)
        baselines[ticker] = selected
        predicted, confidence = decision(percent)
        forecasts.append(dict(schema_version='primary-forward-forecast-v1',
            forecast_id=_forecast_id(binding['bundle_id'], ticker), forecast_bundle_id=binding['bundle_id'],
            target_session=target_session, ticker=ticker, model_id=cfg['model_id'], model_role='PRIMARY',
            model_version=cfg['model_version'], generated_at=generated, freeze_time=generated,
            scope=scope, terms_status=policy.TERMS, feature_session=snapshot['previous_session'],
            feature_snapshot_hash=digest(snapshot), common_input_hash=common_hash,
            threshold_version=cfg['threshold_version'], baseline_version=cfg['baseline_version'],
            raw_probabilities=raw, percent_probabilities=percent, probabilities={c: percent[c] / 100 for c in core.CLASSES},
            predicted_class=predicted, confidence=confidence, relative_strength='missing_optional',
            relative_strength_reason='RULE_NOT_FROZEN', code_commit=commit))
    store, runtime_dir = Path(store or _store()), Path(runtime_dir or core.ROOT / 'state/runtime')
    with storage.locked(store), storage.locked(runtime_dir):
        # Do not mix recovery mutations into a new attempt's failure semantics.
        marker = runtime_dir / 'primary_projection.json'
        projected = json.loads(marker.read_bytes())['forecast_bundle_id'] if marker.exists() else None
        existing_ids = set()
        existing_target = False
        for path in store.iterdir():
            if not path.is_dir() or path.name.startswith('.'):
                continue
            old, _ = _read_bundle(path)
            existing_ids.update(old['forecast_ids'])
            if old['target_session'] == target_session:
                existing_target = True
        if existing_ids.intersection(r['forecast_id'] for r in forecasts):
            raise FileExistsError('DUPLICATE_FORECAST_ID')
        if existing_target:
            raise FileExistsError('TARGET_SESSION_ALREADY_FROZEN')
        finalized = [p for p in store.iterdir() if p.is_dir() and not p.name.startswith('.')]
        if finalized and projected != max((_read_bundle(p)[0] for p in finalized), key=lambda r: r['freeze_time'])['forecast_bundle_id']:
            raise ValueError('RUNTIME_RECOVERY_REQUIRED')
        destination = store / binding['bundle_id']
        def build(stage):
            frozen = policy.require_execution_clock(target_session, binding['started']).isoformat()
            for record in forecasts:
                record['freeze_time'] = frozen
            receipt = dict(schema_version='primary-forward-freeze-receipt-v1', forecast_bundle_id=binding['bundle_id'],
                target_session=target_session, model_id=cfg['model_id'], model_version=cfg['model_version'],
                forecast_ids=[r['forecast_id'] for r in forecasts], ticker_statuses=audit['ticker_statuses'],
                common_input_hash=common_hash, feature_snapshot_hash=digest(snapshot),
                baseline_version=cfg['baseline_version'], threshold_version=cfg['threshold_version'],
                generated_at=generated, freeze_time=frozen, code_commit=commit, scope=scope, terms_status=policy.TERMS)
            objects = {r['forecast_id'] + '.json': r for r in forecasts}
            objects.update({'prospective.json': pre, 'input_audit.json': audit, 'input_binding.json': binding['source_hashes'], 'baseline_snapshot.json': baselines,
                            'runtime_after.json': _runtime_images(runtime_dir, receipt)})
            for name, value in objects.items():
                storage.seal(stage / name, value)
            receipt['artifact_hashes'] = {name: core.sha256(stage / name) for name in objects}
            receipt['bundle_sha256'] = digest(receipt)
            storage.seal(stage / 'receipt.json', receipt)
        def confirm(stage):
            _, transaction = _read_bundle(stage)
            for name, before_hash in transaction['before_hashes'].items():
                if core.sha256(runtime_dir / name) != before_hash:
                    raise ValueError('RUNTIME_CHANGED_BEFORE_FREEZE')
            require_contract()
            policy.require_execution_scope(scope, target_session, scheduled=scheduled, formal=formal)
            execution.artifacts._unchanged(binding['source_hashes'])
            execution.artifacts._unchanged(frozen_hashes)
            if storage.code_commit() != commit:
                raise ValueError('CODE_CHANGED_BEFORE_FREEZE')
            policy.require_execution_clock(target_session, binding['started'])
        storage.commit(destination, build, confirm)
        # If this fails, the final receipt is authoritative. recover_runtime is
        # mandatory before another execution; no second Forecast is generated.
        _project_runtime(destination, runtime_dir)
    return destination

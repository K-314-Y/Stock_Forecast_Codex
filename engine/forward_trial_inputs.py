"""Ephemeral Fresh input preparation. No Forecast, Freeze, probabilities or scoring."""
import hashlib
import hmac
import json
import math
from weakref import WeakKeyDictionary
from datetime import datetime
from pathlib import Path

from engine import execution_policy as policy
from engine import fresh_bridge as fresh
from engine import historical_close_bridge as historical
from engine import minimal_trial as core
from engine import forward_artifacts as artifacts
from engine import partial_ticker as partial
from engine import forward_storage as storage


def _digest(value):
    return hashlib.sha256(core.canonical_bytes(value)).hexdigest()


def _history_path():
    return core.ROOT / policy.load_policy()['historical_snapshot']


def register_validated_history(preparation_path):
    """Create a history-only derivative from the independently anchored Task 10A.

    Never import the last Fresh row, prepared features, or provider responses.
    Existing JPX originals/derivatives and frozen candidates remain untouched.
    """
    rules = policy.load_policy()
    preparation_path = Path(preparation_path)
    if core.sha256(preparation_path) != rules['approved_preparation_sha256']:
        raise ValueError('HISTORICAL_PREPARATION_CHECKPOINT_MISMATCH')
    preparation = json.loads(preparation_path.read_bytes())
    ref = preparation['validated_close_history_reference']
    if core.sha256(ref['path']) != ref['sha256']:
        raise ValueError('VALIDATED_HISTORY_HASH_MISMATCH')
    source = json.loads(Path(ref['path']).read_bytes())
    if (preparation['history_through'] != rules['historical_through']
            or preparation['point_in_time_validation'] != 'PASS'
            or preparation['available_tickers'] != list(core.CORE)):
        raise ValueError('HISTORICAL_PREPARATION_INVALID')
    # The checkpoint authenticates the full source; the registered representation
    # contains only already validated Historical values, strictly before Fresh.
    stocks = {ticker: [dict(session_date=row['session_date'], close=row['close'])
                       for row in source['stocks'][ticker]
                       if row['session_date'] <= rules['historical_through']]
              for ticker in core.CORE}
    original_refs = json.loads(Path(preparation['source_hash_reference']['path']).read_bytes())
    if core.sha256(preparation['source_hash_reference']['path']) != preparation['source_hash_reference']['sha256']:
        raise ValueError('HISTORICAL_SOURCE_REFERENCE_HASH_MISMATCH')
    # Check originals without importing any provider response as execution values.
    historical_hashes = {path: digest for path, digest in original_refs.items()
                         if any(Path(path).is_relative_to(core.ROOT / folder) for folder in
                                ('data/outcome_input', 'data/outcome_parsed',
                                 'data/historical_close_bridge',
                                 'analysis/provider_test_results/yfinance_cache_2026-09-13'))}
    artifacts._unchanged(historical_hashes)
    record = dict(schema_version='validated-forward-history-v1',
                  historical_through=rules['historical_through'], stocks=stocks,
                  approved_preparation_sha256=rules['approved_preparation_sha256'],
                  registered_at=fresh._receipt_now().isoformat(),
                  source_hashes=historical_hashes, fresh_rows_excluded=True)
    record['hmac_sha256'] = hmac.new(historical._key(create=True),
                                   core.canonical_bytes(record), hashlib.sha256).hexdigest()
    path = _history_path()
    path.parent.parent.mkdir(parents=True, exist_ok=True)
    with artifacts._locked(path.parent.parent):
        artifacts._commit(path.parent, lambda stage: artifacts._seal(stage / path.name, record))
    return path


def load_validated_history(calendar, thresholds, execution_started):
    """Only the independently registered history-only snapshot can be loaded."""
    rules = policy.load_policy()
    path = _history_path()
    record = core.read_frozen(path)
    signed = dict(record)
    signature = signed.pop('hmac_sha256')
    expected = hmac.new(historical._key(), core.canonical_bytes(signed), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError('HISTORICAL_RECEIPT_AUTHENTICATION_FAILED')
    if (record['schema_version'] != 'validated-forward-history-v1'
            or record['approved_preparation_sha256'] != rules['approved_preparation_sha256']
            or record['historical_through'] != rules['historical_through']
            or record['fresh_rows_excluded'] is not True
            or fresh._receipt_time(record['registered_at']) > execution_started
            or set(record['stocks']) != set(core.CORE)):
        raise ValueError('HISTORICAL_INPUT_INVALID')
    artifacts._unchanged(record['source_hashes'])
    for ticker, rows in record['stocks'].items():
        dates = [row['session_date'] for row in rows]
        end = thresholds[ticker]['training_end_date']
        expected_days = [day for day in calendar['jp_sessions']
                         if end < day <= rules['historical_through']]
        if (len(rows) < 61 or dates != sorted(set(dates))
                or dates[-1] != rules['historical_through']
                or [day for day in dates if day > end] != expected_days
                or sum(day <= end for day in dates) != 61
                or any(set(row) != {'session_date', 'close'} or type(row['close']) not in (int, float)
                       or not math.isfinite(row['close']) or row['close'] <= 0 for row in rows)):
            raise ValueError('HISTORICAL_CONTINUITY_OR_NUMERIC_INVALID')
    return record['stocks'], {str(path): core.sha256(path), **record['source_hashes']}


def load_execution_history(calendar, thresholds, started, previous):
    """Extend only with authenticated registered Historical Close derivatives.

    No legacy Fresh raw or saved feature snapshots are eligible for extension.
    Missing registration aborts before any provider call.
    """
    stocks, hashes = load_validated_history(calendar, thresholds, started)
    stocks = {ticker: list(rows) for ticker, rows in stocks.items()}
    cutoff = fresh._receipt_time(started.astimezone(fresh.JST).date().isoformat() + 'T08:45:00+09:00')
    through = policy.load_policy()['historical_through']
    for day in calendar['jp_sessions']:
        if through < day < previous:
            path = historical._root() / day / 'record.json'
            if not path.exists():
                raise ValueError('VALIDATED_HISTORICAL_EXTENSION_MISSING:' + day)
            evidence = dict(source_role='FORECAST_FEATURE', source_kind='HISTORICAL_CLOSE_BRIDGE',
                            path=str(path), sha256=core.sha256(path), session_end=day)
            closes, refs = historical.verify_day(evidence, started, cutoff, previous)
            if set(closes) != set(core.CORE):
                raise ValueError('HISTORICAL_EXTENSION_GRAIN_INVALID')
            for ticker in core.CORE:
                session, close = closes[ticker]
                if session != day:
                    raise ValueError('HISTORICAL_EXTENSION_GRAIN_INVALID')
                stocks[ticker].append(dict(session_date=day, close=close))
            hashes.update(refs)
    return stocks, hashes


def _frozen_candidates(previous):
    hashes = {}
    bundles = {}
    for name in ('trial_thresholds_candidate', 'trial_baselines_candidate',
                 'trial_baseline_state_tables_candidate'):
        bundles[name] = artifacts._load(core.ROOT / ('config/' + name + '.json'), hashes)
    quality_path = core.ROOT / 'config/historical_data_quality_policy.json'
    quality_hash = core.sha256(quality_path)
    hashes[str(quality_path)] = quality_hash
    threshold_spec = artifacts._load(core.ROOT / 'config/threshold_spec.json', hashes)
    baseline_spec = artifacts._load(core.ROOT / 'config/baseline_spec.json', hashes)
    for bundle in bundles.values():
        if (bundle['status'] != 'CANDIDATE_FROZEN_PRETRIAL_NOT_ACTIVATED'
                or bundle['automatic_reestimation'] is not False
                or bundle['quality_policy_sha256'] != quality_hash
                or bundle['training_end_date'] > previous):
            raise ValueError('FROZEN_CANDIDATE_INVALID')
    thresholds, baselines, tables = bundles.values()
    if (len({b['training_end_date'] for b in bundles.values()}) != 1
            or tables['threshold_version'] != thresholds['threshold_version']
            or tables['baseline_version'] != baselines['baseline_version']
            or thresholds['threshold_version'] != threshold_spec['version']
            or tables['baseline_version'] != baseline_spec['version']):
        raise ValueError('FROZEN_BASELINE_VERSION_CONFLICT')
    for ticker in core.CORE:
        core.validate_frozen_threshold(thresholds['thresholds'][ticker], previous, ticker)
        if tables['tables'][ticker]['base_rate'] != baselines['baselines'][ticker]['base_rate']:
            raise ValueError('FROZEN_BASE_RATE_CONFLICT')
        missing = []
        table = tables['tables'][ticker]
        partial._distribution(table['base_rate'], 'base_rate', missing)
        for kind, states in (('momentum', ('POSITIVE', 'NEGATIVE', 'FLAT')),
                             ('ma_trend', ('BULL', 'BEAR', 'NEUTRAL'))):
            for state in states:
                partial._distribution(table.get(kind, {}).get(state), kind + '.' + state, missing)
        if missing:
            raise ValueError('FROZEN_BASELINE_TABLE_INCOMPLETE')
    # Read/validate only: no empirical calculations, selection, training or output.
    return thresholds['thresholds'], hashes


class _ExecutionInputs:
    """In-process, one-use inputs. Persisted snapshot dictionaries cannot replace it."""
    __slots__ = ('_snapshot', '_audit', '_started', '_used', '_snapshot_hash',
                 '_audit_hash', '_source_hashes', '_bundle_id', '_ledger_path', '__weakref__')

    def __init__(self, snapshot, audit, started, source_hashes):
        self._snapshot, self._audit, self._started, self._used = snapshot, audit, started, False
        self._snapshot_hash, self._audit_hash = _digest(snapshot), _digest(audit)
        self._source_hashes = dict(source_hashes)


_CURRENT_INPUTS = WeakKeyDictionary()


def consume_execution_inputs(inputs, scope, target_session, *, with_binding=False):
    policy.require_execution_scope(scope, target_session)
    now = policy.require_execution_clock(target_session)
    cutoff = fresh._receipt_time(target_session + 'T08:45:00+09:00')
    if (type(inputs) is not _ExecutionInputs or inputs not in _CURRENT_INPUTS
            or inputs._used or inputs._snapshot['target_session'] != target_session
            or now < inputs._started or now >= cutoff):
        raise ValueError('NEW_EXECUTION_INPUTS_REQUIRED')
    binding = _CURRENT_INPUTS.pop(inputs)
    inputs._used = True
    if ((_digest(inputs._snapshot), _digest(inputs._audit), inputs._started,
         inputs._source_hashes, inputs._bundle_id, inputs._ledger_path) != binding):
        raise ValueError('EXECUTION_INPUT_CHANGED_AFTER_DERIVATION')
    artifacts._unchanged(inputs._source_hashes)
    import copy
    if with_binding:
        return (copy.deepcopy(inputs._snapshot), copy.deepcopy(inputs._audit),
                dict(started=binding[2], source_hashes=copy.deepcopy(binding[3]),
                     bundle_id=binding[4], ledger_path=binding[5]))
    return copy.deepcopy(inputs._snapshot), copy.deepcopy(inputs._audit)


def prepare_execution_inputs(scope, target_session, fetch_by_ticker, store=None, *,
                             scheduled=False, formal=False):
    """Serialize preparations at target-session grain before any Fresh call."""
    from engine.primary_readiness import require_contract
    cfg = require_contract()
    policy.require_execution_scope(scope, target_session, scheduled=scheduled, formal=formal)
    policy.require_execution_clock(target_session)
    store = Path(store or core.ROOT / cfg['prospective_store'] / 'pre_session')
    with storage.locked(store):
        if (store / target_session).exists():
            raise FileExistsError('PROSPECTIVE_TARGET_TICKER_CREATE_ONLY')
        return _prepare_execution_inputs(scope, target_session, fetch_by_ticker, store,
                                         scheduled=scheduled, formal=formal)


def _prepare_execution_inputs(scope, target_session, fetch_by_ticker, store, *,
                              scheduled=False, formal=False):
    """Task 10C input entry: at most six Fresh calls, derived/audit-only storage.

    Callables are trusted existing connector invocations, never paths or payloads.
    This entry reserves a bundle identity but creates no Forecast/ID/Freeze/score.
    """
    policy.require_execution_scope(scope, target_session, scheduled=scheduled, formal=formal)
    from engine.primary_readiness import require_contract
    from engine import prospective_pit as ledger
    cfg = require_contract()
    commit = storage.code_commit()
    if (not isinstance(fetch_by_ticker, dict) or set(fetch_by_ticker) != set(core.CORE)
            or any(call is not None and not callable(call) for call in fetch_by_ticker.values())):
        raise ValueError('CORE6_NEW_FRESH_RETRIEVAL_REQUIRED')
    started = policy.require_execution_clock(target_session)
    cutoff = fresh._receipt_time(target_session + 'T08:45:00+09:00')
    if started.astimezone(fresh.JST).date().isoformat() != target_session or started >= cutoff:
        raise ValueError('ACTUAL_TARGET_DAY_PRE_CUTOFF_REQUIRED')
    hashes = {}
    calendar_path = core.ROOT / policy.load_policy()['calendar']
    calendar = policy.validated_calendar()
    hashes[str(calendar_path)] = core.sha256(calendar_path)
    previous = fresh.expected_previous(calendar, target_session)
    for path in ('config/forward_trial_execution_policy.json', 'config/minimal_forward_trial_config.json',
                 'config/primary_forward_trial_v1.json'):
        artifacts._load(core.ROOT / path, hashes)
    bridge_config = artifacts._load(core.ROOT / 'config/fresh_feature_bridge.json', hashes)
    if (bridge_config['BIGDATA_FRESH_BRIDGE_TECHNICALLY_VALIDATED'] is not True
            or bridge_config['provider'] != 'BIGDATA' or bridge_config['underlying_provider'] != 'FMP'
            or bridge_config['CURRENT_ACCESS_AVAILABLE'] is not True
            or bridge_config['PAYMENT_OR_UPGRADE_REQUIRED'] is not False
            or bridge_config['price_semantics'] != 'VALIDATED_AS_PREVIOUS_SESSION_CLOSE_FOR_MINIMAL_TRIAL'):
        raise ValueError('BRIDGE_UNVALIDATED_OR_STALE')
    thresholds, candidate_hashes = _frozen_candidates(previous)
    hashes.update(candidate_hashes)
    histories, history_hashes = load_execution_history(calendar, thresholds, started, previous)
    hashes.update(history_hashes)
    stocks, audits, statuses = {}, {}, {}
    for ticker in core.CORE:
        if fetch_by_ticker[ticker] is None:
            statuses[ticker] = dict(status=partial.SKIPPED, reason='REQUIRED_INPUT_ABSENT',
                                    missing_inputs=['fresh_retrieval'])
            continue
        ticket = fresh.capture_fresh_close(ticker, previous, fetch_by_ticker[ticker])
        close, audit = fresh.consume_fresh_close(ticket, ticker, previous, started, cutoff)
        rows = histories[ticker] + [dict(session_date=previous, close=close)]
        computed = core.features(rows, thresholds[ticker])
        # Allowlist persistence; core.features also returns previous_close.
        stocks[ticker] = {name: computed[name] for name in core.REQUIRED_FEATURES}
        available = policy.require_execution_clock(target_session, started)
        audits[ticker] = dict(audit, target_session=target_session, feature_available_at=available.isoformat())
        statuses[ticker] = dict(status='FORECAST_INPUT_READY')
        del ticket, close, rows, computed
    if not stocks:
        raise ValueError('NO_PROCESSABLE_TICKERS')
    snapshot = dict(schema_version='derived-fresh-features-v1', scope=scope,
                    target_session=target_session, previous_session=previous, stocks=stocks)
    snapshot_hash = _digest(snapshot)
    for audit in audits.values():
        audit['feature_snapshot_sha256'] = snapshot_hash
    bundle_id = ledger.new_bundle_id(target_session)
    audit_record = dict(schema_version='ephemeral-fresh-audit-v1', scope=scope,
                        target_session=target_session, stocks=audits, ticker_statuses=statuses,
                        forecast_bundle_id=bundle_id)
    destination = Path(store or core.ROOT / cfg['prospective_store'] / 'pre_session') / target_session
    pre = ledger.build_pre_session(snapshot, audit_record, hashes, commit)
    def build(stage):
        artifacts._seal(stage / 'features.json', snapshot)
        artifacts._seal(stage / 'audit.json', audit_record)
        artifacts._seal(stage / 'prospective.json', pre)
    def confirm(stage):
        now = fresh._receipt_now()
        if now < started or now >= cutoff:
            raise ValueError('INPUT_PREPARATION_MISSED_CUTOFF')
        policy.require_execution_scope(scope, target_session, scheduled=scheduled, formal=formal)
        artifacts._unchanged(hashes)
        require_contract()
        ledger.validate_pre_session(core.read_frozen(stage / 'prospective.json'))
        if storage.code_commit() != commit:
            raise ValueError('CODE_CHANGED_DURING_PREPARATION')
        policy.require_execution_clock(target_session, started)
    artifacts._commit(destination, build, confirm)
    inputs = _ExecutionInputs(snapshot, audit_record, started, hashes)
    inputs._bundle_id, inputs._ledger_path = bundle_id, str(destination / 'prospective.json')
    inputs._source_hashes.update({str(destination / name): core.sha256(destination / name)
                                 for name in ('features.json', 'audit.json', 'prospective.json')})
    _CURRENT_INPUTS[inputs] = (inputs._snapshot_hash, inputs._audit_hash, started,
                             dict(inputs._source_hashes), inputs._bundle_id, inputs._ledger_path)
    return inputs, destination

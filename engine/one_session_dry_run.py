"""One-session Base Rate pipeline probe; never a Formal/Trial/Shadow run.

Explicit phases only (no scheduler or acquisition):
  python3 -m engine.one_session_dry_run forecast --store DIR --target YYYY-MM-DD
      --ticker 6857 --inputs prepared.json
  python3 -m engine.one_session_dry_run settle --store DIR --key KEY --parsed JPX.json
  python3 -m engine.one_session_dry_run status --store DIR --key KEY

Prepared input: {target_session, feature_cutoff, stocks, market?, news?}.
stocks contains Core6 {features?, provenance: {evidence: [...]}}. Each evidence is
{source_role: FORECAST_FEATURE, source_kind: FRESH_CLOSE_BRIDGE, HISTORICAL_CACHE
 or HISTORICAL_CLOSE_BRIDGE,
 path, sha256, session_end, receipt?}. Bridge receipts are authenticated by the
acquisition producer; available_at/retrieved_at, if supplied, are assertions only.
Feature cutoff is the last completed session.
Thresholds and empirical Baselines are loaded from existing frozen candidates.
Features are computed using the existing functions; supplied features, if any,
must reproduce exactly. The probe does not learn or fetch any prices.

Artifacts are create-only, hash-sealed directory commits. A store binds exactly
one session. Uncommitted .pending-* directories are ignored after interruption.
Use a dedicated store (e.g. data/pipeline_probe/YYYY-MM-DD), never a history log.
"""
from contextlib import contextmanager
from datetime import datetime, time
import argparse
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import uuid
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator, FormatChecker
from engine import fresh_bridge as bridge
from engine import minimal_trial as core

JST = ZoneInfo('Asia/Tokyo')
MODEL_ID = 'BASE_RATE_PIPELINE_PROBE'
MODEL_VERSION = 'base-rate-pipeline-probe-v1'
SCHEMA = 'one-session-pipeline-probe-v1'


def _now():
    return datetime.now(JST)


def _stamp(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError('NAIVE_PROBE_TIME')
    return stamp.astimezone(JST)


def _digest(value):
    return hashlib.sha256(core.canonical_bytes(value)).hexdigest()


def _identity(record):
    return {k: record[k] for k in ('target_session', 'ticker', 'model_id',
                                   'model_version', 'forecast_cutoff')}


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def _locked(store):
    store = Path(store)
    store.mkdir(parents=True, exist_ok=True)
    with (store / '.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield store
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _seal(path, record):
    payload = core.canonical_bytes(record)
    with path.open('xb') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    with path.with_suffix('.sha256').open('x', encoding='ascii') as stream:
        stream.write(hashlib.sha256(payload).hexdigest() + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def _commit(destination, builder, confirm=None):
    if destination.exists():
        raise FileExistsError('PROBE_CREATE_ONLY:' + str(destination))
    stage = Path(tempfile.mkdtemp(prefix='.pending-', dir=destination.parent))
    try:
        builder(stage)
        # Existing writers' sidecars are also durable before the directory commit.
        for file in stage.iterdir():
            with file.open('rb') as stream:
                os.fsync(stream.fileno())
        if confirm is not None:
            confirm(stage)
        _sync_dir(stage)
        os.rename(stage, destination)
        _sync_dir(destination.parent)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _commit_record(destination, record):
    _commit(destination, lambda stage: _seal(stage / 'record.json', record))


def _source_dates(value, created):
    dates = []
    if isinstance(value, dict):
        if set(value) & {'actual_class', 'actual_open', 'actual_close', 'outcome', 'score'}:
            raise ValueError('OUTCOME_IN_FORECAST_INPUT')
        for key, item in value.items():
            if key in ('session_date', 'previous_session', 'training_end_date', 'session_end'):
                dates.append(datetime.fromisoformat(item).date().isoformat())
            elif key in ('available_at', 'retrieved_at', 'published_time') and item is not None:
                if _stamp(item) > created:
                    raise ValueError('POST_GENERATION_SOURCE_TIMESTAMP')
            else:
                dates.extend(_source_dates(item, created))
    elif isinstance(value, list):
        for item in value:
            dates.extend(_source_dates(item, created))
    return dates


def _evidence(stocks, previous, created, cutoff):
    hashes, prices = {}, {}
    for symbol, item in stocks.items():
        closes = {}
        provenance = item['provenance']
        if set(provenance) != {'evidence'} or not provenance['evidence']:
            raise ValueError('PROBE_INPUT_EVIDENCE_REQUIRED')
        ends = []
        for evidence in provenance['evidence']:
            required = {'source_role', 'source_kind', 'path', 'sha256', 'session_end'}
            if not required <= set(evidence) or set(evidence) - required - {'receipt', 'available_at', 'retrieved_at'}:
                raise ValueError('PROBE_INPUT_EVIDENCE_REQUIRED')
            if evidence['source_role'] != 'FORECAST_FEATURE':
                raise ValueError('OUTCOME_ONLY_SOURCE_NOT_FORECAST_FEATURE')
            path = Path(evidence['path']).resolve()
            if (path.suffix.lower() == '.pdf' or any(path.is_relative_to((core.ROOT / part).resolve())
                    for part in ('data/outcome_input', 'data/outcome_parsed', 'data/outcome_output'))):
                raise ValueError('OUTCOME_ONLY_SOURCE_NOT_FORECAST_FEATURE')
            if evidence['source_kind'] == 'FRESH_CLOSE_BRIDGE':
                observations, verified_hashes = bridge.verify_fresh_evidence(evidence, symbol, created, cutoff)
            elif evidence['source_kind'] == 'HISTORICAL_CACHE' and 'receipt' not in evidence:
                observations, verified_hashes = bridge.verify_cached_evidence(evidence, symbol, created, cutoff)
            elif evidence['source_kind'] == 'HISTORICAL_CLOSE_BRIDGE' and 'receipt' not in evidence:
                from engine import historical_close_bridge
                observations, verified_hashes = historical_close_bridge.verify(evidence, symbol, created, cutoff, previous)
            else:
                raise ValueError('UNSUPPORTED_PROBE_PRICE_EVIDENCE')
            dates = [day for day, close in observations]
            if not dates or max(dates) != evidence['session_end'] or max(dates) > previous:
                raise ValueError('INPUT_SESSION_LOOKAHEAD')
            hashes.update(verified_hashes)
            ends.append(max(dates))
            for day, close in observations:
                if type(close) not in (int, float) or not math.isfinite(close) or close <= 0:
                    raise ValueError('INVALID_PRICE_EVIDENCE')
                if day in closes and closes[day] != close:
                    raise ValueError('PRICE_EVIDENCE_CONFLICT')
                closes[day] = close
        if max(ends) != previous:
            raise ValueError('STALE_PROBE_PRICE_EVIDENCE')
        prices[symbol] = [{'session_date': day, 'close': close} for day, close in sorted(closes.items())]
    return hashes, prices


def _load(path, hashes):
    path = Path(path).resolve()
    payload = path.read_bytes()
    hashes[str(path)] = hashlib.sha256(payload).hexdigest()
    return json.loads(payload)


def _unchanged(hashes):
    if any(core.sha256(path) != expected for path, expected in hashes.items()):
        raise ValueError('INPUT_CHANGED_BEFORE_FREEZE')


def forecast_phase(store, target_session, ticker, inputs_path, forecast_id=None):
    """Create a NON_FORMAL probe now, before the current session's 08:45 deadline."""
    created = _now().astimezone(JST)
    cutoff = _stamp(target_session + 'T08:45:00+09:00')
    if created.date().isoformat() != target_session or created > cutoff:
        raise ValueError('PROBE_REQUIRES_ACTUAL_PRE_CUTOFF_SESSION')
    if ticker not in core.CORE:
        raise ValueError('CORE_ONLY')
    hashes = {}
    calendar = _load(core.ROOT / 'analysis/calendar_validation_2026-09-12.json', hashes)
    previous = bridge.expected_previous(calendar, target_session)
    prepared = _load(inputs_path, hashes)
    if (set(prepared) - {'target_session', 'feature_cutoff', 'stocks', 'market', 'news'}
            or prepared['target_session'] != target_session
            or set(prepared['stocks']) != set(core.CORE)):
        raise ValueError('INVALID_PROBE_PREPARED_INPUT')
    feature_cutoff = _stamp(prepared['feature_cutoff'])
    if (feature_cutoff.date().isoformat() != previous or feature_cutoff > created
            or feature_cutoff.time() < time(15, 30)):
        raise ValueError('FEATURE_CUTOFF_LOOKAHEAD_OR_INCOMPLETE_SESSION')
    evidence_hashes, prices = _evidence(prepared['stocks'], previous, created, cutoff)
    hashes.update(evidence_hashes)
    threshold_bundle = _load(core.ROOT / 'config/trial_thresholds_candidate.json', hashes)
    baseline_bundle = _load(core.ROOT / 'config/trial_baselines_candidate.json', hashes)
    table_bundle = _load(core.ROOT / 'config/trial_baseline_state_tables_candidate.json', hashes)
    baseline_spec = _load(core.ROOT / 'config/baseline_spec.json', hashes)
    policy = core.ROOT / 'config/historical_data_quality_policy.json'
    hashes[str(policy)] = core.sha256(policy)
    for bundle in (threshold_bundle, baseline_bundle, table_bundle):
        if (bundle['status'] != 'CANDIDATE_FROZEN_PRETRIAL_NOT_ACTIVATED'
                or bundle['automatic_reestimation'] is not False
                or bundle['quality_policy_sha256'] != hashes[str(policy)]
                or bundle['training_end_date'] > previous):
            raise ValueError('FROZEN_CANDIDATE_INVALID')
    if (baseline_bundle['baseline_version'] != baseline_spec['version']
            or table_bundle['baseline_version'] != baseline_spec['version']
            or table_bundle['training_end_date'] != baseline_bundle['training_end_date']
            or table_bundle['threshold_version'] != threshold_bundle['threshold_version']):
        raise ValueError('FROZEN_BASELINE_VERSION_CONFLICT')
    stocks = {}
    for symbol in core.CORE:
        item = prepared['stocks'][symbol]
        if set(item) not in ({'features', 'provenance'}, {'provenance'}):
            raise ValueError('INVALID_PROBE_STOCK_INPUT')
        supplied = item.get('features', {})
        if set(supplied) - core.ALLOWED_FEATURES:
            raise ValueError('INVALID_PROBE_FEATURE')
        threshold = threshold_bundle['thresholds'][symbol]
        expected = [day for day in calendar['jp_sessions']
                    if threshold['training_end_date'] < day <= previous]
        observed = [row['session_date'] for row in prices[symbol]
                    if row['session_date'] > threshold['training_end_date']]
        if observed != expected:
            raise ValueError('PROBE_CONTINUOUS_HISTORY_REQUIRED')
        # Reuse the existing formulas on hash-verified source prices, never trust
        # caller-supplied feature values or repair/retrain historical data.
        feature = core.features(prices[symbol], threshold)
        for field in ((*core.REQUIRED_FEATURES, 'previous_session', 'threshold_version') if supplied else ()):
            actual = supplied[field]
            if isinstance(feature[field], (int, float)):
                valid = type(actual) in (int, float) and math.isfinite(actual) and math.isclose(
                    actual, feature[field], rel_tol=1e-12, abs_tol=1e-12)
            else:
                valid = actual == feature[field]
            if not valid:
                raise ValueError('PREPARED_FEATURE_SOURCE_MISMATCH:' + field)
        feature['momentum_state'] = core.momentum_state(feature['momentum_20d'])
        tables = table_bundle['tables'][symbol]
        if tables['base_rate'] != baseline_bundle['baselines'][symbol]['base_rate']:
            raise ValueError('FROZEN_BASE_RATE_CONFLICT')
        selected = copy.deepcopy(bridge.select_baselines(feature, tables))
        for baseline in selected.values():
            p = baseline['probabilities']
            if (set(p) != set(core.CLASSES) or any(type(v) not in (int, float)
                    or not math.isfinite(v) or not 0 <= v <= 1 for v in p.values())
                    or not math.isclose(sum(p.values()), 1, abs_tol=1e-12)):
                raise ValueError('INVALID_FROZEN_BASELINE_PROBABILITIES')
        selected.update(training_end_date=baseline_bundle['training_end_date'],
                        threshold_version=threshold_bundle['threshold_version'],
                        baseline_version=baseline_bundle['baseline_version'])
        stocks[symbol] = dict(features=feature, threshold=threshold_bundle['thresholds'][symbol],
                              baselines=selected, provenance=item['provenance'])
    # The generation timestamp, rather than the later deadline, bounds admitted evidence.
    snapshot = core.compact_snapshot(target_session, previous, created.isoformat(), stocks,
                                     market=prepared.get('market'), news=prepared.get('news'))
    probabilities = stocks[ticker]['baselines']['base_rate']['probabilities']
    winner = max(('FLAT', 'UP', 'DOWN'), key=lambda k: probabilities[k])
    peak = max(probabilities.values()) * 100
    forecast = dict(status='PIPELINE_PROBE', eligibility='NON_FORMAL',
                    exclude_from_trial_metrics=True, schema_version=SCHEMA,
                    forecast_id=forecast_id or 'PROBE-' + uuid.uuid4().hex, forecast_version='v1',
                    target_session=target_session, ticker=ticker, created_at=created.isoformat(),
                    forecast_cutoff=cutoff.isoformat(), feature_cutoff=feature_cutoff.isoformat(),
                    feature_end=previous, model_id=MODEL_ID, model_version=MODEL_VERSION,
                    threshold_version=threshold_bundle['threshold_version'],
                    baseline_version=baseline_bundle['baseline_version'], input_hashes=hashes,
                    threshold=copy.deepcopy(stocks[ticker]['threshold']),
                    most_likely_class=winner, confidence='HIGH' if peak >= 60 else 'MEDIUM' if peak >= 45 else 'LOW',
                    top_factors=[], **{k: 100 * probabilities[k] for k in core.CLASSES},
                    **{'p_' + k.lower(): probabilities[k] for k in core.CLASSES})
    key = _digest(_identity(forecast))
    with _locked(store) as directory:
        session_path = directory / 'session'
        if session_path.exists() and core.read_frozen(session_path / 'record.json')['target_session'] != target_session:
            raise ValueError('ONE_SESSION_STORE_ONLY')
        destination = directory / key
        if destination.exists():
            raise FileExistsError('DUPLICATE_SESSION_TICKER_MODEL_CUTOFF')
        frozen = _now().astimezone(JST)
        if frozen < created or frozen > cutoff:
            raise ValueError('FREEZE_MISSED_CUTOFF')
        snapshot['freeze_time'] = frozen.isoformat()
        forecast.update(freeze_time=frozen.isoformat(), input_snapshot_hash=_digest(snapshot))
        _unchanged(hashes)
        core.validate_forecast(forecast, snapshot)
        if not session_path.exists():
            _commit_record(session_path, dict(target_session=target_session, status='PIPELINE_PROBE',
                                             eligibility='NON_FORMAL', schema_version=SCHEMA))
        def build(stage):
            core.write_test_snapshot(snapshot, stage)
            path = core.write_frozen(forecast, snapshot, stage)
            _seal(stage / 'commit.json', dict(forecast_file=path.name,
                                             forecast_sha256=core.sha256(path),
                                             snapshot_sha256=core.sha256(stage / 'snapshot.json')))
        def confirm(stage):
            # Observe the real clock AFTER Forecast and Snapshot are durable.
            confirmed = _now().astimezone(JST)
            if not frozen <= confirmed <= cutoff:
                raise ValueError('FREEZE_MISSED_CUTOFF')
            _seal(stage / 'freeze_confirmation.json', dict(confirmed_at=confirmed.isoformat(),
                   forecast_sha256=core.sha256(stage / (forecast['forecast_id'] + '.json'))))
        _commit(destination, build, confirm)
    return key


def _read_forecast(store, key):
    if len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
        raise ValueError('INVALID_PROBE_KEY')
    directory = Path(store) / key
    commit = core.read_frozen(directory / 'commit.json')
    name = commit['forecast_file']
    if Path(name).name != name or not name.endswith('.json'):
        raise ValueError('INVALID_PROBE_COMMIT')
    path = directory / name
    forecast, snapshot = core.read_frozen(path), core.read_frozen(directory / 'snapshot.json')
    confirmation = core.read_frozen(directory / 'freeze_confirmation.json')
    if (core.sha256(path) != commit['forecast_sha256']
            or core.sha256(directory / 'snapshot.json') != commit['snapshot_sha256']
            or _digest(_identity(forecast)) != key
            or confirmation['forecast_sha256'] != core.sha256(path)
            or not _stamp(forecast['freeze_time']) <= _stamp(confirmation['confirmed_at']) <= _stamp(forecast['forecast_cutoff'])
            or forecast['schema_version'] != SCHEMA or forecast['status'] != 'PIPELINE_PROBE'
            or forecast['target_session'] != core.read_frozen(Path(store) / 'session/record.json')['target_session']):
        raise ValueError('PROBE_COMMIT_HASH_OR_IDENTITY_MISMATCH')
    core.validate_forecast(forecast, snapshot)
    return directory, path, forecast, snapshot


def lifecycle(store, key):
    if not Path(store).is_dir():
        raise FileNotFoundError('PROBE_STORE_MISSING')
    with _locked(store) as directory:
        return _lifecycle(directory, key)


def _lifecycle(store, key):
    directory, path, forecast, snapshot = _read_forecast(store, key)
    state = 'WAITING_OUTCOME'
    if (directory / 'outcome').exists():
        record = core.read_frozen(directory / 'outcome/record.json')
        if record['forecast_sha256'] != core.sha256(path):
            raise ValueError('OUTCOME_FORECAST_HASH_MISMATCH')
        state = 'OUTCOME_FROZEN'
    if (directory / 'score').exists():
        record = core.read_frozen(directory / 'score/record.json')
        if (state != 'OUTCOME_FROZEN' or record['forecast_sha256'] != core.sha256(path)
                or record['outcome_sha256'] != core.sha256(directory / 'outcome/record.json')):
            raise ValueError('SCORE_LINK_HASH_MISMATCH')
        state = 'SCORED'
    return state


def _validated_jpx(parsed_path, target_session, now):
    parsed_hash = core.sha256(parsed_path)
    parsed = core.load_parsed_jpx(parsed_path)  # Includes the existing Linux raw resolver.
    manifest_path = core.ROOT / 'data/outcome_input/manual_jpx/retention_manifest.json'
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    entries = [entry for entry in manifest['entries']
               if entry.get('session_date') == target_session
               and entry.get('sha256') == parsed['source_file_hash']
               and entry.get('normalized_output_sha256') == parsed_hash]
    if (len(entries) != 1 or entries[0].get('parser_status') != 'PASS'
            or entries[0].get('schema_validation') != 'PASS'
            or entries[0].get('core6_count') != 6
            or parsed.get('source_file_hash_after') != parsed['source_file_hash']
            or parsed.get('manual_file_integrity') != 'PASS'
            or parsed.get('duplicate_count') != 0
            or parsed.get('session_date') != target_session
            or parsed.get('normalized_schema_validation') != 'PASS'
            or parsed.get('core6_extraction') != '6/6'):
        raise ValueError('JPX_MANIFEST_OR_VALIDATION_MISMATCH')
    close = _stamp(target_session + 'T15:30:00+09:00')
    if not close <= _stamp(parsed['retrieved_at']) <= now or _stamp(entries[0]['parsed_at']) > now:
        raise ValueError('JPX_OUTCOME_AVAILABILITY_INVALID')
    records = parsed['records']
    if len(records) != 6 or {r['ticker'] for r in records} != set(core.CORE):
        raise ValueError('JPX_CORE6_MISMATCH')
    schema = json.loads((core.ROOT / 'config/normalized_market_schema.json').read_text(encoding='utf-8-sig'))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    for row in records:
        validator.validate(row['normalized_observation'])
        if (row['session_date'] != target_session
                or row['source_file_hash'] != parsed['source_file_hash']
                or row['series_type'] != 'RAW_OHLC'
                or row['normalized_observation']['symbol'] != row['ticker']
                or row['normalized_observation']['exchange'] != 'XTKS'
                or row['normalized_observation']['currency'] != 'JPY'
                or row['normalized_observation']['series_type'] != 'RAW_OHLC'
                or row['normalized_observation']['normalization']['basis_session'] != row['session_date']
                or 'sha256=' + parsed['source_file_hash'] not in row['normalized_observation']['source_reference']['reference']
                or row['open_semantics'] != 'OFFICIAL_SESSION_OPEN'
                or row['close_semantics'] != 'OFFICIAL_SESSION_CLOSE'):
            raise ValueError('JPX_ROW_PROVENANCE_MISMATCH')
        o, h, l, c = (row[k] for k in ('open', 'high', 'low', 'close'))
        if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (o, h, l, c))
                or not 0 < l <= min(o, c) <= max(o, c) <= h
                or any(row[k] != row['normalized_observation'][k] for k in ('open', 'high', 'low', 'close'))
                or row['normalized_observation']['session_date'] != row['session_date']):
            raise ValueError('JPX_ROW_PRICE_MISMATCH')
    if core.sha256(parsed_path) != parsed_hash:
        raise ValueError('JPX_PARSED_CHANGED_DURING_VALIDATION')
    return parsed, dict(parsed_sha256=parsed_hash, manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                       raw_sha256=parsed['source_file_hash'])


def settle_phase(store, key, parsed_path=None):
    """Freeze validated JPX Outcome after close, then score only sealed artifacts."""
    with _locked(store) as directory:
        run, path, forecast, snapshot = _read_forecast(directory, key)
        now = _now().astimezone(JST)
        if now < _stamp(forecast['target_session'] + 'T15:30:00+09:00'):
            raise ValueError('OUTCOME_BEFORE_MARKET_CLOSE')
        state = _lifecycle(directory, key)
        if state == 'SCORED':
            return state
        forecast_hash = core.sha256(path)
        if state == 'WAITING_OUTCOME':
            if parsed_path is None:
                return state
            parsed, provenance = _validated_jpx(Path(parsed_path), forecast['target_session'], now)
            limit = snapshot['stocks'][forecast['ticker']]['threshold']['T_final']
            outcome = core.outcome_from_jpx(parsed, forecast['ticker'], forecast, limit)
            frozen_at = _now().astimezone(JST)
            if frozen_at < now:
                raise ValueError('PROBE_CLOCK_REVERSED')
            _commit_record(run / 'outcome', dict(status='PIPELINE_PROBE', eligibility='NON_FORMAL',
                           exclude_from_trial_metrics=True, forecast_sha256=forecast_hash,
                           threshold_version=forecast['threshold_version'], frozen_at=frozen_at.isoformat(),
                           outcome=outcome, **provenance))
        frozen_outcome = core.read_frozen(run / 'outcome/record.json')
        outcome_hash = core.sha256(run / 'outcome/record.json')
        outcome = frozen_outcome['outcome']
        limit = snapshot['stocks'][forecast['ticker']]['threshold']['T_final']
        if (frozen_outcome['threshold_version'] != forecast['threshold_version']
                or outcome['actual_class'] != core.classify(outcome['actual_close'] / outcome['actual_open'] - 1, limit)):
            raise ValueError('FROZEN_OUTCOME_THRESHOLD_MISMATCH')
        scored = core.score_frozen(path, run / 'snapshot.json', outcome)
        scored_at = _now().astimezone(JST)
        if scored_at < max(now, _stamp(frozen_outcome['frozen_at'])):
            raise ValueError('PROBE_CLOCK_REVERSED')
        _commit_record(run / 'score', dict(status='PIPELINE_PROBE', eligibility='NON_FORMAL',
                       exclude_from_trial_metrics=True, forecast_sha256=forecast_hash,
                       outcome_sha256=outcome_hash, scored_at=scored_at.isoformat(), score=scored))
        if core.sha256(path) != forecast_hash:
            raise ValueError('FORECAST_CHANGED_DURING_SETTLEMENT')
        return _lifecycle(directory, key)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('phase', choices=('forecast', 'settle', 'status'))
    parser.add_argument('--store', required=True)
    parser.add_argument('--target')
    parser.add_argument('--ticker', choices=core.CORE)
    parser.add_argument('--inputs')
    parser.add_argument('--key')
    parser.add_argument('--parsed')
    args = parser.parse_args()
    if args.phase == 'forecast':
        if not all((args.target, args.ticker, args.inputs)) or args.parsed or args.key:
            parser.error('forecast requires --target --ticker --inputs; no Outcome input allowed')
        print(forecast_phase(args.store, args.target, args.ticker, args.inputs))
    elif args.phase == 'settle':
        if not args.key:
            parser.error('settle requires --key')
        print(settle_phase(args.store, args.key, args.parsed))
    else:
        if not args.key:
            parser.error('status requires --key')
        print(lifecycle(args.store, args.key))


if __name__ == '__main__':
    main()

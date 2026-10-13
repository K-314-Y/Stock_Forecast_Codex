"""Audit existing numerical evidence without fetching, reparsing, or forecasting."""
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

from engine import historical_admission as admission
from engine import historical_quality as quality
from engine import minimal_trial as core
from engine import parallel_training as training

EVIDENCE = core.ROOT / 'config/historical_pit_source_evidence_v1.json'


def local_inventory(target_session, *, cache=core.CACHE, manifest_path=None, detailed=False):
    cache = Path(cache)
    manifest_path = Path(manifest_path or core.ROOT / 'data/outcome_input/manual_jpx/retention_manifest.json')
    hashes, history, candidates, summaries, groups, actions = {}, [], [], [], [], []

    def ref(path):
        path = Path(path).resolve()
        name = str(path.relative_to(core.ROOT))
        hashes[name] = core.sha256(path)
        return name

    def provenance(path, source, ticker, sessions, fields, kind, basis, retrieved, role):
        name = ref(path)
        groups.append(dict(source=source, provider=source, file_artifact=name, fields=fields,
            ticker=ticker, sessions=sessions, raw_or_derived=kind, adjustment_basis=basis,
            source_hash=hashes[name], existing_retrieved_at=retrieved,
            historical_vintage_known=False, role=role))
        return name

    for path in (training.POLICY, admission.POLICY, EVIDENCE, quality.POLICY_PATH,
                 core.ROOT / 'config/threshold_spec.json', core.ROOT / 'config/price_source_contract.md',
                 core.ROOT / 'config/storage_contract.md', core.ROOT / 'config/official_gap_policy.json',
                 core.ROOT / 'config/fresh_feature_bridge.json', core.ROOT / 'config/trial_thresholds_candidate.json',
                 core.ROOT / 'config/parallel_forward_trial_v1.json',
                 core.ROOT / 'engine/parallel_training.py', core.ROOT / 'engine/parallel_training_local.py',
                 core.ROOT / 'engine/historical_admission.py', core.ROOT / 'engine/historical_quality.py',
                 core.ROOT / 'engine/minimal_trial.py', manifest_path):
        ref(path)
    limits = json.loads((core.ROOT / 'config/trial_thresholds_candidate.json').read_bytes())['thresholds']
    for ticker in core.CORE:
        audit = quality.audit(ticker, cache=cache)
        raw_path = cache / (ticker + '_auto_adjust_false.csv')
        if core.sha256(raw_path) != limits[ticker]['source_file_sha256']:
            raise ValueError('HISTORICAL_RAW_CHANGED_FROM_FROZEN_SOURCE')
        meta_path = cache / (ticker + '_probe.json')
        normalized_path = cache / (ticker + '_split_normalized_candidate.json')
        refs = [ref(p) for p in (raw_path, meta_path, normalized_path)]
        meta = json.loads(meta_path.read_bytes())
        with raw_path.open(newline='') as handle:
            reader = csv.DictReader(handle)
            schema = admission.detect_yfinance_schema(reader.fieldnames, meta)
            raw_rows = list(reader)
        if schema['schema'] != 'PASS' or schema['source_identity'] != 'PASS':
            raise ValueError('HISTORICAL_SOURCE_SCHEMA_OR_IDENTITY_UNRESOLVED')
        normalized = json.loads(normalized_path.read_bytes())
        mapping = {(r['session_date'], r['symbol']): r for r in normalized}
        if len(mapping) != len(normalized) or len(mapping) != len(audit['records']):
            raise ValueError('HISTORICAL_NORMALIZED_JOIN_NOT_ONE_TO_ONE')
        source = admission.YAHOO
        sessions = [r['session_date'] for r in audit['records'] if r['session_date'] < target_session]
        split_events = [dict(session=r['Date'][:10], ratio=float(r['Stock Splits']))
                        for r in raw_rows if float(r['Stock Splits']) != 0]
        actions.append(dict(ticker=ticker, source_hash=hashes[refs[0]],
            split_events=split_events, dividend_events=sum(float(r['Dividends']) != 0 for r in raw_rows),
            reverse_splits_observed=sum(e['ratio'] < 1 for e in split_events),
            merger_basis_changes='UNKNOWN', listing_changes='UNKNOWN', other_capital_actions='UNKNOWN',
            full_action_coverage='UNKNOWN', no_event_is_not_no_action=True,
            known_quality_issues=[dict(session=r['session_date'], issues=sorted(r['issues'])) for r in audit['records'] if r['issues']],
            local_action_screen_warnings=dict(Counter(w for r in audit['records'] for w in r['warnings']))))
        provenance(raw_path, source, ticker, sessions, list(admission.FIELDS)[1:], 'LIBRARY_OUTPUT_NOT_RAW_HTTP',
                   schema['price_basis'], meta['retrieved_at'], 'HISTORICAL_FEATURE_AND_OUTCOME_CANDIDATE')
        provenance(meta_path, source, ticker, sessions, ['requested', 'retrieved_at', 'metadata'], 'ACQUISITION_METADATA',
                   'NOT_APPLICABLE', meta['retrieved_at'], 'SOURCE_IDENTITY_AND_OBSERVED_VINTAGE')
        provenance(normalized_path, source, ticker, sessions, ['open', 'high', 'low', 'close', 'volume'], 'DERIVED_PASSTHROUGH',
                   'SPLIT_NORMALIZED_CANDIDATE_NOT_VALIDATED', meta['retrieved_at'], 'LEGACY_BASIS_EVIDENCE')
        for suffix, fields in (('_auto_adjust_true_local.csv', ['Open', 'High', 'Low', 'Close', 'Volume']),
                               ('_total_return_reference.json', ['open', 'high', 'low', 'close', 'volume'])):
            provenance(cache / (ticker + suffix), source, ticker, sessions, fields, 'DERIVED_LOCAL_AUTO_ADJUST',
                       'TOTAL_RETURN_REFERENCE', meta['retrieved_at'], 'EXCLUDED_FROM_PRICE9_AND_OUTCOME')
        normalized_deltas = []
        for row in audit['records']:
            day = row['session_date']
            if day >= target_session:
                continue
            evidence = mapping.get((day, ticker))
            if evidence is None or evidence['provider'] != source or evidence['currency'] != 'JPY':
                raise ValueError('HISTORICAL_SOURCE_IDENTITY_UNRESOLVED')
            normalized_deltas.extend(admission.passthrough_deltas(row['values'], evidence, quality.policy()['numeric_tolerance']))
            bound = dict(status='OBSERVED', source=source, time_kind='RETRIEVAL',
                available_at=meta['retrieved_at'], evidence_sha256=hashes[refs[1]])
            basis = evidence['source_reference']['price_basis_status']
            # Current flags are kept as evidence. No automatic promotion from a clean OHLC.
            verified_basis = basis == 'VALIDATED' and evidence['primary_price_direction_eligible'] is True
            record = dict(target_session=day, ticker=ticker, source=source, values=row['values'],
                source_hash=hashes[refs[0]], source_identity='PASS',
                availability=bound, original_publication_availability='UNKNOWN',
                feature_integrity=verified_basis, feature_corporate_action='PASS' if verified_basis else 'UNKNOWN',
                outcome_integrity=not bool(row['issues']), outcome_source_admitted=False,
                derived_training_persistence_allowed=admission.policy()['persistence']['derived_training_artifact'],
                quality_issues=sorted(row['issues']), source_refs=refs, synthetic=False,
                split_sessions=[e['session'] for e in split_events],
                outcome_evidence=dict(source_identity='PASS', field_semantics=schema['field_semantics'],
                    same_basis='PASS' if verified_basis else 'UNKNOWN', corporate_action='PASS' if verified_basis else 'UNKNOWN',
                    revision_safety='UNKNOWN'))
            history.append(record)
            candidates.append(record)
        summaries.append(dict(source=source, ticker=ticker, rows=len(sessions),
            quality_excluded_rows=audit['summary']['excluded_rows'], schema=schema,
            normalized_serialization_difference_cells=len(normalized_deltas),
            normalized_max_absolute_difference=max(normalized_deltas, default=0),
            normalized_comparison_tolerance=quality.policy()['numeric_tolerance'],
            original_historical_availability='UNKNOWN', current_vintage_retrieved_at=meta['retrieved_at'],
            price_basis_statuses=sorted({r['source_reference']['price_basis_status'] for r in normalized}),
            availability_contract_version='yahoo-observed-cache-v1'))
    known = {(r['target_session'], r['ticker']) for r in history}
    manifest = json.loads(manifest_path.read_bytes())
    days = [e['session_date'] for e in manifest['entries']]
    if len(days) != len(set(days)):
        raise ValueError('DUPLICATE_JPX_MANIFEST_SESSION')
    for entry in manifest['entries']:
        day = entry['session_date']
        if day >= target_session:
            continue
        parsed_path = core.ROOT / entry['normalized_output_path']
        parsed = json.loads(parsed_path.read_bytes())
        raw_path = core._resolve_jpx_raw_source(parsed)
        if (core.sha256(raw_path) != parsed['source_file_hash']
                or core.sha256(parsed_path) != entry['normalized_output_sha256']
                or parsed['source_file_hash'] != entry['sha256']
                or parsed.get('source_file_hash_after') != parsed['source_file_hash']
                or parsed['session_date'] != day or parsed['duplicate_count'] != 0
                or parsed['core6_extraction'] != '6/6'
                or entry['parser_status'] != 'PASS' or entry['schema_validation'] != 'PASS'):
            raise ValueError('JPX_EXISTING_SOURCE_INTEGRITY_FAILED')
        refs = [ref(parsed_path), ref(raw_path), str(manifest_path.relative_to(core.ROOT))]
        records = parsed['records']
        if len(records) != 6 or {r['ticker'] for r in records} != set(core.CORE):
            raise ValueError('JPX_EXISTING_CORE6_GRAIN_FAILED')
        sources = {r['provider'] for r in records}
        if len(sources) != 1 or not sources <= admission.JPX:
            raise ValueError('JPX_OUTCOME_SOURCE_IDENTITY_UNRESOLVED')
        source = next(iter(sources))
        bound = (dict(status='OBSERVED', source=source, time_kind='RETRIEVAL',
                      available_at=entry['downloaded_at'], evidence_sha256=hashes[refs[-1]])
                 if entry.get('downloaded_at') else dict(status='UNKNOWN'))
        for row in records:
            norm = row['normalized_observation']
            if (row['session_date'] != day or row['provider'] != source
                    or norm['symbol'] != row['ticker'] or norm['session_date'] != day
                    or norm['currency'] != 'JPY' or norm['exchange'] != 'XTKS'
                    or row['source_file_hash'] != parsed['source_file_hash']
                    or any(row[k] != norm[k] for k in ('open', 'high', 'low', 'close', 'volume'))):
                raise ValueError('JPX_OUTCOME_SOURCE_IDENTITY_UNRESOLVED')
            values = {n: row[n] for n in ('open', 'high', 'low', 'close', 'volume')}
            numeric = (all(type(v) in (int, float) and math_isfinite_positive(v) for v in values.values())
                and values['low'] <= min(values['open'], values['close']) <= max(values['open'], values['close']) <= values['high'])
            action = row['corporate_action_status'] == 'VALIDATED'
            basis = norm['source_reference']['price_basis_status'] == 'VALIDATED'
            semantics = (row['open_semantics'] == 'OFFICIAL_SESSION_OPEN' and row['close_semantics'] == 'OFFICIAL_SESSION_CLOSE'
                         and row.get('semantics_status') == 'VALIDATED')
            record = dict(target_session=day, ticker=row['ticker'], source=source, values=values,
                source_hash=hashes[refs[0]], source_identity='PASS',
                availability=bound, original_publication_availability='UNKNOWN',
                feature_integrity=False, feature_corporate_action='PASS' if action and basis else 'UNKNOWN',
                outcome_integrity=numeric, outcome_source_admitted=False,
                derived_training_persistence_allowed=admission.policy()['persistence']['derived_training_artifact'],
                quality_issues=[] if numeric else ['INVALID_OHLC'], source_refs=refs, synthetic=False,
                outcome_evidence=dict(source_identity='PASS', field_semantics='PASS' if semantics else 'UNKNOWN',
                    same_basis='PASS' if basis else 'UNKNOWN', corporate_action='PASS' if action else 'UNKNOWN',
                    revision_safety='PASS' if basis and action and parsed['OUTCOME_SCORING_SOURCE_VALIDATED'] else 'UNKNOWN'))
            candidates.append(record)
            if (day, row['ticker']) not in known:
                history.append(record)
            for p, kind in ((parsed_path, 'DERIVED_AM_OPEN_PM_CLOSE'), (raw_path, 'RAW_OFFICIAL_REPORT')):
                provenance(p, source, row['ticker'], [day], ['open', 'high', 'low', 'close', 'volume'], kind,
                    'RAW_OHLC_CANDIDATE;' + norm['source_reference']['price_basis_status'],
                    parsed['retrieved_at'], 'OUTCOME_CANDIDATE;RETRIEVED_AT_IS_LOCAL_IMPORT_NOT_PUBLICATION')
        summaries.append(dict(source=source, session=day, rows=6,
            historical_availability=bound['status'], observed_current_vintage_download=entry.get('downloaded_at'),
            local_import_is_not_publication=parsed['retrieved_at_meaning'],
            corporate_action_statuses=sorted({r['corporate_action_status'] for r in records}),
            legacy_outcome_source_admitted=parsed['OUTCOME_SCORING_SOURCE_VALIDATED'],
            availability_contract_version='jpx-retained-observation-v1'))
    outcomes, comparisons = admission.select_outcomes(candidates)
    selected = {(r['target_session'], r['ticker']): r for r in outcomes}
    # Threshold replay reads the history. Outcome flags are synchronized, but the
    # feature series is never replaced by a preferred label provider.
    for row in history:
        outcome = selected[(row['target_session'], row['ticker'])]
        row['outcome_source_admitted'] = outcome['outcome_source_admitted'] and outcome['source'] == row['source']
    auxiliary = []
    for name in ('data/forward_trial_history/2026-10-08/history.json',
                 'analysis/provider_test_results/limited_yfinance_probe.py',
                 'providers/jp/yfinance/adapter.py', 'analysis/provider_test_results/analyze_yfinance_cache.py'):
        path = core.ROOT / name
        if path.exists():
            if name.endswith('/history.json'):
                registered = json.loads(path.read_bytes())
                for ticker, rows in registered['stocks'].items():
                    provenance(path, 'TASK_10B_REGISTERED_HISTORY', ticker,
                        [r['session_date'] for r in rows if r['session_date'] < target_session],
                        ['close'], 'DERIVED_CLOSE_ONLY', 'FORWARD_OPERATIONAL_COMPATIBILITY_ONLY',
                        None, 'EXCLUDED_FROM_HISTORICAL_TRAINING;REGISTRATION_IS_NOT_ORIGINAL_PUBLICATION')
            auxiliary.append(dict(artifact=ref(path), source_hash=hashes[name],
                role='LINEAGE_ONLY_NOT_ADMITTED_AS_HISTORICAL_OHLC',
                reason='Close-only registered forward history or acquisition/normalization code; not new historical vintage evidence'))
    detail = dict(version=admission.VERSION, source_provenance_groups=groups, corporate_actions=actions,
        outcome_candidates=len(candidates), selected_outcomes=len(outcomes), overlap=comparisons,
        source_outcome_admission=dict(Counter(admission.outcome_decision(r)['status'] for r in candidates)),
        auxiliary_inputs=auxiliary, availability_contracts=json.loads(EVIDENCE.read_bytes())['availability_contracts'],
        feature_proofs=admission.feature_proofs(), persistence=admission.policy()['persistence'],
        source_provenance_csv_sha256=hashlib.sha256(admission.provenance_csv(groups)).hexdigest())
    history.sort(key=lambda r: (r['target_session'], r['ticker']))
    return (history, outcomes, hashes, summaries, detail) if detailed else (history, hashes, summaries)


def math_isfinite_positive(value):
    import math
    return math.isfinite(value) and value > 0


def build_local_dataset(target_session, training_as_of):
    history, outcomes, hashes, provenance, detail = local_inventory(target_session, detailed=True)
    dates = sorted({r['target_session'] for r in history})
    calendar_path = core.ROOT / 'analysis/calendar_validation_2026-09-12.json'
    calendar = json.loads(calendar_path.read_bytes())
    hashes[str(calendar_path.relative_to(core.ROOT))] = core.sha256(calendar_path)
    dates = sorted(set(dates) | {s for s in calendar['jp_sessions'] if dates[0] <= s <= dates[-1]})
    features = training.replay_price_features(history, dates, target_session)
    dataset = training.build_dataset(features, outcomes, target_session, training_as_of, hashes)
    dataset['source_provenance'] = provenance
    dataset['admission_audit'] = detail
    dataset['calendar_provenance'] = dict(source=str(calendar_path.relative_to(core.ROOT)),
        older_period='EXISTING_OBSERVED_SOURCE_SEQUENCE;FULL_HISTORICAL_JPX_CALENDAR_NOT_VALIDATED',
        missing_observed_price_dates=sorted(set(dates) - {r['target_session'] for r in history}))
    admission.summarize(dataset)
    dataset['blockers'] = sorted({
        'BLOCKER_HISTORICAL_AVAILABILITY_AND_VINTAGE_NOT_ESTABLISHED'
            for r in dataset['excluded_rows'] if r['admission_checks']['historical_availability'] != 'PASS'} | {
        'BLOCKER_OUTCOME_SOURCE_AND_CORPORATE_ACTION_NOT_ADMITTED'
            for r in dataset['excluded_rows'] if r['admission_checks']['outcome_source_admitted'] != 'PASS'} | {
        'BLOCKER_PRICE9_ABSOLUTE_MA_HISTORICAL_BASIS_UNPROVEN'
            for r in dataset['excluded_rows'] if r['admission_checks']['revision_safety'] != 'PASS'})
    # Excluded warmup/unresolved rows do not veto an otherwise safe population.
    # These source causes block only an empty admitted population; folds decide
    # sufficiency of a nonempty admitted subset.
    if dataset['eligible_rows']:
        dataset['blockers'] = []
    training.plan_folds(dataset, history)
    return dataset


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target-session', required=True)
    parser.add_argument('--training-as-of', required=True)
    parser.add_argument('--output-root', type=Path, default=core.ROOT / 'data/training/parallel_models_v1')
    args = parser.parse_args()
    dataset = build_local_dataset(args.target_session, args.training_as_of)
    path = training.write_dataset(dataset, args.output_root)
    print(json.dumps(dict(artifact=str(path), dataset_sha256=training.dataset_sha256(dataset),
        readiness=dataset['readiness'], blockers=dataset['blockers'], pit_audit=dataset['pit_audit']), ensure_ascii=False, indent=2))
    return 2 if dataset['readiness'] == 'BLOCKED' else 0


if __name__ == '__main__':
    raise SystemExit(main())

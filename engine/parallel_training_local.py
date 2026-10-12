"""Read existing immutable numerical evidence; never fetch or parse JPX again."""
import json
from pathlib import Path

from engine import historical_quality as quality
from engine import minimal_trial as core
from engine import parallel_training as training


def local_inventory(target_session, *, cache=core.CACHE, manifest_path=None):
    """Source admission is read from existing evidence, never inferred from values."""
    cache = Path(cache)
    manifest_path = Path(manifest_path or core.ROOT / 'data/outcome_input/manual_jpx/retention_manifest.json')
    hashes, history, summaries = {}, [], []
    def ref(path):
        path = Path(path).resolve()
        name = str(path.relative_to(core.ROOT))
        hashes[name] = core.sha256(path)
        return name
    for path in (training.POLICY, quality.POLICY_PATH, core.ROOT / 'config/threshold_spec.json',
                 core.ROOT / 'config/price_source_contract.md', core.ROOT / 'config/storage_contract.md',
                 core.ROOT / 'config/official_gap_policy.json', core.ROOT / 'config/fresh_feature_bridge.json',
                 core.ROOT / 'config/trial_thresholds_candidate.json',
                 core.ROOT / 'config/parallel_forward_trial_v1.json',
                 core.ROOT / 'engine/parallel_training.py', core.ROOT / 'engine/parallel_training_local.py',
                 core.ROOT / 'engine/historical_quality.py', core.ROOT / 'engine/minimal_trial.py', manifest_path):
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
        normalized = json.loads(normalized_path.read_bytes())
        mapping = {(r['session_date'], r['symbol']): r for r in normalized}
        if len(mapping) != len(normalized) or len(mapping) != len(audit['records']):
            raise ValueError('HISTORICAL_NORMALIZED_JOIN_NOT_ONE_TO_ONE')
        source = 'YAHOO_FINANCE_YFINANCE'
        for row in audit['records']:
            day = row['session_date']
            if day >= target_session:
                continue
            evidence = mapping.get((day, ticker))
            if evidence is None or evidence['provider'] != source or evidence['currency'] != 'JPY':
                raise ValueError('HISTORICAL_SOURCE_IDENTITY_UNRESOLVED')
            # These timestamps bind only the retrieved Sep-13 vintage. Original
            # publication is UNKNOWN and never backdated to the market session.
            bound = dict(status='OBSERVED', source=source, time_kind='RETRIEVAL',
                available_at=meta['retrieved_at'], evidence_sha256=hashes[refs[1]])
            basis = evidence['source_reference']['price_basis_status']
            admitted = bool(evidence['primary_price_direction_eligible']) and basis == 'VALIDATED'
            history.append(dict(target_session=day, ticker=ticker, source=source,
                values=row['values'],
                availability=bound, original_publication_availability='UNKNOWN',
                feature_integrity=admitted, outcome_integrity=admitted,
                outcome_source_admitted=False, derived_training_persistence_allowed=False,
                quality_issues=sorted(row['issues']), source_refs=refs, synthetic=False))
        summaries.append(dict(source=source, ticker=ticker, rows=len(audit['records']),
            quality_excluded_rows=audit['summary']['excluded_rows'],
            original_historical_availability='UNKNOWN', current_vintage_retrieved_at=meta['retrieved_at'],
            price_basis_statuses=sorted({r['source_reference']['price_basis_status'] for r in normalized}),
            outcome_admission='RESEARCH_ONLY_NOT_VALIDATED_FOR_TRAINING_OUTCOME'))
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
                or parsed.get('source_file_hash_after') != parsed['source_file_hash']
                or parsed['session_date'] != day or parsed['duplicate_count'] != 0
                or parsed['core6_extraction'] != '6/6'):
            raise ValueError('JPX_EXISTING_SOURCE_INTEGRITY_FAILED')
        refs = [ref(parsed_path), ref(raw_path), str(manifest_path.relative_to(core.ROOT))]
        records = parsed['records']
        if len(records) != 6 or {r['ticker'] for r in records} != set(core.CORE):
            raise ValueError('JPX_EXISTING_CORE6_GRAIN_FAILED')
        sources = {r['provider'] for r in records}
        if len(sources) != 1 or not sources <= {'JPX_DAILY_REPORT', 'JPX_DAILY_REPORT_MANUAL'}:
            raise ValueError('JPX_OUTCOME_SOURCE_IDENTITY_UNRESOLVED')
        source = next(iter(sources))
        bound = (dict(status='OBSERVED', source=source, time_kind='RETRIEVAL',
                      available_at=entry['downloaded_at'], evidence_sha256=hashes[refs[-1]])
                 if entry.get('downloaded_at') else dict(status='UNKNOWN'))
        for row in records:
            if row['session_date'] != day or row['provider'] != source:
                raise ValueError('JPX_OUTCOME_SOURCE_IDENTITY_UNRESOLVED')
            if (day, row['ticker']) in known:
                # Existing Sep-11 overlap is a crosscheck, not a duplicate label
                # or a license to switch sources on rows where cache quality fails.
                continue
            history.append(dict(target_session=day, ticker=row['ticker'], source=source,
                values={n: row[n] for n in ('open', 'high', 'low', 'close', 'volume')},
                availability=bound, original_publication_availability='UNKNOWN',
                feature_integrity=False,
                outcome_integrity=(row['corporate_action_status'] == 'VALIDATED'
                    and row['normalized_observation']['source_reference']['price_basis_status'] == 'VALIDATED'),
                outcome_source_admitted=bool(parsed['OUTCOME_SCORING_SOURCE_VALIDATED']),
                derived_training_persistence_allowed=True, quality_issues=[], source_refs=refs, synthetic=False))
        summaries.append(dict(source=source, session=day, rows=6,
            historical_availability=bound['status'], observed_current_vintage_download=entry.get('downloaded_at'),
            local_import_is_not_publication=parsed['retrieved_at_meaning'],
            corporate_action_statuses=sorted({r['corporate_action_status'] for r in records}),
            outcome_source_admitted=parsed['OUTCOME_SCORING_SOURCE_VALIDATED']))
    return sorted(history, key=lambda r: (r['target_session'], r['ticker'])), hashes, summaries


def build_local_dataset(target_session, training_as_of):
    history, hashes, provenance = local_inventory(target_session)
    dates = sorted({r['target_session'] for r in history})
    calendar_path = core.ROOT / 'analysis/calendar_validation_2026-09-12.json'
    calendar = json.loads(calendar_path.read_bytes())
    hashes[str(calendar_path.relative_to(core.ROOT))] = core.sha256(calendar_path)
    # Retain known missing sessions (e.g. Sep-30); never shorten a 61-session
    # window by silently jumping over a missing price row.
    dates = sorted(set(dates) | {s for s in calendar['jp_sessions'] if dates[0] <= s <= dates[-1]})
    features = training.replay_price_features(history, dates, target_session)
    dataset = training.build_dataset(features, history, target_session, training_as_of, hashes)
    dataset['source_provenance'] = provenance
    dataset['calendar_provenance'] = dict(source=str(calendar_path.relative_to(core.ROOT)),
        older_period='EXISTING_OBSERVED_SOURCE_SEQUENCE;FULL_HISTORICAL_JPX_CALENDAR_NOT_VALIDATED',
        missing_observed_price_dates=sorted(set(dates) - {r['target_session'] for r in history}))
    dataset['blockers'] = [
        'BLOCKER_HISTORICAL_AVAILABILITY_AND_VINTAGE_NOT_ESTABLISHED',
        'BLOCKER_OUTCOME_SOURCE_AND_CORPORATE_ACTION_NOT_ADMITTED']
    # Fail closed even if a local field was edited to look permissive. This adapter
    # has no approved historical training source contract in the current task.
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
                          readiness=dataset['readiness'], blockers=dataset['blockers'],
                          pit_audit=dataset['pit_audit']), ensure_ascii=False, indent=2))
    return 2 if dataset['readiness'] == 'BLOCKED' else 0


if __name__ == '__main__':
    raise SystemExit(main())

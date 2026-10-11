"""One-time local JPX gap validation; never downloads or edits source/cache data."""
import json
from pathlib import Path

from engine import historical_quality as quality
from engine import minimal_trial as base
from engine import official_gap as gap

ROOT = base.ROOT
INBOX = ROOT / 'data/outcome_input/manual_jpx'
PARSED = ROOT / 'data/outcome_parsed/jpx'
LAYER = ROOT / 'data/fresh_bridge/jp/official_gap'
BOOTSTRAP = set(gap.MISSING_ALLOWLIST)


def parsed_path(day):
    if day in BOOTSTRAP:
        return PARSED / f'{day}_core6_manual_bootstrap.json'
    return PARSED / ({'2026-09-15': '2026-09-15_core6_validation_2026-09-18.json',
                      '2026-09-18': '2026-09-18_core6_one_shot_r2.json'}[day])


def validate(output_path):
    calendar = json.loads((ROOT / 'analysis/calendar_validation_2026-09-12.json').read_text(encoding='utf-8'))
    manifest = json.loads((INBOX / 'retention_manifest.json').read_text(encoding='utf-8'))
    thresholds = json.loads((ROOT / 'config/trial_thresholds_candidate.json').read_text(encoding='utf-8'))['thresholds']
    expected = gap.expected_gap_sessions(calendar)
    entries = {entry['session_date']: entry for entry in manifest['entries']}
    if len(entries) != len(manifest['entries']) or any(day not in entries for day in expected):
        raise ValueError('MANIFEST_MISSING_OR_DUPLICATE_SESSION')
    cache_hashes = {ticker: base.sha256(base.CACHE / f'{ticker}_auto_adjust_false.csv') for ticker in base.CORE}
    raw_hashes = {day: base.sha256(INBOX / entries[day]['filename']) for day in expected}
    layers = []
    LAYER.mkdir(parents=True, exist_ok=True)
    for day in expected:
        destination = LAYER / f'{day}_core6.json'
        if destination.exists():
            layer = json.loads(destination.read_text(encoding='utf-8'))
            if layer['source_hash'] != raw_hashes[day] or layer['parsed_output_hash'] != base.sha256(parsed_path(day)):
                raise ValueError('EXISTING_LAYER_PROVENANCE_CONFLICT')
        else:
            layer = gap.stage_existing_pdf(parsed_path(day), manifest, calendar, INBOX, destination)
        layers.append(layer)
    continuity = gap.continuity(calendar, layers)
    if continuity['status'] != 'CONTINUOUS':
        raise ValueError('GAP_REMAINS')
    candidates = {}
    for ticker in base.CORE:
        history = quality.audit(ticker)['records']
        candidates[ticker] = gap.candidate_features(history, layers, calendar, ticker, thresholds[ticker])
        candidates[ticker]['boundary_extreme_candidate'] = (
            abs(candidates[ticker]['boundary_return_09_11_to_09_14']) >=
            quality.policy()['extreme_close_to_close_change_candidate_abs'])
        if base.sha256(base.CACHE / f'{ticker}_auto_adjust_false.csv') != cache_hashes[ticker]:
            raise ValueError('HISTORICAL_CACHE_MUTATED')
    if any(base.sha256(INBOX / entries[day]['filename']) != raw_hashes[day] for day in expected):
        raise ValueError('RAW_PDF_MUTATED')
    result = {'phase': '2C-4C', 'run_mode': 'FREE_FORWARD_TRIAL_PREP',
              'phase_status': 'PASS', 'gap_continuity': continuity,
              'historical_cache_hashes': cache_hashes, 'raw_pdf_hashes': raw_hashes,
              'bootstrap_sessions': sorted(BOOTSTRAP), 'reused_sessions': list(gap.EXISTING_SESSIONS),
              'candidates': candidates, 'core6_feature_build_count': len(candidates),
              'source_adjustment_basis_compatibility': 'UNVERIFIED',
              'HISTORICAL_TO_OFFICIAL_BRIDGE_READY': True,
              'BIGDATA_FRESH_BRIDGE_TECHNICALLY_VALIDATED': False,
              'LIVE_FEATURE_SNAPSHOT_TECHNICALLY_READY': False,
              'READY_FOR_LIVE_DRY_RUN': 'NO', 'TERMS_STATUS': 'UNRESOLVED',
              'forecast_generated': False, 'new_provider_calls': 0}
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write('\n')
    return result


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('output')
    args = parser.parse_args()
    outcome = validate(args.output)
    print(json.dumps({'phase_status': outcome['phase_status'],
                      'continuous_through': outcome['gap_continuity']['through'],
                      'core6_feature_build_count': outcome['core6_feature_build_count']}))

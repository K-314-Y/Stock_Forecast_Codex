"""Create-only local Phase 2C-3 artifacts. No network, forecasts, or schedules."""
import json
from datetime import datetime, timezone

from engine import minimal_trial as engine
from engine import historical_quality as quality


def main():
    targets = {
        'quarantine': engine.ROOT / 'data/quality/historical_quarantine_manifest.json',
        'thresholds': engine.ROOT / 'config/trial_thresholds_candidate.json',
        'baselines': engine.ROOT / 'config/trial_baselines_candidate.json',
        'summary': engine.ROOT / 'analysis/provider_test_results/historical_data_quality_trial_freeze_2026-09-25.json',
    }
    if any(path.exists() for path in targets.values()):
        raise FileExistsError('Phase outputs already exist; do not overwrite historical evidence')
    now = datetime.now(timezone.utc).isoformat()
    rules = quality.policy()
    policy_hash = engine.sha256(quality.POLICY_PATH)
    audits = {ticker: quality.audit(ticker, detected_at=now, rules=rules) for ticker in engine.CORE}
    candidates = {ticker: quality.training_candidates(audits[ticker], rules) for ticker in engine.CORE}
    if any(a['summary']['data_gate'] != 'PASS_OBSERVED_ROWS' for a in audits.values()):
        raise ValueError('HISTORICAL_DATA_GATE_FAILED')
    if len({candidates[t][0]['training_end_date'] for t in engine.CORE}) != 1:
        raise ValueError('TRAINING_END_CONFLICT')
    quarantine = {'schema_version': '1.0', 'policy_version': rules['policy_version'],
                  'policy_sha256': policy_hash, 'detected_at': now,
                  'raw_cache_immutable': True, 'repair_performed': False,
                  'entries': [q for ticker in engine.CORE for q in audits[ticker]['quarantine']]}
    threshold_bundle = {'schema_version': '1.0', 'status': 'CANDIDATE_FROZEN_PRETRIAL_NOT_ACTIVATED',
                        'threshold_version': 'short-oc-p33-v1', 'quality_policy_sha256': policy_hash,
                        'training_end_date': candidates[engine.CORE[0]][0]['training_end_date'],
                        'thresholds': {ticker: candidates[ticker][0] for ticker in engine.CORE},
                        'automatic_reestimation': False, 'human_start_approval': False}
    baseline_bundle = {'schema_version': '1.0', 'status': 'CANDIDATE_FROZEN_PRETRIAL_NOT_ACTIVATED',
                       'baseline_version': 'minimal-baselines-v1', 'quality_policy_sha256': policy_hash,
                       'training_end_date': threshold_bundle['training_end_date'],
                       'baselines': {ticker: candidates[ticker][1] for ticker in engine.CORE},
                       'automatic_reestimation': False, 'human_start_approval': False}
    all_current = all(audits[t]['summary']['current_60_session_window'] == 'PASS'
                      and audits[t]['summary']['current_atr14_window'] == 'PASS' for t in engine.CORE)
    summary = {'phase': '2C-3', 'run_mode': 'FREE_FORWARD_TRIAL_PREP',
               'audit_status': 'PASS_OBSERVED_ROWS', 'audited_at': now,
               'policy_sha256': policy_hash,
               'core6': {ticker: audits[ticker]['summary'] for ticker in engine.CORE},
               'quarantined_rows': len(quarantine['entries']),
               'threshold_candidates': len(threshold_bundle['thresholds']),
               'baseline_candidates': len(baseline_bundle['baselines']),
               'current_cached_feature_windows_valid': all_current,
               'technical_data_gate': 'PASS_OBSERVED_ROWS' if all_current else 'FEATURE_INPUT_BLOCKED',
               'full_historical_calendar': rules['full_historical_jpx_calendar_status'],
               'terms_gate': 'UNRESOLVED', 'live_feature_freshness': 'NOT_VALIDATED_STALE_CACHE',
               'ready_for_one_session_live_dry_run': False,
               'free_forward_trial_enabled': False, 'daily_acquisition_enabled': False,
               'schedules_enabled': False, 'automatic_learning_enabled': False,
               'automatic_promotion_enabled': False, 'raw_cache_changed': False,
               'new_provider_requests': 0}
    quality.write_candidate(targets['quarantine'], quarantine)
    quality.write_candidate(targets['thresholds'], threshold_bundle)
    quality.write_candidate(targets['baselines'], baseline_bundle)
    quality.write_candidate(targets['summary'], summary)
    print(json.dumps({'quarantined_rows': len(quarantine['entries']),
                      'threshold_candidates': len(threshold_bundle['thresholds']),
                      'baseline_candidates': len(baseline_bundle['baselines']),
                      'current_cached_feature_windows_valid': all_current}, ensure_ascii=False))


if __name__ == '__main__':
    main()

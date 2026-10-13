"""Primary gates are independent of historical Shadow ML admission."""
import json
from engine import execution_policy as policy
from engine import minimal_trial as core
from engine import fresh_bridge as fresh


def config():
    return json.loads((core.ROOT / 'config/primary_forward_trial_v1.json').read_bytes())


def require_contract():
    cfg = config()
    if (cfg['model_id'] != 'equal-linear-pool-v1' or cfg['model_version'] != 'equal-linear-pool-v1'
            or cfg['model_role'] != 'PRIMARY' or cfg['scope'] != policy.SCOPE
            or cfg['baseline_version'] != 'minimal-baselines-v1'
            or cfg['threshold_version'] != 'short-oc-p33-v1'
            or any(cfg[k] is not False for k in ('formal_forecast_enabled', 'schedules_enabled', 'scoring_enabled'))):
        raise ValueError('PRIMARY_CONTRACT_CONFLICT')
    spec = json.loads((core.ROOT / 'config/minimal_forward_trial_config.json').read_bytes())['forecast']
    if (spec['probability_step_percentage_points'] != 5 or spec['probability_sum'] != 100
            or spec['probability_bounds'] != [5, 95] or spec['tie_break_order'] != ['FLAT', 'UP', 'DOWN']
            or spec['confidence_rule'] != 'HIGH if max>=60; MEDIUM if max>=45; else LOW; not calibrated certainty'):
        raise ValueError('PRIMARY_FROZEN_PROBABILITY_RULE_CONFLICT')
    for path, digest in cfg['frozen_contract_hashes'].items():
        if core.sha256(core.ROOT / path) != digest:
            raise ValueError('PRIMARY_FROZEN_IDENTITY_MISMATCH:' + path)
    ref = cfg['execution_review']
    path = core.ROOT / ref['path']
    if core.sha256(path) != ref['sha256']:
        raise ValueError('TASK10B_EXECUTION_REVIEW_HASH_MISMATCH')
    review = json.loads(path.read_bytes())
    if (review['schema_version'] != 'forecast-execution-review-v1' or review['scope'] != cfg['scope']
            or review['terms_status'] != policy.TERMS or review['fresh_strategy'] != 'EPHEMERAL_RETRIEVAL'
            or review['raw_provider_persistence'] != 'DISABLED' or review['ready_for_task10c'] is not True
            or review['remaining_blockers'] or any(review['remaining_issue_counts'][k] for k in ('BLOCKER', 'HIGH'))
            or review['formal_forecast_enabled'] is not False or review['schedules_enabled'] is not False):
        raise ValueError('TASK10B_EXECUTION_REVIEW_INVALID')
    return cfg


def assess_primary(target_session=None):
    """Read only; READY requires the actual target-day clock and new input run."""
    from engine import forward_trial_inputs as inputs
    blockers = []
    try:
        target_session = target_session or policy.next_target_session()
        require_contract()
        policy.require_execution_scope(policy.SCOPE, target_session)
        calendar = policy.validated_calendar()
        previous = fresh.expected_previous(calendar, target_session)
        thresholds, _ = inputs._frozen_candidates(previous)
        inputs.load_execution_history(calendar, thresholds, fresh._receipt_now(), previous)
    except (ValueError, OSError, KeyError) as error:
        blockers.append(str(error))
    try:
        policy.require_execution_clock(target_session)
    except (ValueError, OSError, KeyError) as error:
        blockers.append(str(error))
    return dict(readiness='READY' if not blockers else 'BLOCKED',
                gate='PRIMARY_FORWARD_READINESS', target_session=target_session,
                blockers=blockers, historical_ml_dependency=False,
                live_inputs='NEW_ONE_USE_EPHEMERAL_RETRIEVAL_REQUIRED')


def shadow_readiness():
    return dict(gate='SHADOW_ML_TRAINING_READINESS', readiness='TRAINING_BLOCKED',
                models=config()['shadow_training_status'], historical_admitted=0,
                historical_unknown=7434, prospective_training='NOT_RUN')

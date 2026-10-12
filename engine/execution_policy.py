"""Scope-aware gate for explicitly requested local research execution."""
import json

from engine import minimal_trial as core

SCOPE = 'LOCAL_PERSONAL_NONCOMMERCIAL_FORWARD_TRIAL'
TERMS = 'CONDITIONAL_PERSONAL_NONCOMMERCIAL'


def load_policy():
    return json.loads((core.ROOT / 'config/forward_trial_execution_policy.json').read_bytes())


def execution_gates(scope, target_session, *, scheduled=False, formal=False):
    policy = load_policy()
    config = json.loads((core.ROOT / 'config/minimal_forward_trial_config.json').read_bytes())
    allowed = (scope == SCOPE == policy.get('scope') == config.get('execution_scope')
               and target_session == policy.get('target_session')
               and policy.get('terms_status') == config.get('terms_status') == TERMS
               and policy.get('fresh_strategy') == 'EPHEMERAL_RETRIEVAL'
               and policy.get('raw_provider_persistence') == 'DISABLED'
               and policy.get('stored_fresh_forecast_reuse') is False
               and config.get('enabled') is True and not scheduled and not formal
               and all(policy.get(key) is True and config['gates'].get(key) is True
                       for key in ('forecast_execution_enabled', 'forward_trial_enabled'))
               and all(policy.get(key) is False and config['gates'].get(key) is False
                       for key in ('formal_forecast_enabled', 'schedules_enabled')))
    return dict(scope=scope, target_session=target_session, terms_status=policy.get('terms_status'),
                forecast_execution_enabled=allowed, forward_trial_enabled=allowed,
                forecast_allowed=allowed, trial_enabled=allowed,
                formal_forecast_enabled=False, schedules_enabled=False)


def require_execution_scope(scope, target_session, **kwargs):
    gates = execution_gates(scope, target_session, **kwargs)
    if not gates['forecast_execution_enabled']:
        raise ValueError('FORECAST_EXECUTION_GATE_CLOSED')
    return gates

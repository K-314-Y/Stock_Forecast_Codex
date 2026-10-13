"""Scope-aware gate for explicitly requested local research execution."""
import json
from datetime import date

from engine import minimal_trial as core

SCOPE = 'LOCAL_PERSONAL_NONCOMMERCIAL_FORWARD_TRIAL'
TERMS = 'CONDITIONAL_PERSONAL_NONCOMMERCIAL'


def load_policy():
    return json.loads((core.ROOT / 'config/forward_trial_execution_policy.json').read_bytes())


def validated_calendar():
    policy = load_policy()
    path = core.ROOT / policy['calendar']
    if core.sha256(path) != policy['calendar_sha256']:
        raise ValueError('VALIDATED_CALENDAR_HASH_MISMATCH')
    calendar = json.loads(path.read_bytes())
    days = calendar['jp_sessions']
    if days != sorted(set(days)) or not days:
        raise ValueError('VALIDATED_CALENDAR_INVALID')
    for day in days:
        date.fromisoformat(day)
    return calendar


def next_target_session(now=None):
    from engine import fresh_bridge as fresh
    now = fresh._receipt_now() if now is None else now
    if now.tzinfo is None:
        raise ValueError('TIMEZONE_REQUIRED')
    prohibited = load_policy()['permanently_not_run_sessions']
    for day in validated_calendar()['jp_sessions']:
        if day not in prohibited and now < fresh._receipt_time(day + 'T08:45:00+09:00'):
            return day
    raise ValueError('VALIDATED_CALENDAR_EXHAUSTED')


def require_execution_clock(target_session, started=None):
    from engine import fresh_bridge as fresh
    now = fresh._receipt_now()
    if (now.tzinfo is None or target_session != next_target_session(now)
            or now.astimezone(fresh.JST).date().isoformat() != target_session
            or (started is not None and now < started)):
        raise ValueError('ACTUAL_TARGET_DAY_PRE_CUTOFF_REQUIRED')
    return now


def execution_gates(scope, target_session, *, scheduled=False, formal=False):
    policy = load_policy()
    config = json.loads((core.ROOT / 'config/minimal_forward_trial_config.json').read_bytes())
    try:
        valid_target = (policy.get('target_policy') == 'NEXT_VALIDATED_JPX_SESSION'
                        and target_session == next_target_session())
    except (ValueError, OSError, KeyError):
        valid_target = False
    allowed = (scope == SCOPE == policy.get('scope') == config.get('execution_scope')
               and valid_target
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

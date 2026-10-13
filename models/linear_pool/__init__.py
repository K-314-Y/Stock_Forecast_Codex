"""Frozen three-baseline equal pool with deterministic bounded 5pp rounding."""
import json
from fractions import Fraction

from engine.parallel_data import CLASSES, probability_valid
from engine import minimal_trial as core


def equal_pool(baselines):
    if set(baselines) != {'base_rate', 'momentum', 'ma_trend'}:
        raise ValueError('THREE_FROZEN_BASELINES_REQUIRED')
    for p in baselines.values():
        probability_valid(p)
    return {c: float(sum(Fraction(str(baselines[b][c])) for b in baselines) / 3) for c in CLASSES}


def round_primary(raw):
    probability_valid(raw)
    spec = json.loads((core.ROOT / 'config/minimal_forward_trial_config.json').read_text())['forecast']
    step = spec['probability_step_percentage_points']
    lower, upper = spec['probability_bounds']
    order = spec['tie_break_order']
    if step != 5 or spec['probability_sum'] != 100 or order != ['FLAT', 'UP', 'DOWN']:
        raise ValueError('EXISTING_PROBABILITY_SCHEMA_CHANGED')
    values = {c: Fraction(str(raw[c])) * 100 for c in CLASSES}
    result = {c: max(lower, min(upper, int(values[c] // step) * step)) for c in CLASSES}
    while sum(result.values()) != 100:
        add = sum(result.values()) < 100
        eligible = [c for c in order if result[c] < upper] if add else [c for c in order if result[c] > lower]
        if not eligible:
            raise ValueError('PROBABILITY_BOUNDS_INFEASIBLE')
        key = max(eligible, key=lambda c: values[c] - result[c] if add else result[c] - values[c])
        result[key] += step if add else -step
    return result


def predict(baselines):
    raw = equal_pool(baselines)
    return raw, round_primary(raw)


def forward_predict(baselines):
    """Exact equal pool, floor, largest residual. Reject out-of-bound outputs.

    The older bounded projection remains for existing offline probes. Primary
    execution never clips or redistributes around a bound to alter this rule.
    Fractions retain exact residual ties across the three baseline inputs.
    """
    from engine.primary_readiness import require_contract
    require_contract()
    equal_pool(baselines)  # validate input names, units and sum
    order = ('FLAT', 'UP', 'DOWN')
    raw = {c: sum(Fraction(str(p[c])) for p in baselines.values()) / 3 for c in CLASSES}
    points = {c: raw[c] * 100 for c in CLASSES}
    final = {c: int(points[c] // 5) * 5 for c in CLASSES}
    remaining = 100 - sum(final.values())
    if remaining < 0 or remaining % 5 or remaining > 15:
        raise ValueError('POOL_ROUNDING_SUM_INVALID')
    residual_order = sorted(order, key=lambda c: -(points[c] - final[c]))
    for c in residual_order[:remaining // 5]:
        final[c] += 5
    if sum(final.values()) != 100 or any(v < 5 or v > 95 or v % 5 for v in final.values()):
        raise ValueError('PRIMARY_ROUNDED_PROBABILITY_BOUNDS')
    return {c: float(raw[c]) for c in CLASSES}, final


def decision(percent):
    winner = max(('FLAT', 'UP', 'DOWN'), key=lambda c: percent[c])
    peak = percent[winner]
    return winner, 'HIGH' if peak >= 60 else 'MEDIUM' if peak >= 45 else 'LOW'

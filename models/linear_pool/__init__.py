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

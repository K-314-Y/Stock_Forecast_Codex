"""Invented numerical history, never admitted as real training evidence."""
import math
from datetime import date, timedelta
from functools import lru_cache

from engine import minimal_trial as core
from engine import parallel_training as training
from engine.parallel_data import digest


@lru_cache(maxsize=3)
def fixture(count=1060):
    days, current = [], date(2020, 1, 2)
    while len(days) < count + 1:
        if current.weekday() < 5:
            days.append(current.isoformat())
        current += timedelta(days=1)
    history = []
    reference = 'SYNTHETIC_NUMERICAL_FIXTURE'
    source_hashes = {reference: digest(dict(seed=314, count=count))}
    for t_index, ticker in enumerate(core.CORE):
        close = 1000 + 100 * t_index
        for i, day in enumerate(days[:-1]):
            close *= 1 + .008 * math.sin(i * 1.7 + t_index) + .002 * math.cos(i * .11)
            ret = (.02, -.02, 0, .002, -.002, .006, -.006)[(i + t_index) % 7]
            open_ = close / (1 + ret)
            history.append(dict(target_session=day, ticker=ticker, source=reference,
                values=dict(open=open_, close=close, high=max(open_, close) * 1.002,
                            low=min(open_, close) * .998, volume=10000.0),
                availability=dict(status='OBSERVED', source=reference, time_kind='PUBLICATION',
                    available_at=day + 'T17:00:00+09:00', evidence_sha256=source_hashes[reference]),
                original_publication_availability='OBSERVED', feature_integrity=True,
                synthetic_feature_threshold=dict(threshold_value=.004, training_end='2019-12-31',
                    input_available_at='2020-01-01T17:00:00+09:00',
                    replay_as_of='2020-01-01T17:00:00+09:00', purpose='SYNTHETIC_FEATURE_INPUT_ONLY'),
                outcome_integrity=True, outcome_source_admitted=True,
                derived_training_persistence_allowed=True, quality_issues=[],
                source_refs=[reference], synthetic=True))
    target = days[-1]
    features = training.replay_price_features(history, days[:-1], target, synthetic=True)
    dataset = training.build_dataset(features, history, target, target + 'T08:00:00+09:00',
                                     source_hashes, synthetic=True)
    return history, features, dataset

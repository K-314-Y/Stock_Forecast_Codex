"""Entirely invented synthetic Core6 data. No acquired price/news content."""
from datetime import date, timedelta

from engine import minimal_trial as core
from engine.parallel_data import PRICE9, build_common_snapshot, digest

SOURCE_REGISTRY = {'SYNTHETIC_DERIVED': dict(validated=True, derived_persistence_allowed=True)}


def weekdays(count, start=date(2025, 1, 2)):
    result = []
    day = start
    while len(result) < count:
        if day.weekday() < 5:
            result.append(day.isoformat())
        day += timedelta(days=1)
    return result


def snapshot(day, index=0, *, source_registry=None):
    previous = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    meta, thresholds, tables = {}, {}, {}
    p = {'UP': .34, 'FLAT': .33, 'DOWN': .33}
    for tindex, ticker in enumerate(core.CORE):
        state_index = (index + tindex) % 3
        signal = (state_index - 1) * .02
        values = dict(ret_1d=signal, ret_5d=signal * 2, momentum_20d=signal * 3,
                      momentum_60d=signal * 4, realized_vol_20d=.01 + tindex * .001,
                      MA20=100 + index * .1 + tindex, MA60=99 + index * .1 + tindex,
                      MA_state=('BEAR', 'NEUTRAL', 'BULL')[state_index], threshold=.01)
        meta[ticker] = {n: dict(value=values[n], source='SYNTHETIC_DERIVED',
                               source_hash=digest(['synthetic', day, ticker, n]),
                               timestamp=previous + 'T15:30:00+09:00',
                               available_at=previous + 'T16:00:00+09:00', session=previous, currency='JPY')
                        for n in PRICE9}
        thresholds[ticker] = dict(threshold_version='short-oc-p33-v1', T_final=.01,
                                  training_end_date='2024-01-01', status='ENGINE_TEST_ONLY')
        entry = dict(probabilities=p)
        tables[ticker] = dict(base_rate=entry,
                             momentum={s: entry for s in ('POSITIVE', 'NEGATIVE', 'FLAT')},
                             ma_trend={s: entry for s in ('BULL', 'BEAR', 'NEUTRAL')})
    baseline = dict(tables=tables, baseline_version='minimal-baselines-v1',
                    threshold_version='short-oc-p33-v1', training_end_date='2024-01-01')
    return build_common_snapshot(day, previous, day + 'T08:00:00+09:00', meta, thresholds,
                                 baseline, {'synthetic': digest('fixture')},
                                 SOURCE_REGISTRY if source_registry is None else source_registry, synthetic=True)


def samples(count=124):
    days = weekdays(count + 1)
    data = []
    for index, day in enumerate(days[:-1]):
        outcomes = {t: dict(return_oc=(((index + i) % 3) - 1) * .02,
                            available_at=day + 'T18:00:00+09:00', source_hash=digest(['outcome', day, t]))
                    for i, t in enumerate(core.CORE)}
        data.append(dict(snapshot=snapshot(day, index), outcomes=outcomes))
    return data, days[-1]

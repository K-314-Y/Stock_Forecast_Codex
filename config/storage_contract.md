# Storage contract v1
These are initial storage headers, not a running forecasting application.
IDs and versions join forecasts, evidence, baselines and outcomes; old versions remain immutable.
Probability CSV values are percentage points; divide by 100 for Brier/log loss.
Missing CSV values are empty; JSON missing values are null. No missing value becomes zero.
All nested evidence objects require source, observation_time, published_time, available_at, forecast_as_of
and input reference. Publication later than the forecast cutoff disqualifies evidence.
A source response retrieved today alone does not establish historical point-in-time availability.
major_factors_json: at most top three objects with factor_name, direction (Bullish/Bearish/Neutral),
importance (Primary/Secondary/Supporting), evidence, evidence_timestamp, source, expected_lag,
causal_distance, confidence and the four PIT timestamps.
Historical analog entries include event reference and all comparison dimensions in shock_library.json.
Store contradictory evidence, assumptions and falsification conditions separately.
Cohort/expectation entries are always layer SHADOW until documented promotion.
Outcome evaluations append new referenced entries and evaluate cohort dominance, expectation direction,
reaction occurrence and horizon separately from whether price direction was correct.
State snapshots are empty at initialization. Future snapshots use new versioned filenames;
the initial files and existing artifacts must not be overwritten.
Regime values and lifecycle enums follow MASTER_PROMPT.md; unknown states stay empty, not NORMAL.
Source conflicts record both sources/timestamps/discrepancy; never silently average.
Logs are header-only. No forecast, baseline, result, trial or regime evidence is fabricated.
Append-only behavior, permissions, transactionality and cloud locking still require implementation.

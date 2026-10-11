# Analysis configuration v1.2-free-forward-trial

Status: PARTIAL_READY.
Current local mode: FREE_FORWARD_TRIAL_PREP.
System specification: ../MASTER_PROMPT.md v1.2. All sections apply.

## Runtime gates
RUN_MODE = FREE_FORWARD_TRIAL_PREP
PAID_DATA_ENABLED = false
FORMAL_PRODUCTION_ENABLED = false
FORMAL_SCORING_ENABLED = false
FORWARD_TRIAL_ENABLED = false
ROTATION_DRAW_ENABLED = false
AUTOMATIC_LEARNING_ENABLED = false
AUTOMATIC_PROMOTION_ENABLED = false
SCHEDULES_ENABLED = false
BROKER_SESSION_AUTOMATION_ENABLED = false
BROKER_SCREEN_SCRAPING_ENABLED = false

`FORWARD_TRIAL_ENABLED` may become true only after Free Provider Adapter validation succeeds and the user approves the trial start.
Formal Production remains false until all Formal blockers are resolved and explicitly approved.

## Source-of-truth state
Current Trial working source: local Work folder.
Cloud provider / authoritative formal location / synchronization / conflict handling / approval workflow: UNRESOLVED.
Future Formal Production may use Cloud as authoritative source, but no Cloud persistence is assumed today.

Configuration changes:
Proposal -> Human Review -> Version Update.
Preserve previous versions and append-only logs.

## Frozen specification
Core v1: Japan 6, US 6.
Rotation target: Japan 100, US 100; not populated.
Horizons: Short 1, Medium 20, Long 126 actual exchange sessions.

Japan:
- analysis start 08:00 Asia/Tokyo
- freeze by 08:45
- no post-freeze input in the same-day Short forecast
- Tokyo pre-open realtime quote/order-book data is NOT required for the initial free trial

US:
- analysis start regular open minus 60 minutes
- freeze regular open minus 15 minutes
- exchange calendar and DST required

Regional target once enabled:
Core 6 + Rotation 2, each at Short / Medium / Long.
JP Morning: Japan scored target / US provisional.
US Pre-Market: US scored target / Japan provisional.
Provisional output is never mixed into scored trial results.

Primary return:
- Short: forecast-day Open -> same-day Close
- Medium/Long: forecast-day Open -> target-session Close

Reference:
prior-session Close -> target Close.
Store Primary and Reference separately.

## Threshold / probability
Threshold v1:
per ticker/horizon trailing five-year absolute-return 33rd percentile.

Short ATR correction:
activate only when recent 20-session realized volatility differs from five-year rolling-20-session median by >=30%.
ATR basis: ATR(14) / Price.
When active:
50% distribution threshold + 50% ATR-based threshold.

Anomaly:
REVIEW_REQUIRED. Never improvise a replacement threshold silently.

Probability:
UP / FLAT / DOWN = 100%, 5 percentage-point increments, normally no 0% or 100%.

No Formal threshold values or Formal probabilities are approved yet.

## Baselines / evaluation
Three baselines:
1. Base Rate
2. Momentum conditional frequency
3. Simple Trend conditional frequency

Primary score:
3-class Brier Score.

Also:
Brier Skill, Log Loss, Accuracy, Calibration, sample size and uncertainty.

Calibration:
<100 OOS = CALIBRATION_INSUFFICIENT.
>=200 is a higher-confidence range.

No random K-fold for overlapping financial labels.
Use walk-forward / expanding OOS, purging, embargo when needed and block-style uncertainty.

## Production / Shadow
Production rule set and weights: NOT_CONFIGURED.
Investor Cohort + Expectation Gap + Behavioral Reactions: SHADOW_ONLY.
No Shadow result may change Production during FREE_FORWARD_TRIAL_PREP.

Hypothesis lifecycle:
- <30 Candidate
- 30-59 Provisional
- >=60 Validated Candidate only if all forward-validation conditions are met

Promotion requirements remain those in MASTER_PROMPT.md.
Automatic production weight changes remain disabled until Formal Production exists.

## Data policy v1.2
Provider architecture:
Single-Primary + Conditional Fallback.

Do NOT query every available Provider on every Run.

Cross-check Secondary/Fallback only when:
- primary failure
- missing/stale data
- anomaly
- corporate-action suspicion
- timestamp mismatch
- material source conflict
- major event requiring verification

Provider roles and eligibility are defined in provider_registry.json.

Initial free-first policy:
- no paid subscription required
- no automatic paid-provider enrollment
- no logged-in brokerage account used as a data feed
- no SBI or other broker screen scraping / session automation
- Research-only Japanese data may support trial engineering but must not be labeled Formal-grade
- all Cohorts share the same normalized Snapshot instead of performing separate searches

Freshness:
Japan Short uses latest completed Japan session plus information available by 08:45 JST.
US Short uses information available by US freeze time.
Medium/Long do not force unnecessary intraday retrieval.

Token budget:
- market-wide data fetched once per Run
- deduplicate news
- prioritize top 3-5 material news/events per ticker
- Slow State not refreshed daily
- secondary providers conditional only

## Point-in-Time / storage contract
Every evidence item distinguishes:
observation_time / published_time / available_at / forecast_as_of.

Unavailable timestamps stay null.
Missing is never zero.

Data states:
OK / PARTIAL_DATA / STALE_DATA / INSUFFICIENT_DATA.

CSV:
UTF-8, comma separated.
Empty cell means unavailable, not zero.
*_json fields use valid JSON with CSV quoting.
ISO 8601 timestamps with UTC offset.
IDs join logs.
Forecast and trial history is append-only.

Decision Trace stores auditable rationale, not hidden chain-of-thought.

## Current blockers before Forward Trial can start
- Validate a zero-cost Japan price path for trial use and record freshness/history/adjustment limitations.
- Validate US free path continuity and normalization policy.
- Finalize provider adapter + normalized schema.
- Verify symbol/exchange mapping used by selected Primary providers.
- Approve calendar/horizon counting convention.
- Resolve price normalization and split/dividend conventions.
- Define reproducible baseline parameters.
- Define reproducible trial forecast-generation procedure.
- Keep Rotation disabled until provider coverage is adequate.

## Additional blockers before Formal Production
- Formal-grade Japan market data source.
- Validated five-year price history and corporate-action convention.
- Formal PIT-safe snapshot process.
- Production rules/weights.
- immutable freeze / settlement / scoring execution.
- authoritative persistent storage and conflict procedure.
- Rotation 200 construction and validated sampling.

Do not silently resolve blockers. Version and report them.

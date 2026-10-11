# Minimal Token / Retrieval Budget v1

Design-only profile: MINIMAL_FORWARD_TRIAL. Current lifecycle: FREE_FORWARD_TRIAL_PREP; no execution authorization.

## Normal path after separate admission
1. Local cache updates and feature calculations only. Never send 5-year raw rows to an LLM. Missing required historical prices/thresholds/baselines cause a recorded skip, not a fabricated forecast.
2. Fetch each necessary market-wide category once and reference the same immutable snapshot for all six stocks. Cache by provider/category/session/as_of; reuse within run. Never reuse a cache past its approved freshness boundary. Slow State updates only on expiry or material change, not every day.
3. Retrieve bounded news candidates, deduplicate by event identity/source/time, retain at most 3 events per stock and 18 unique-or-shared event references per run. A shared event body is stored once. Zero news is valid. Provider result limits must apply before loading large candidate corpora; no local truncation after bulk downloading.
4. News envelope: event_type, event_time, short factual summary (maximum 400 characters), source, materiality, event_id and PIT metadata. No article bodies in LLM input. Missing availability or unverifiable summaries are flagged/excluded rather than filled in.
5. At most one forecast LLM request containing one common snapshot and six compact stock feature objects. No browsing/tool calls within it, no independent agents or searches per ticker. One invalid batch is rejected; no automatic LLM retry or post-freeze replacement.
6. Local scoring and baseline comparison require 0 LLM calls and 0 news calls. Weekly selected review: at most 1 extra LLM batch, <=3 records, 100-300 output tokens per record target, never a measured claim.

## Hard limits
| Item | Limit |
|---|---|
| Historical raw rows into LLM | 0 |
| News/events retained | <=3 per stock, <=18 per run |
| Historical analog or shock search | 0 |
| Cohort independent searches | 0 |
| Rotation | 0 |
| Medium/Long recalculation | 0 |
| Normal provider cross-check | 0 |
| Normal full-news detail calls | 0 |
| Material-event detail exception | <=2 calls/run, explicit approval and reason; still <=3 retained events/stock |
| Forecast LLM calls | <=1/run; no automatic retry |
| Scoring LLM calls | 0 |
| Weekly review LLM calls | <=1/week, only selected candidates and separate authorization |

Cross-check permitted only for missing, stale, anomaly, data_conflict, corporate_action_suspicion. Log trigger, source and request. No all-provider cross-check. Unknown cost, nonadmitted provider or missing rights fails closed, even for an exception.
News detail exceptions are for earnings/guidance/M&A/regulatory or similarly material events, not unrestricted cross-provider research. If exceptions exceed limits, defer rather than silently expand.
Provider total call cap and precise token ceilings: UNRESOLVED until the approved request plan/tokenizer/context capacity are chosen. This is a Trial-start blocker, not an unlimited allowance. Provider API batch capacity must not be guessed.

## Feature and evidence contract
Candidate stock inputs: previous completed OHLCV; 1D/5D returns; 20D/60D momentum; 20D realized volatility; ATR(14)/Price; volume ratio; MA20/MA60 state; frozen stock-specific threshold. Optional sector relative strength only if already available cheaply.
Common candidates: prior Japan market move, overnight S&P500/Nasdaq, USDJPY move, major rates, materially relevant commodities. No six-fold common-data acquisition.
Feature formulas, annualization, MA/ATR conventions, volume-ratio window, exact overnight cutoff/benchmark and missing-data policy must be versioned before start. They are not silently chosen here. Preserve existing threshold methodology, including ATR activation; do not compute it from quote snapshots.
Baseline construction must use five-year historical Open-to-Close class outcomes with input states available before each historical outcome, not the same day's completed return as its predictor. Freeze training cutoff, feature-state bins, threshold vintage, minimum sample size, smoothing and empty-bin behavior before Trial. No post-outcome baseline re-estimation for an already frozen forecast. A baseline missing required history blocks a matched comparison; never silently invent a uniform distribution.
Evaluate all three baselines on the same frozen/scorable forecast IDs. Brier Skill uses matched mean Brier, not mean per-row ratios; zero reference mean gives null with reason. Record exclusions/skips and their causes. Log-loss nulls (including zero empirical actual-class probability) need per-method reason/coverage in the evaluation manifest; never silently clip or compare mismatched subsets.
Each snapshot retains source IDs, PIT metadata, feature/config versions and content hash outside the compact forecast. top_factors evidence IDs must resolve to this manifest. Full trace/Cohort/analogs are not required in this profile. No private chain-of-thought.

## Per-run accounting
Record run_id, phase (acquisition/forecast/scoring/review), llm_calls, estimated_input_tokens, estimated_output_tokens, provider_calls, news_items_loaded, cross_checks.
Unavailable values are null plus status UNKNOWN, never zero. Known skipped operations may truthfully be 0. Each token estimate records method and ESTIMATED; actual provider-reported token usage has separate actual_input_tokens/actual_output_tokens and MEASURED status. Never equate character length with measured tokens.
Count external calls including failed/retried requests; record cache hits separately. If underlying provider fan-out is hidden, exposed tool-call counts are MEASURED but internal provider_calls UNKNOWN. Keep exception and review costs in totals. Do not infer zero marginal cost from successful access.
Reduction compared to Full Mode: QUALITATIVE ONLY. No measured full/minimal paired runs exist.

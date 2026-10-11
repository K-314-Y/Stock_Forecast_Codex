# Minimal Forward Trial Mode & Failure Review Design

Phase: 2C-0. Date: 2026-09-15. Design Status: **PARTIAL**.
Minimal operating rules and schemas are defined and synthetic checks passed. Reproducible feature/baseline parameters, data admission and runtime implementation remain blockers. Trial readiness is NOT established.

## A-B. Mode and scope
Current RUN_MODE remains FREE_FORWARD_TRIAL_PREP. MINIMAL_FORWARD_TRIAL is a separate, inactive execution profile, not a fourth lifecycle stage and not Formal Production. A future human-approved FREE_FORWARD_TRIAL may select it after the price contract and start gates are satisfied.
Japan only: Advantest 6857, Toyota Motor 7203, Mitsubishi UFJ 8306, INPEX 1605, KDDI 9433, Skylark 3197. Core membership unchanged; codes refer to existing audited Tokyo identity records, not a new provider selection.
Short only: forecast-day Open -> same-day Close, UP/FLAT/DOWN. Intended first window 20-30 JPX sessions. Exact planned count must be preregistered before start. No outcome-driven early stopping or automatic extension.

## C. Inputs
Enabled in design as candidates, NOT live inputs: local previous-completed OHLCV, 1D/5D return, 20D/60D momentum, 20D realized volatility, ATR14/Price, volume ratio, MA20/MA60 state and stock threshold. Cheap already-available sector relative strength is optional.
One shared market snapshot: previous Japan move, overnight S&P500/Nasdaq, USDJPY, relevant rates and materially relevant commodities. No duplicated per-stock retrieval.
Formula conventions and PIT provenance must be fixed before activation. Required gaps cause a skip; optional fields remain null with reason. No five-year raw history enters the LLM.

## D. Heavy features
DISABLED_FOR_MINIMAL_TRIAL: full Cohorts, historical shock/analog retrieval, detailed long-form Decision Trace, broad web research, independent ticker research, Rotation100+100, Medium/Long daily work. Existing full specifications and historical files remain intact for separately approved future Shadow experiments.

## E. Compact forecast and freeze
Schema includes every requested field: forecast_id/version, ticker, session_date, freeze_time, UP/FLAT/DOWN, most_likely_class, confidence, threshold and <=3 top factors (label/direction/evidence timestamp).
Additional compact references: config/model/prompt version, created_at, supersedes, snapshot ID/hash, evidence IDs and frozen three baseline probability vectors/versions/sample counts. The referenced snapshot holds full PIT metadata; no hidden chain-of-thought is stored.
AI probabilities are 5..95 in 5-point steps, sum100. Fixed tie order FLAT then UP then DOWN. Confidence is only a probability bucket: HIGH>=60, MEDIUM>=45, otherwise LOW; not a calibration claim.
One batch request for six stocks; no search tools in the forecast call. Invalid output rejects the batch without silent repair or automatic LLM retry. Atomic freeze-manifest selection, create-only writes, idempotence and append-only corrections must be implemented before start. Post-freeze corrections cannot replace evaluated predictions.

## F. Outcome scoring
Future local code only; no LLM/news search for scoring. Actual Open/Close must meet price_source_contract.md. Bigdata company_overview remains prohibited for scoring.
Return = Close/Open - 1. UP when r>threshold, DOWN when r<-threshold, otherwise FLAT. Equal boundaries are FLAT as a minimal design convention; threshold magnitude methodology remains unchanged.
Brier = sum over three classes of squared probability error, with AI percentages divided by100; range0..2, not divided by3. Accuracy uses fixed argmax. Optional natural-log loss; no silent epsilon clipping. Baseline scores use the frozen empirical vectors without forcing 5-point rounding.
Missing outcome yields PENDING_DATA and null scores, not zero/error or an invented actual. Invalid data may be EXCLUDED_DATA_QUALITY with reason; preserve records and report coverage. Score corrections are new versions. No production scoring engine was implemented or executed in this phase.

## G. Selective Failure Review
Local Correct/Incorrect first. Ordinary misses retain UNKNOWN unless evidence supports a cause. Correct records have no failure type. Pending outcomes are not called misses.
Detailed candidates only: >=60% miss; sum-Brier>=1; same supported non-UNKNOWN type >=3 times/last5 sessions; a baseline correct and AI wrong with Brier gap>=0.25; or logged data-quality suspicion.
At most one separately authorized weekly batch, <=3 records, target100-300 tokens each. No daily long review. Diagnosis is explicitly HYPOTHESIS_NOT_FACT, evidence-linked, availability checked. Post-freeze information is NEW_INFORMATION only with supporting evidence, not automatically a logic failure.
Requested YES/NO new-information field permits null when unknown to avoid falsely asserting NO. Review thresholds are design settings, not evidence of predictive efficacy or changes to Brier/Promotion.
Weekly local summary includes types, UNKNOWN and review coverage, confidence misses, Brier contribution with denominators, ticker, frozen market condition and repeated patterns. At most3 improvements become hypotheses, never automatic changes. Full promotion/weight criteria remain intact.

## H. Token and retrieval guards
Raw history to LLM0; news<=3/stock and <=18/run, deduplicated factual event envelopes; analog0; independent Cohort searches0; normal cross-check0; Rotation/Medium/Long0. Shared market retrieval once per required category.
Forecast<=1 LLM request/run, scoring0, selective review<=1 batch/week. Material-event detail exceptions require approval, <=2 calls/run, and do not relax retained-news caps. Cross-check causes limited to missing/stale/anomaly/conflict/action suspicion.
Track llm_calls, estimated_input_tokens, estimated_output_tokens, provider_calls, news_items_loaded and cross_checks with measured/estimated/unknown status. Hidden upstream fan-out remains UNKNOWN. No cost or exact token count is inferred.
Provider request cap and precise token ceiling remain unresolved pending a validated endpoint plan/model tokenizer; unknown is not an unlimited allowance. No automatic use of unverified-cost data.

## I. Baselines
Preserved: five-year class Base Rate; Momentum conditional frequency; MA/Trend conditional frequency. Same Open-to-Close target, training cutoff, threshold conventions and scorable forecast set. Freeze baseline distributions before results.
Momentum state bins, MA conditional rules, minimum sample count, smoothing/empty-bin policy and detailed threshold/ATR implementation remain UNRESOLVED. Do not invent definitions or infer them from example fixtures. No random K-fold, post-outcome tuning or substitution of a more favorable comparator.
Report all three and strongest matched comparator; BSS denominator zero => null. Pilot20-30 sessions does not establish independent120-180 samples: cross-ticker same-day dependence, per-ticker small samples and calibration limits must be reported. Losing to simple baselines is a valid outcome, not a reason to add complexity.

## J-K. Blockers
Preserved data blockers: Bigdata price/volume semantics; Raw/Adjusted/action definitions; Japan scoring Open/Close source; yfinance terms/eligibility; US continuity/adjustment; maturity convention; Bigdata cost UNVERIFIED; MUFG historical OHLC conflict.
US and Medium/Long work is outside this profile, but their original global blockers are not silently waived. Existing analysis_config.md remains authoritative; any narrowed start-gate waiver would need explicit approval.
Additional start blockers: role-specific price contract; feature and baseline parameter versions; approved precise duration; immutable writer/manifest and local scorer; batch/PIT/token guards tested end-to-end; bounded zero-cost provider request plan; explicit human start approval.
No data blocker was solved merely by creating this mode.

## Validation
- JSON configuration, three Draft2020-12 schemas and three synthetic example records parsed/validated.
- 19 offline tests PASS: schema validity; inactive gates/scope/budgets; bad sums/steps/extremes; factor cap; late input/output rejection; baseline sum; extra private-reasoning field rejection; ties; pending outcomes; correct unnormalized Brier; quote-as-close rejection; diagnosis/hindsight/trigger guards; class boundaries.
- All listed gates false: Rotation, Medium, Long, Cohort retrieval, Shock retrieval, Automatic Learning, Automatic Promotion, Schedules, paid auto-use and broker automation. Forecast/Trial/scoring/review execution additionally false.
- Immutability, selective triggers, baseline comparison and budget rules present.
- Examples are SYNTHETIC_TEST, dated2000, artificial100/100 prices; no actual forecast, baseline calculation from market data or Trial score was generated.
- The test helper validates cross-field constraints JSON Schema cannot express. It does not implement atomic storage, trusted source admission, verified evidence availability, real tokenizer budgets or execution scheduling. Those remain untested runtime requirements.
- Validation command: uv run --offline --no-project --with jsonschema==4.26.0 python -B analysis/provider_test_results/test_minimal_design.py. Cached dependencies only; no market calls.

## L. Files
Created:
- config/minimal_forward_trial_config.json
- config/failure_review_policy.md
- config/token_budget.md
- config/minimal_forecast_schema.json
- config/failure_record_schema.json
- config/trial_score_schema.json
- analysis/provider_test_results/minimal_design_examples_2026-09-15.json
- analysis/provider_test_results/test_minimal_design.py
- analysis/minimal_forward_trial_design_2026-09-15.md
Changed: MASTER_PROMPT.md, additive minimal-profile reference only.
Runtime/provider registry/price contract, existing logs/state/historical_shocks and past validation history unchanged. No schedule, paid contract, account action, prediction, learning or promotion.

## M. Reduction
**QUALITATIVE ONLY**. No comparable full/minimal operational token or retrieval measurements exist. Expected structural savings: one shared snapshot and batched forecast; no raw histories, heavy retrieval or long daily review; local scoring and selective weekly review. No numerical percentage or cost saving is asserted.

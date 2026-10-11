# Minimal Failure Review Policy v1

Date: 2026-09-15. Design only; all execution remains disabled in FREE_FORWARD_TRIAL_PREP.
This profile supplements, not replaces, the full MASTER_PROMPT. Failure labels describe evidence-backed hypotheses, not causal facts established by a wrong forecast.

## Local classification first
Only after a valid score: compare frozen most_likely_class with actual_class. Correct or Incorrect is deterministic. Pending/excluded outcomes receive no correctness or causal diagnosis; retain their data-quality reason in the score record.
Correct records have failure_type=null. An ordinary Incorrect record has failure_type=UNKNOWN and detail=null unless existing evidence supports a more specific label. DIRECTION is not automatically assigned merely because argmax was wrong. Missing diagnosis is not RESIDUAL_UNCERTAINTY.
Allowed failure types: FEATURE_OMISSION, WEIGHTING, DIRECTION, TIMING_LAG, REGIME, EXPECTATION_GAP, NEW_INFORMATION, DATA_QUALITY, THRESHOLD, RESIDUAL_UNCERTAINTY, UNKNOWN.

## Selective review candidates (not execution authority)
Fixed design thresholds for this profile; change only by a new version before a subsequent trial, never to reinterpret completed scores:
A HIGH_CONFIDENCE_MISS: Incorrect and max frozen class probability >=60%.
B LARGE_BRIER: sum-scale Brier >=1.0 (range 0..2).
C RECURRING_TYPE: >=3 evidence-supported records of the same non-UNKNOWN failure type within the last 5 observed JPX sessions. Undiagnosed misses do not become an asserted repeated cause.
D BASELINE_WIN_AI_MISS: AI Incorrect, at least one frozen baseline argmax Correct, and AI Brier minus that baseline Brier >=0.25. This selects review, not a new favorable baseline for headline results.
E DATA_QUALITY_SUSPECTED: logged source/validator anomaly. May create a review candidate before scoring, but no fabricated actual or score record; keep it as a quality issue until scoreability is resolved.
No trigger means no detailed LLM review. Trigger means CANDIDATE, not automatic execution.

## Review budget and queue
No daily narrative reviews. At most one separately authorized weekly batched LLM review, using at most 3 candidate records, target 100-300 output tokens each. Rank deterministically: data quality, high-confidence miss, larger Brier, then forecast_id. Others remain DEFERRED_BUDGET; do not lose their counts.
Use only stored frozen snapshot, frozen baselines, outcome provenance and already saved event evidence. No routine news re-search, broad research or historical shock retrieval. When evidence is missing, keep UNKNOWN rather than searching until a story fits.
Fields: forecast_id, prediction, actual, failure_type, available_before_freeze YES/NO/UNKNOWN, primary_error, optional secondary_error, new_information_after_freeze YES/NO (null if unknown), candidate_improvement, confidence_in_failure_diagnosis. Include evidence IDs and diagnosis_status=HYPOTHESIS_NOT_FACT. Character caps in Schema are not token measurements.

## Hindsight guard
Check actual evidence availability, not only the event's nominal date. Keep source observation, publication, availability and receipt times in the referenced snapshot. Unknown availability is UNKNOWN, not YES. Proven local receipt before freeze can bound accessibility without inventing provider first-publication time.
If documented post-freeze information supports the diagnosis, mark NEW_INFORMATION, available_before_freeze=NO, new_information_after_freeze=YES. Do not call that an omitted pre-freeze feature. Timing alone does not prove market causation; retain hypothesis status.
FEATURE_OMISSION/WEIGHTING/EXPECTATION_GAP require evidence that the relevant information was available before freeze. If that cannot be established, keep UNKNOWN. Verified pre-freeze availability still does not by itself establish causation.
Do not modify frozen factors, threshold, probability or baseline based on the outcome. Later diagnosis/correction is append-only with references to the original IDs.
For a future writer: new immutable forecast IDs/versions, create-only persistence and an atomically frozen run manifest must select the evaluated version before freeze. Never select a more successful corrected version afterward. Repeated execution must be idempotent. A late correction is annotation only; a data-invalid original may be excluded with an explicit reason, not replaced retrospectively. These storage guarantees are requirements, not implemented by a JSON Schema.

## Weekly aggregation and improvements
Local summary only: counts by type, UNKNOWN/undiagnosed coverage, high-confidence misses, Brier sums AND means with denominators, ticker, frozen market-condition tag (UNKNOWN allowed), repeated patterns, number deferred. Report all scored forecasts, not just selected failures. Group same-session cross-ticker observations for uncertainty; 6 tickers are not 6 independent market days.
At most 3 recurring patterns become improvement hypotheses. No minimum count alone proves validity. Example: repeated EXPECTATION_GAP -> Shadow feature candidate -> separately approved forward comparison -> Brier/calibration/log-loss/strongest-baseline comparison -> promotion candidate only under unchanged full-system guardrails.
20-30 sessions is an initial observation window, not proof of superiority or promotion eligibility. Freeze intended duration before start; no performance-based early stopping. If AI loses to baselines, report it and retain the simpler comparator; do not add complexity to disguise failure.
No weekly schedule created. Automatic learning and promotion remain disabled.

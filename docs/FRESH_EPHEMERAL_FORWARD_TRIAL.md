# Task 10B operating decision

2026-10-13 execution is authorized only for
`LOCAL_PERSONAL_NONCOMMERCIAL_FORWARD_TRIAL`. Terms state is
`CONDITIONAL_PERSONAL_NONCOMMERCIAL`; earlier `UNRESOLVED` states remain history.
This records the user's operating decision, not unrestricted provider licensing.

JPX is limited to personal, noncommercial market research, with necessary,
low-frequency acquisition. No redistribution or publication of Raw/PDF data.
Existing validated Historical inputs through 2026-10-08 must be reused without
refetching or modifying Raw, Parsed or Manifest files.

BIGDATA/FMP calls are explicit, ticker-bound and limited to one per Core6 ticker
per execution. Fresh prices and response bodies exist only in process memory.
The existing nine feature formulas, frozen Thresholds and Baselines are unchanged.
Persist only derived features and allowlisted audit metadata, never the raw close,
company_overview, response JSON, provider timestamp value, or provider content cache.
Audit metadata contains identifiers, actual retrieval time, timestamp presence,
validation status and the derived feature snapshot hash. Derived returns and MAs
are explicitly allowed by this operating decision; they can reveal prices when
combined with known history. Non-reconstruction applies to the audit metadata,
not a claim of mathematical irreversibility of the permitted features.

Legacy Fresh records remain retained audit evidence, marked
`NOT_ELIGIBLE_FOR_FORECAST_REUSE` and `DO_NOT_USE_AS_FORWARD_TRIAL_INPUT`.
Retention of original provider content is `DELETE_RECOMMENDED`; this task does
not delete it. Historical values through 2026-10-08 are independently fixed from
the hash-anchored Task 10A validated history; its 2026-10-09 row and prepared
features are excluded. No stored Fresh receipt is admitted by the execution path.
Legacy receipt readers serve historical diagnostics and synthetic probe regressions.

Commercial use, third-party distribution, Forecast API, SaaS, raw market data
redistribution, unlimited schedules and Formal Production remain prohibited.
Formal, Schedule, scoring, learning, promotion and Candidate Discovery stay disabled.
Policy permission does not prove live provider availability: Task 10C must retrieve
Core6 anew on the actual target day before 08:45 JST, validate identity, currency,
session, timestamp and PIT, and consume the in-process inputs in the same execution.
No replay of saved derived snapshots as a new execution input is supported.

The 2026-09-18 `RAW_SOURCE_HASH_MISMATCH` fixture remains unchanged and is
`MEDIUM_NON_BLOCKING_LEGACY_FIXTURE`; the validated current historical chain
is independent. Task 10B does not generate Forecasts, Freeze, IDs, probabilities,
relative strength predictions, Brier scores or Candidate Discovery.

## Task 10C-4 target renewal

2026-10-13 is permanently NOT_RUN; the original review remains immutable history.
The explicit Task 10C-4 decision authorizes only the next validated JPX session,
selected by the existing calendar and actual target-day pre-08:45 clock.
Scope, terms and raw persistence remain unchanged. See
[Primary contract](PRIMARY_FORWARD_TRIAL.md) for the real runner, observed
prospective ledger, partial coverage and crash recovery. Historical ML blockers
do not gate Primary. Missing required new historical continuity still blocks
execution before Fresh acquisition.


## Permanent NOT_RUN evidence

The tracked execution policy preserves the 10/13 decision and forbids deadline
backfill. New missed sessions live in `state/forecast_not_run_sessions/`, outside
Git. `execution_policy.not_run_sessions()` is the authoritative static/runtime
union used by target selection, Forecast and prospective PIT validation. Runtime
records are create-once, hash-verified, aware-time evidence with target/reason/source;
conflicting duplicates, corruption, missing published records and clock rollback
fail closed. Deadline bookkeeping cannot dirty tracked execution configuration.

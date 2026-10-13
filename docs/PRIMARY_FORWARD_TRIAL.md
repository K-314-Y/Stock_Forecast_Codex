# Task 10C-4: Primary Forward Trial and prospective PIT

2026-10-13 is permanently `NOT_RUN`. No Forecast ID, retrospective Forecast or
Freeze can be made for that session. `execution_policy.next_target_session()`
selects the next session whose cutoff has not passed from the existing
hash-validated JPX calendar. Execution requires the actual target day before
08:45 JST (the existing Task 10B window), an aware clock, and the same-process
input capability. A future calendar date alone is never execution permission.

The scope remains `LOCAL_PERSONAL_NONCOMMERCIAL_FORWARD_TRIAL`, terms remain
`CONDITIONAL_PERSONAL_NONCOMMERCIAL`. Formal, schedules and scoring stay disabled.
The anchored Task 10B review supplies the scope and provider/persistence contract;
its old target is retained as review history. Task 10C-4 explicitly replaces that
single target with the next validated session rule. No live calls or real
Forecast/Freeze/Outcome/scoring take place during implementation.

## Primary rule

`equal-linear-pool-v1` / version `equal-linear-pool-v1` / `PRIMARY` uses only the
frozen `minimal-baselines-v1` Base Rate, Momentum and MA Trend distributions.
Each class is their exact arithmetic mean, with weights 1/3. State selection
uses the existing momentum and MA rules. Thresholds remain `short-oc-p33-v1`.
Candidate/spec/quality/bridge identities are pinned by SHA256; no reestimation.

`models.linear_pool.forward_predict` uses rational arithmetic, floors each
class to 5pp, and distributes the remaining 5pp units by largest raw residual.
Exact ties use FLAT, UP, DOWN. The sum must be 100 and every result must satisfy
existing [5,95] bounds. If this exact allocation falls outside bounds the run
fails; it never clips or applies a discretionary correction. The older bounded
projection exists only for existing offline research/probe regressions.
The predicted class is the maximum final probability with the same tie order.
Confidence is HIGH >=60, MEDIUM >=45, LOW otherwise; the config string is checked
before acquisition. Relative strength is `missing_optional` / `RULE_NOT_FROZEN`.

## Input and Primary readiness

`prepare_execution_inputs(...) → _ExecutionInputs → consume_execution_inputs(...)
→ engine.forward_trial_forecast.run_forward(...)` is the sole real input path.
Stored Task 10A evidence, legacy Fresh response/receipts, persisted features,
dictionaries and manually constructed tokens do not enter the runner.
A weak-key capability registry separately binds snapshot/audit hashes, source
hashes, actual start time, bundle identity and ledger path. Consumption removes
the capability; resetting object attributes cannot replay it. Modified inputs,
wrong scope/session, changed sources and expired/rolled-back clocks are rejected.
The trusted controller supplies ticker-bound connector callables; this module
neither discovers providers nor loads saved responses into those callables.

The existing authenticated history through 2026-10-08 remains unchanged. Later
history can extend it only through already registered, authenticated JPX
Historical Close derivatives strictly before the new previous session. No saved
Fresh row is recycled into history. Missing extension stops before provider calls.
In particular the current next session needs a validated 2026-10-09 extension;
this task does not acquire/register it or mark the current state READY.

The Core6 mapping remains mandatory. An explicitly absent callable (`None`)
is an absent required Fresh input and receives `SKIPPED_MISSING_INPUT` with its
reason; valid tickers may proceed under the existing absent-input-only Partial
Ticker rule. Missing keys, malformed numbers, security challenges, wrong identity,
provenance conflicts and provider exceptions abort. No exception is converted to
missing, no data are filled, and all-missing creates no Forecast. The one-call
per available ticker, six-call maximum, no automatic retry rules remain intact.

`primary_readiness.assess_primary()` and `shadow_readiness()` expose separate
`PRIMARY_FORWARD_READINESS` and `SHADOW_ML_TRAINING_READINESS` gates. Primary has
no historical ML dependency. Its contract/history/clock still must pass.
Logistic and LightGBM remain SHADOW / TRAINING_BLOCKED, with no real training or
model Freeze. Historical classification stays ADMITTED=0 / UNKNOWN=7434.
60 Forward sessions is the minimum promotion review milestone and 120 the
stronger evaluation target; neither is a Primary execution or row-count gate.

## Freeze and runtime recovery

The runner loads frozen baselines, computes Primary, creates records and commits
one create-only bundle under `data/forecasts/primary_forward_trial_v1/BUNDLE_ID`.
Bundle IDs use `primary_YYYYMMDD_UUID32`; Forecast IDs use the existing parallel
prefix, date, ticker, model ID and that UUID suffix. Identity is independent of
mutable paths. Under an exclusive store lock it checks all committed IDs and
session collisions. A second final bundle for the same target is rejected.

The pending directory contains all forecasts, permitted input bindings, frozen
baseline probabilities, prospective rows, runtime after-images and receipt.
Every artifact is canonically serialized, fsynced and sealed with SHA256. Before
rename the complete artifact allowlist, seals, probabilities, times, IDs, grain,
input hashes and scope are validated again. The directory is fsynced, the clock
is checked, then it is atomically renamed and its parent fsynced. Partial and
orphan `.pending-*` directories are never admitted as final bundles.

Forecast records have strict fields for ID/bundle/target/ticker, model role and
version, generated/freeze timestamps, scope/terms, feature session/hash, common
input hash, threshold/baseline versions, raw probabilities, final percentages,
normalized probabilities, predicted class/confidence, relative strength and
code commit. `percent_probabilities` maps UP/FLAT/DOWN to schema percentage points.
The receipt binds Forecast IDs, complete ticker statuses, all identities/times,
code commit, scope/terms and each artifact hash. `bundle_sha256` is the canonical
hash of the receipt excluding its own hash, transitively sealing the bundle.

Runtime changes happen only after the durable final Freeze. Each of the four
existing JSON projections receives generated/frozen flags, cumulative count,
latest IDs, target, actual timestamps and the disabled Formal/Schedule/scoring
flags. The receipt seals the exact before hashes and after-images. Replacements
are atomic and fsynced, followed by an atomic `primary_projection.json` checkpoint.
This is a roll-forward transaction: four filesystem paths cannot be swapped in
one rename. If interrupted after Freeze, `recover_runtime()` validates the final
bundle and restores remaining after-images without generating another Forecast
or incrementing the count twice. A before/after mixture is recoverable; an
unrelated modification is a conflict and is never silently overwritten.
`refresh_runtime_snapshot.refresh()` recovers first. Other runtime readers must
recover first after an interrupted Primary run. No retry or Resume policy is
changed. A new Forecast cannot proceed while runtime recovery is pending.
Before the final rename, every failure leaves runtime flags/count/files unchanged.

## Prospective ledger

The default preparation destination is
`data/training/prospective_parallel_v1/pre_session/TARGET_SESSION/`.
Preparation serializes at the session grain and rejects duplicates before any
Fresh callback. `prospective.json` is sealed at successful preparation, even if
Forecast subsequently fails; its reserved bundle ID alone does not prove Freeze.
The Forecast bundle copies and binds these exact rows.

Rows are `target_session × ticker`, with feature session/set version, the original
nine features, actual retrieval and derivation-availability timestamps, OBSERVED,
source identity, ephemeral response SHA256 and historical/config source hashes,
PIT validation, common snapshot hash, bundle identity and code commit.
`feature_available_at` is sampled from the actual clock after derivation. No
caller timestamp, filesystem mtime, session-close guess or historical backdating
is used. No Fresh price/response/provider timestamp/body/cache is persisted.
Provider/section identifiers are audit labels, not copied provider content.
Derived returns and MAs remain permitted under Task 10B, including their existing
reconstruction caveat.

`prospective_pit.record_outcomes()` is infrastructure for a later task only. After
session completion, a trusted admitted source producer returns ticker-bound
Open/Close, source identity/hash and corporate-action status. The module records
OPEN_TO_CLOSE return, target class under the pre-session frozen threshold,
actual outcome retrieval/completion availability, OBSERVED, and threshold version
at `outcomes/TARGET_SESSION/outcomes.json`. No availability timestamp parameter
is accepted. No Outcome is acquired in Task 10C-4.
`join_rows()` rejects duplicated or mismatched keys, incomplete coverage, future
availability and unknown actions. It returns one-to-one rows with separate
source/training review still pending; it never fits an estimator or automatically
upgrades source admission. No prospective row is required to repair historical
UNKNOWN rows. Research eligibility and promotion remain separate future reviews.

## Offline verification

Use `.venv/bin/python`, `PYTHONPATH=.:tests`, and unittest modules
`test_forward_trial_forecast test_prospective_pit test_fresh_ephemeral
 test_execution_policy` plus the existing parallel/historical/Fresh/Minimal/
Partial/execution-policy/dry-run regressions. Run full local offline discovery.
Public archive verification uses only synthetic suites (local PDFs/history/runtime
and market-derived candidate bundles are intentionally absent).
`python -m scripts.check_prospective_pit PRE_SESSION_JSON --output LOCAL_EVIDENCE`
is a stdlib-only independent Data Analytics grain/time/allowlist/hash audit.
Evidence and acquired/local runtime artifacts remain ignored by Git.

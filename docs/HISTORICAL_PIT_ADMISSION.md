# Task 10C-3: historical PIT admission

`historical-pit-admission-v1` is limited to the requested local, personal,
noncommercial offline research. It permits derived training artifacts and audit
metadata under ignored directories. This is an internal operating decision,
not resolution of Yahoo/JPX terms. Terms remain UNRESOLVED. Task 10B Fresh
persistence, provider registry, live scope, Forecast and scoring are unchanged.
No raw redistribution, new acquisition, repair or feature redesign is authorized.

Admission evaluates source identity, historical availability, revision safety,
feature and Outcome integrity, Outcome source admission, corporate actions, PIT,
and research persistence separately. Any UNKNOWN gives UNKNOWN_PIT, including
rows with another known failure. Otherwise any FAIL gives REJECTED_PIT. All must
pass (corporate actions may be NOT_APPLICABLE) for ADMITTED_PIT. Known failures
remain explicit in gate reasons, including MUFG's existing quarantine.

A saved download timestamp proves that content was observed by that time only.
Neither a session label nor filesystem/Git/cache creation time proves historical
publication. Local import timestamps remain separately identified. No current
retrieval is backdated. Official exchange closing times and quoted delays alone
are not guarantees of historical bar availability or absence of corrections.
SOURCE_SCHEDULE_DERIVED remains supported only with the existing verified,
versioned, source-bound upper-bound contract. No real source rule is activated.

Uniform positive multiplication preserves returns, sample volatility and MA
ordering; absolute MA20/MA60 scale. Threshold replay includes a full-prefix
volatility regime test and ATR divided by the latest Close, so it requires a
common factor across its entire support, not just the latest 61 rows. Changes
inside the window or unequal OHLC factors need separate evidence. Mathematics
alone does not prove which revisions affected the actual saved vintage.

Same-session Close/Open is invariant to a common positive factor. This permits
source-specific Outcome admission only when identity, field/basis semantics,
corporate-action integrity and revision evidence are established. A numeric
match or clean OHLC does not establish all these conditions. Official JPX is
preferred only when admitted. Any material overlap discrepancy quarantines that
Outcome without averaging or silently falling back. Parser/structure validation
and current close-bridge compatibility do not upgrade historical Outcome flags.

Local reports contain source-field/session provenance, feature proof conditions
and counterexamples, action evidence, exact join/admission results, fold and
readiness results. Historical feature and Outcome sources remain separate;
replacing a label by JPX never replaces the feature-window price series.
Old Raw, Parsed, Manifest, frozen Forecast and Outcome records stay immutable.
All nine original price features and the threshold algorithm stay unchanged.

Real model validation/Freeze requires sufficient PIT dates, at least two
expanding folds (60 train / 20 validation dates), and no BLOCKER/HIGH. Otherwise
metrics are unavailable, model hashes are null, and Shadow ML remains blocked.
Primary next-session readiness is a separate read-only assessment; this task
never activates or generates a Forecast, including after the cutoff.

## Reproduce the local audit

Use the existing model environment and an actual analysis timestamp. The target
is only an exclusion boundary; this command does not execute a Forecast.

```bash
PYTHONPATH=. .venv/bin/python -m engine.parallel_training_local \
  --target-session YYYY-MM-DD --training-as-of ACTUAL_ISO_TIMESTAMP
PYTHONPATH=. .venv/bin/python -m scripts.check_historical_admission \
  data/training/parallel_models_v1/DATASET_SHA \
  --output analysis/historical_pit_admission_v1/independent_data_quality.json
PYTHONPATH=.:tests .venv/bin/python -m unittest test_historical_admission test_parallel_training
```

A BLOCKED dataset exits with code 2 and still writes its immutable exclusion
artifact. Files include `dataset.json`, `source_provenance.csv`,
`feature_revision_proofs.json` and `source_admission_audit.json`. The CSV contains
metadata at artifact/field/ticker/session grain; no price column. Its checksum
is bound into the dataset hash. The JSON supplements have sidecar seals.

The independent checker imports no engine code and re-reads the original local
sources. Its current-inventory assertions intentionally detect any change in
source admission/basis flags. Re-audit those assertions when a future source
contract changes; never delete a failing check just to admit more rows.

An Outcome source candidate count includes JPX/Yahoo overlaps. Count deduplicated
Outcome keys separately; never add overlapping provider rows to the training
population. Local auto-adjust reconstruction is a transformation check, not an
independent provider or historical availability verification.

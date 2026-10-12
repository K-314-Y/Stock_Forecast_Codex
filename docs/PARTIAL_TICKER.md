# Partial ticker v1

Core6 preparation uses `engine.partial_ticker.prepare_session` for source rows or
`prepare_snapshot` for already prepared features. Both return a session result
with an explicit coverage ledger. These are offline, in-memory entry points:
they do not acquire data, execute a Forecast or scoring, train/refit a baseline,
activate a trial, or write Raw/runtime/Frozen data.

The existing strict APIs keep their defaults. `minimal_trial.compact_snapshot`
has an explicit `allow_partial` option used by the new preparation entry points.
Use the new entry points to retain skip records; a bare partial snapshot alone
does not carry the session ledger. Existing Forecast writers still require their
existing gates. No Forecast logic or parser/admission semantics changed.

## Admission order

1. Validate the session, freeze time and common inputs.
2. Inspect all supplied ticker inputs, including incomplete tickers, for integrity
   conflicts before generating any features or selecting baselines.
3. Only absent or `None` required inputs allow `SKIPPED_MISSING_INPUT`.
4. Pass admitted tickers to the existing feature formulas and frozen baseline
   selector, then build a snapshot containing only those tickers.

No broad exception is converted to missing. Invalid numeric values (including
zero/nonfinite prices), source/hash/provenance conflicts, ticker/session mismatch,
OHLC inconsistencies, quarantine issues and an explicit security challenge raise
an error. A missing ticker never suppresses a conflict in another ticker or in
another supplied field of the same ticker. Acquisition STOP / Retry / Resume v2
and challenge detection remain unchanged; their exceptions must propagate.

There is no interpolation, fill-forward, previous-close replacement, zero fill,
synthetic OHLC, peer replacement, or LLM call. A missing historical session is a
missing input, not a repaired series. Close-only feature histories are supported
because the existing live feature formulas require closes; an OHLC input contract
must explicitly require `('open', 'high', 'low', 'close')`.

## Inputs and result

`prepare_session(target_session, expected_previous_session, freeze_time,
stock_inputs, expected_sessions, *, required_price_fields=('close',), ...)`
expects each available ticker to provide:

- `rows`: ordered observed rows with `session_date` and required price fields.
- `threshold`: the existing frozen threshold, never reestimated.
- `baseline_tables`: frozen `base_rate`, all three momentum states, and all three
  MA states, using the existing selector and probability distributions.
- `baseline_metadata`: `training_end_date`, `threshold_version`, `baseline_version`.
- `provenance`: existing provenance; file evidence is represented by
  `evidence: [{path, sha256, ...}]`. Supplied files are hash-checked before and after
  calculations. Missing files/hashes are required-input gaps; mismatches are errors.

`expected_sessions` is the complete required ordered history window ending at
`expected_previous_session`; extra, duplicated or unordered observed sessions are
errors. Fewer than 61 observations or missing expected sessions cause ticker SKIP.
Rows must already have passed their upstream source admission/verification. These
entry points check consistency and completeness; they do not grant a new source
permission. A `MISSING_OHLC`/`MISSING_VOLUME` issue is accepted as missing only when
the corresponding required field is actually absent; any additional integrity
issue still raises an error.

`prepare_snapshot` accepts the existing `features`, `threshold`, `baselines`,
`provenance` stock structure. It inspects supplied fields for conflicts even if
another required feature is missing. With all six tickers valid, its `snapshot`
is byte-for-byte and hash compatible with the legacy snapshot.

The result includes `expected_tickers`, `processed_tickers`, `available_tickers`,
`skipped_tickers`, `ticker_results`, `stages`, and `snapshot`. Status is `COMPLETE`,
`PARTIAL`, or `NO_PROCESSABLE_TICKERS`. All-missing produces an empty stock map,
six skip records, and no feature/baseline outputs.

Example skip record (logical input reference, not fabricated source evidence):

```json
{
  "ticker": "6857",
  "session": "2026-09-18",
  "status": "SKIPPED_MISSING_INPUT",
  "missing_inputs": ["features.MA20"],
  "reason": "REQUIRED_INPUT_ABSENT",
  "evidence_refs": [{"input_ref": "stock_inputs.6857"}]
}
```

Observed evidence references are also copied into the record when supplied.
Entirely absent tickers use `missing_inputs: ["ticker"]`; row-field gaps use
paths such as `rows[99].close`. The ledger is the concise log; no verbose output
is emitted. Persist the complete result in an authorized caller's result store
when persistence is needed.

## Downstream coverage

`inputs_for_stage(result, stage)` returns only admitted stocks, with expected,
available, processed and skipped ticker lists. Supported stages are
`feature_generation`, `baseline`, `scoring`, `forecast_input`. Preparation reports
the accepted feature/baseline sets as `PREPARED`. Scoring and Forecast input report
`NOT_RUN`, with `processed_tickers: []` and the explicit available set. An absent
ticker never appears in the returned stock map. Future consumers must keep this
coverage envelope alongside their inputs and outputs.

The new path does not replace the older validation-only fresh bridge path, which
has a different `SKIP_FRESH_FEATURE` contract. Its existing regression behavior is
unchanged. Do not use that old skip decision as Partial ticker v1 admission.

## Offline validation

Use the existing Python environment:

```bash
PY=/home/yugo314/.venvs/stock-forecast-codex-py312/bin/python
$PY -m unittest tests.test_partial_ticker -v
$PY -m unittest discover -s tests -p 'test_*.py'
```

The new focused tests use only synthetic fixtures and temporary files and run from
a public checkout. Production history, runtime snapshots and acquired PDFs are
not included in public-tree validation. No live JPX requests are permitted.

The public-tree runnable subset (run inside a Git archive, with no ignored data)
includes the focused tests, synthetic STOP/Retry/Resume/acceptance/JST suites,
historical listing/request tests and raw-source resolution tests:

```bash
PYTHONPATH=.:tests $PY -m unittest \
  test_partial_ticker test_jpx_policy_v2 test_jpx_policy_v2_regression \
  test_jpx_acceptance_regression test_jpx_crash_safety \
  test_jpx_challenge_ledger_regression test_jpx_jst_observation_day \
  test_jpx_historical_direct.ListingTests test_jpx_historical_direct.RequestTests \
  test_minimal_trial_engine.RawSourceResolutionTests
```

Tests requiring acquired local PDFs, cached prices, runtime snapshots or candidate
bundles remain part of the full local offline suite, not this public subset.

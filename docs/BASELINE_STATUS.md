# Initial Repository Baseline

Recorded when the existing Linux project was first connected to `K-314-Y/Stock_Forecast_Codex`.

## Offline test baseline
- Command: `python3.14 -m unittest discover -s tests -p 'test_*.py'`
- Tests run: 233
- Passed: 232
- Error: 1

Known pre-existing error:
- `tests.test_jpx_historical_direct.AcquisitionTests.test_real_parser_validation_direct_metadata_and_create_only_evidence`
- Error: `JPX_OUTCOME_AVAILABILITY_INVALID`
- Existing cause: the test uses a fixed adapter/mock time that precedes the parser retrieval time. This baseline commit does not weaken production time validation or repair that test.

This file records the starting condition only. It is not a claim that the full production pipeline is enabled or validated.

## Public boundary audit: 2026-10-11

The historical test result above was not rerun during this audit. No functional
code or configuration values were changed. The public candidate list excludes
acquired data, market-derived config bundles and two tests with inline real market
values; see `PUBLIC_BASELINE_AUDIT.md`. Other published test source can still refer
to local-only data, runtime files or test helpers, so full test discovery is not
currently a self-contained public-clone check. Offline CI and synthetic fixture
separation are future work, outside this audit.

# Repository Architecture

## Tracked
- `engine/` forecast, scoring, bridge and validation logic
- `providers/` provider adapters and parsers
- `tests/` selected unit/fixture test source, excluding inline acquired market values
- `config/` policies, schemas and research definitions, excluding runtime and market-derived bundles
- `docs/` operating and architecture documentation
- `research/` shadow-only research implementations and evaluation code
- `historical_shocks/` compact research definitions, not raw market data
- explicitly allowed design/template files under `analysis/`; see `PUBLIC_BASELINE_FILES.txt`

## Local only
- `data/` raw/parsed/runtime market data
- `logs/` execution logs
- `state/` mutable runtime state
- `backups/` local backups
- `exports/` generated exports
- acquired samples, provider responses and generated validation reports under `analysis/`
- all PDF files
- credentials, tokens and private keys

## Public boundary audit

`PUBLIC_BASELINE_AUDIT.md` records the publication decisions and held files.
`PUBLIC_BASELINE_FILES.txt` is the reviewed candidate list for this baseline.
REVIEW_REQUIRED files remain on disk and are ignored until their data provenance or
redistribution conditions are resolved. Ignored data references in source code do
not grant permission to include the referenced files. Some existing tests need
local data and cannot run from the public tree alone.

## Planned development order
1. Repository protection and GitHub structure
2. STOP / retry / resume policy redesign
3. Historical acquisition stabilization
4. Forecast timestamp and evidence compatibility
5. Partial-ticker forecasts and optional-input tolerance
6. Lightweight logging
7. CI for offline tests
8. Stable daily pipeline
9. Company relation graph
10. Event spillover shadow evaluation
11. Model comparisons only after the pipeline is stable

New research features remain shadow-only until forward evidence supports promotion.

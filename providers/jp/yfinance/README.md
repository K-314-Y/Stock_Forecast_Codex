# Research Prototype (Phase 2B-1A)

Not a forecast engine. RESEARCH_ONLY; no registry routing or execution gate changes.
Dependencies: yfinance==1.7.0, pandas via yfinance, jsonschema==4.26.0.
Calendar validation additionally uses exchange-calendars==4.13.2.

The initial bounded six history requests are in the analysis probe. This adapter
loads that exact local cache and generates schema-compatible research records.
No extra network request is needed for auto_adjust=true: yfinance's own local
transformation is applied to the same cached false response, with separate outputs.
The false response is library output, not original HTTP JSON or native unadjusted prices.

One MUFG bar violates OHLC bounds. It is preserved and flagged DATA_CONFLICT.
All other rows remain PARTIAL_DATA with historical PIT and adjustment admission
unverified; every record has primary_price_direction_eligible=false.
SPLIT_NORMALIZED_OHLC is a provider-basis candidate, not a verified reconstruction.
No native RAW_OHLC series is fabricated. TOTAL_RETURN_REFERENCE never enters
primary price-direction evaluation.

read_cache / normalize / validate are offline. summary_only omits full history.
fetch_update defaults to no network and is limited to explicit, bounded daily
updates for the six Tokyo symbols. Existing snapshots are never overwritten.
Updates are not merged automatically; corporate actions can revise older bases.
Live incremental retrieval has not been exercised in this phase. Function-level
request counts do not include internal yfinance transport calls.

Tests:
uv run --no-project --with yfinance==1.7.0 --with jsonschema==4.26.0 --with exchange-calendars==4.13.2 python -B analysis/provider_test_results/test_yfinance_adapter.py

No installation into a global Python environment, schedule, account login, paid
contract or automatic trial activation is part of this prototype.

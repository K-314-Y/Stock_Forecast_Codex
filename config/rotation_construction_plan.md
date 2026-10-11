# Rotation construction plan
Status: NOT_CONSTRUCTED, 0/100 Japan, 0/100 US. No daily draws.
Rotation: DISABLED. The following is a future plan, not authorization to construct or draw now.
1. Acquire dated broad-market constituent snapshots: TOPIX family Japan, Russell 3000 US;
   use a documented US Total Market fallback only if Russell coverage is unavailable.
2. Resolve issuer, listing date, exchange, currency, corporate actions and data availability as of selection time.
3. Freeze the reference population for the bottom liquidity quartile, sector taxonomy, market-cap and volatility bins,
   concentration limits and Core overlap policy before choosing names; these are unresolved design choices.
4. Acquire 20 completed exchange sessions of turnover. Apply Japan >=JPY500m and US >=USD5m average,
   plus exclusion of the market's bottom liquidity quartile. Missing turnover is ineligible, not zero.
5. Require >=3 listing years. Exceptional New Entrants need multiple documented criteria:
   top-tier cap, very high liquidity, industry/market influence, index inclusion/prospect, sufficient company data.
   At most 5 per market; never fill the exception quota artificially. Mark History Limited = Yes,
   use only available issuer/sector/market/current-volatility history for a separately versioned provisional
   threshold and score these separately. Standard listings with <5 years also need an explicit history policy.
6. Build exactly 100 eligible names per market distributed across Sector x Market Cap x Volatility,
   including large, medium and small companies. Record source timestamps and every inclusion reason.
   A shortfall remains incomplete; do not fabricate constituents.
7. Validate liquidity, history, count, exception limits and coverage. Publish immutable Universe version
   through Proposal -> Human Review -> Version Update only after blockers are resolved.
   Formal authoritative storage is UNRESOLVED; Cloud remains a future candidate, not an existing authority.
8. Daily weighted random sampling: 2 per market, 10-session cooldown, weights 60/25/15,
   attribute history mix 70/30 for 20/60 sessions; correction 1.30/1.00/0.75.
   Freeze exact normalization, draw algorithm, seed and probability semantics before activation.
   Log eligible pool, blocked count, weights, probabilities, attributes, regime, time, previous draw and counts.
   No detailed same-day company news/price inspection before selection.
9. Monthly normal replacements <=5 per market. Delisting, extended halt, unavailable data, major reorganization
   or clear violations may force changes outside the cap, with dated reasons and versions.
   Never replace for recent gains, favorable news or past forecasting success. Do not reclassify old results.

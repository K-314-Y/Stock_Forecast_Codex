# Version history

## Historical listing Direct HTTPS adapter (2026-10-08)
- Scope: explicit single-session Historical Catch-up for Oct 5/6/7 only; no live JPX requests were performed during implementation/testing.
- Added providers/jp/jpx_manual/historical_direct.py and offline fixture/mock tests. TLS/official-host checks precede every request, including redirects; listing structure/date/stock-column href are required; unknown responses and access restrictions stop without retry.
- Parser jpx-local-0.2.2 only admits the new acquisition metadata OFFICIAL_LISTING_DIRECT_HTTPS with no browser actor. Existing extraction, OHLC/Core6/Schema/hash checks are reused; existing Raw/Parsed records stay unchanged.
- Scoped policy exception in JPX acquisition configs and startup guidance. Existing Browser/Fresh/Forecast/Outcome policies, runtime/gates, schedules, thresholds and baselines are unchanged.
- Create-only acquisition bundles remain pending explicit Manifest and existing Historical Close Bridge registration; neither is automatically mutated. Terms remain UNRESOLVED.

2026-09-12 | init-v1 | Local initialization from MASTER_PROMPT v1.1 | No prior configurations changed.
Core membership follows specification; identifiers unverified. Rotation remains draft and empty.
Production rules unconfigured; Cohort shadow only; formal runs disabled. No forecasts or experimental trials performed.
Master SHA256: 5D398FCFB06246AD38CF55E704BFCA93329F9E7950DD538128EFACF8DDB3306A
All future changes require new version records; never overwrite formal history.

## v1.2-migration (recorded 2026-09-13)
- Version: v1.2-migration
- Date: 2026-09-13 (audit/record date; original file placement time not independently established)
- Changed Files: MASTER_PROMPT.md, config/analysis_config.md, config/data_sources.md, config/provider_registry.json, MIGRATION_README.md (user-supplied migration package).
- Reason: Zero additional-cost, Single-Primary + Conditional Fallback preparation policy.
- Previous State: v1.1 local initialization, unresolved data and formal storage.
- New State: FREE_FORWARD_TRIAL_PREP; trial/formal execution disabled; migration audit PARTIAL due to configuration inconsistencies.
- Human Approval status: User supplied migration files and requested audit; no trial or Production approval.

## v1.2-cleanup-2026-09-13
- Version: v1.2-cleanup-2026-09-13; runtime/provider schema 1.2.1.
- Date: 2026-09-13
- Changed Files: MASTER_PROMPT.md, config/runtime_status.json, config/provider_registry.json, config/rotation_construction_plan.md, config/data_sources.md, config/version_history.md; new analysis/v1_2_config_cleanup_report_2026-09-13.md.
- Reason: Phase 2B-0 correction of audited authority, provider references/types, PREP wording and version history only.
- Previous State: runtime source_of_truth=CLOUD; mixed default_query/eligibility; undefined OFFICIAL_IR_WEB; ambiguous PREP/Trial scope.
- New State: LOCAL_WORK trial preparation authority; formal authority UNRESOLVED; Cloud future candidate retained; normalized enums and resolved IDs; all execution gates disabled.
- Human Approval status: Explicit user authorization for Phase 2B-0 configuration cleanup only. Next-phase execution, trial start and Production remain unapproved.
- Previous files: backups/v1_2_pre_cleanup_2026-09-13/ preserves all six edited files byte-for-byte before cleanup.
- No methodological, Core, history, Production rule or weight changes; no provider admission to FORMAL_ELIGIBLE or TRIAL_ELIGIBLE.

## Phase 2C-5B (recorded 2026-09-30)
- Version: phase-2c-5b-direct-boundary-overlap-2026-09-30
- Date: 2026-09-30
- Changed Files: data/outcome_input/manual_jpx/stq_20260911.pdf (new local copy), data/outcome_input/manual_jpx/retention_manifest.json, data/outcome_parsed/jpx/2026-09-11_core6_direct_boundary_overlap.json, analysis/provider_test_results/direct_boundary_overlap_2026-09-30.json, analysis/direct_boundary_overlap_2026-09-30.md, analysis/provider_test_results/direct_boundary_overlap_validation_2026-09-30.py, config/official_gap_policy.json, config/minimal_trial_engine.json, tests/test_official_gap.py, config/version_history.md.
- Reason: Phase 2C-5B direct same-session OHLC overlap for the Historical Cache boundary and scoped Minimal Trial operational compatibility validation.
- Previous State: Historical Cache native semantics UNKNOWN; 2026-09-11 JPX overlap absent; stale official_gap_policy Bigdata technical gate=false; scoped adjustment compatibility and one-session dry-run gate not recorded.
- New State: 2026-09-11 Core6 OHLC 24/24 EXACT_MATCH; native semantics remain UNKNOWN; FEATURE_SERIES_OPERATIONALLY_COMPATIBLE_FOR_MINIMAL_TRIAL=true; ADJUSTMENT_BASIS_COMPATIBLE_FOR_MINIMAL_TRIAL=true; RECALCULATION_REQUIRED=false; READY_FOR_ONE_SESSION_LIVE_DRY_RUN=YES; official_gap_policy and minimal_trial_engine technical gates synchronized. Terms remain UNRESOLVED and FREE_FORWARD_TRIAL / DAILY_ACQUISITION / SCHEDULES remain disabled.
- Human Approval status: Explicit user authorization for this validation only; no approval to start a trial, schedule, or Production operation.
- Preservation: Historical CSVs, existing JPX Gap, Threshold/Baseline Candidates, Quarantine and previous reports were read-only; no correction, interpolation, provider overwrite, forecast or scoring.

## Runtime compact layer (recorded 2026-10-01)
- Version: runtime-compact-v1
- Added: 00_START_HERE.md and state/runtime/{current_state.json,recent_sessions.json,run_checklist.json,archive/}.
- Reason: reduce normal-run context/token usage and avoid reconstructing current state from many historical reports.
- Normal startup reads only the compact runtime layer; config/analysis/MASTER_PROMPT are conditional references.
- recent_sessions uses a rolling 31-calendar-day ledger; old audit files are not deleted.
- Current pending work remains 2026-09-25 JPX gap repair. Phase 2C-6A has NOT been executed.
- No Forecast, Provider call, scoring, Trial start, Schedule start, threshold/baseline change, or historical data mutation was performed by this architecture change.
- Compact runtime was further reduced: normal startup now reads only state/runtime/runtime_snapshot.json; engine/refresh_runtime_snapshot.py regenerates it locally and rolls session entries older than 31 days into state/runtime/archive.\n

## Core6 Freshness acquisition fallback (recorded 2026-10-05)
- Version: core6-freshness-rdc-fallback-2026-10-05
- Changed Files: providers/jp/jpx_manual/parser.py, 00_START_HERE.md, state/runtime/run_checklist.json, engine/refresh_runtime_snapshot.py, state/runtime/runtime_snapshot.json, config/minimal_forward_trial_config.json, config/jpx_computer_use_policy.json.
- Reason: Existing Core6 Freshness Work could observe but did not invoke the already-configured missing-only JPX acquisition path; visible RDC Chrome provenance was also rejected by the parser actor enum.
- New State: Normal Work startup can see an explicit missing-only acquisition fallback; valid or metadata-pending raw files are reused without redownload; parser v0.2.1 accepts GOOGLE_CHROME_VISIBLE_RDC as browser provenance.
- Safety: Forecast gates, 08:45 Freeze rules, parser OHLC extraction logic, thresholds, baselines, scoring, paid-provider policy, CAPTCHA/403 bypass prohibitions, and no-redownload rules are unchanged.
- Human Approval status: User explicitly allowed file changes in this planning chat.
## Late Research Diagnostic path (2026-10-05)
- Added `engine/late_research.py` as a separate post-08:45 research-only path.
- Actual run time is preserved; the admissible evidence cutoff remains 08:45 JST.
- Late research records are explicitly ineligible for formal forecast, Forward Trial, scoring and `prediction_log`.
- Existing `engine/minimal_trial.py` 08:45 validation was not relaxed.
- Added `tests/test_late_research.py`.
- Oct 1/2 RDC Chrome JPX raw PDFs were revalidated with parser jpx-local-0.2.1 and registered as normalized inputs; no redownload and no forecast/scoring occurred.

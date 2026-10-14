# Task 10C-7A: Forward repair and the next live window

The production worktree integrates `dev/efficiency-hardening-v1` by fast-forward
from `f60fbba965ee858ca6e7a3afefbdb4e09655a4f0` to
`7d5fa1b4b272bc9ea379c6f68348e582078b25bb`. The earlier hardening document describes
its isolated development task; this explicitly authorized integration is separate.

## Permanent exclusions

Both 2026-10-13 and 2026-10-14 were migrated with the existing
`scripts.forecast_not_run_ledger.record` function before restoring the temporary
tracked policy change. The existing `forecast-permanent-not-run-v1` schema denotes
PERMANENTLY_NOT_RUN and preserves the session, reason, actual aware recorded time,
source policy hash, record hash, inventory and independent initialization witness.
An empty static list was used for readback verification, proving both exclusions
survive independently in `state/forecast_not_run_sessions/`.

Execution consumers continue to use the static/runtime union. Neither date can
receive a backdated or catch-up Forecast. No historical Forecast is rewritten.

## Calendar-selected monitor and final check

For a newly authorized monitor, use the existing retained official discovery:

```sh
.venv/bin/python scripts/install_jpx_publication_monitor.py --arm \
  --discovery-reference <repository-relative-path-to-sealed-discovery.json>
```

The installer selects the next eligible Forecast target and its immediately
preceding session from the SHA-validated JPX calendar and runtime exclusions.
Systemd commands pin that pair so restarts cannot silently advance the target.
The original date constants support the already completed legacy monitor only;
they do not select new windows.

Context creation replays the retained official listing program, monthly list and
monthly JSON through the existing discovery functions. Body hashes, sealed
response metadata, official redirect chains, month identity and acquisition STOP
gate must pass. A discovery from the same month may anchor a new session's
metadata check; a different month requires its own observed discovery. No monthly
URL or PDF name is guessed. Newly pinned context files are create-only and bind
current policy, calendar, STOP/release and discovery evidence.

The hourly cadence remains one hour after each completed check. A separate
08:35 JST timer permits one final monthly JSON GET despite the hourly guard.
A durable reservation and shared flock prohibit a second final request,
including concurrent invocations. Missing/corrupt reservations and interrupted
requests fail closed. The final check must start between 08:35 and 08:40 JST.
Security STOPs and redirects prevent further requests. Only response hashes and
publication metadata are persisted; response bodies and PDFs are absent.

The 08:45 deadline service performs bookkeeping only, with no HTTP. A late final
timer also cannot request HTTP after cutoff. If usable history is not independently
completed in time, the target becomes a runtime permanent exclusion. A present
Forecast receipt conservatively blocks exclusion and requires review rather than
invalidating an existing Freeze. Final/hourly timers stop on terminal publication
or security state; the deadline timer remains armed until bookkeeping completes.
All eight units are bounded oneshots; no daemon loop is introduced.

Publication detection reports PUBLICATION_DETECTED in the existing session-named
state and hands off to explicit official acquisition/registration and preflight.
These timers do not execute Fresh, Forecast, Freeze, PIT, scoring or training.

## Current history blockers

The validated calendar selected target 2026-10-15 and previous session 2026-10-14
at the actual 2026-10-14 afternoon inspection. Official history ends at 2026-10-09.

The original Forward history snapshot pins retention-manifest hash
`92ee4acb76a099c2616e107c416cf4e19a1f2be7a64a4a6fced93cc5b1a9c491`.
The retained before-manifest has those exact bytes. The current manifest differs
only by an added 2026-10-09 entry; every old entry is unchanged. Nevertheless the
existing loader correctly rejects the changed reference with
`INPUT_CHANGED_BEFORE_FREEZE`. Its immutable snapshot registration mechanism has
no refresh/rebinding operation. Do not edit its HMAC, historical receipt or hashes,
restore the old manifest, or weaken the unchanged-source guard.

The existing historical acquisition scope also pins an older Forecast target and
excludes that target's latest preceding session. Thus it rejects 2026-10-13;
2026-10-14 is additionally outside the configured allowlist and, at inspection,
is not prior to the JPX observation date. No unauthorized acquisition is attempted.
Restoring usable history requires a reviewed formal snapshot-refresh/registration
mechanism and an explicitly validated acquisition scope for the needed completed
sessions. These are retained as concrete blockers, not bypassed by manual JSON or
third-party evidence.

## Model and execution invariants

Threshold `short-oc-p33-v1` remains frozen through 2026-09-11. Baselines
`minimal-baselines-v1` and Primary `equal-linear-pool-v1` remain unchanged:
each baseline has weight 1/3, rounding uses 5pp residual allocation, ties prefer
FLAT > UP > DOWN, and confidence boundaries are 60/45. Relative Strength remains
`missing_optional` / `RULE_NOT_FROZEN`. Both ML models stay SHADOW / TRAINING_BLOCKED.

Fresh and one-use runner execution require actual target-day time before 08:45,
validated history, unchanged contracts, an empty target registry and successful
offline preflight. Existing create-only atomic Freeze and source/cutoff checks
remain intact. No Fresh, Forecast, Freeze, prospective PIT or scoring occurred
in the repair task's ineligible afternoon window.

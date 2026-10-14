# Bounded JPX publication monitor

For calendar-selected new windows and the 08:35 final check, see
[Task 10C-7A deployment](FORWARD_REPAIR_10C_7A.md). The original one-night
authorization and its immutable completed evidence are described below.

Task 10C-7B3 adds an explicitly authorized, offline identity renewal:

```sh
.venv/bin/python scripts/install_jpx_publication_monitor.py --arm --rebind-target 2026-10-15
```

This installer does not start a checking service. It accepts only a pre-final-window
identity STOP caused by the acquisition config or component code changing. The
previous monitor code/config must match its committed ancestor. Calendar, target,
deadline, execution policy, official discovery, STOP and release evidence must
remain unchanged; discovery is replayed through the existing offline validators.
Security challenges, redirects, interrupted checks, completed history and final or
deadline reservations cannot be reset. Exact old envelopes are retained create-only
under `bindings/<new identity>/` before current projections are replaced. The STOP
is cleared only after the new context passes verification. Partial renewal fails
closed with the STOP still present.

The renewed context pins current acquisition component fingerprints (including
the binding verifier) and monitor code, rather than global HEAD. Unrelated commits
and runtime writes do not invalidate it. This is a monitor-only authorization:
the historical acquisition release is neither changed nor renewed. Its separate
gate can remain closed until its own explicitly authorized review. Generated unit
definitions must match the installed units, then daemon-reload and timer enable
arm hourly polling, 08:35 final check and 08:45 bookkeeping without immediate HTTP.

This task explicitly authorizes only metadata polling for 2026-10-13, until
2026-10-14 08:45 JST. The monitor never imports execution modules and never
fetches a PDF. Fresh, Forecast, Freeze, scoring, historical registration,
release generation and prospective PIT writes are absent from this path.

The local `state/jpx_publication_monitor/2026-10-13/context.json` checkpoint was
created offline while the existing historical STOP gate passed at the approved
baseline HEAD. It binds the official HTML → monthly list → October JSON discovery
evidence and 46 existing source/policy/STOP/release files by SHA256. Each run
checks those identities, the validated calendar, branch, armed target/deadline and
monitor code hashes before requesting the **exact observed monthly JSON URL**. No endpoint or
PDF name is guessed. Only a response hash and lightweight publication metadata
are retained; response bodies are not saved.

This independently authorized monitor checkpoint is not a historical release.
Original STOP and generations 1–4 remain immutable. The acquisition gate now
binds relevant component fingerprints; unrelated commits require no renewal.
Task 10C-6c must still pass the historical gate before acquisition. The monitor
cannot authorize acquisition or create a release.
Any change to the pinned context stops further metadata requests as well.
The monitor's own persistent security STOP also requires human review before
Task 10C-6c; deleting that file is not an approved release.

`scripts/install_jpx_publication_monitor.py --arm` installs six small user
systemd unit definitions and performs the first check immediately. The poll
timer waits one hour plus five seconds (startup margin) and repeats one hour
after each completed service run.
A lock plus a durable pre-request reservation prevents concurrent/replayed
requests. The one-hour clock guard also applies to manual invocations. An
interrupted reservation, malformed JSON, identity conflict, unexpected redirect,
401/403/429, CAPTCHA or challenge becomes a persistent STOP with no auto-resume.
The oneshot service has no retry; its OnFailure unit disables the poll timer.

Publication detection atomically saves evidence, disables the hourly timer,
surfaces Task 10C-6c readiness in the journal/state and sends a local desktop
notification if available. It never performs Task 10C-6c or Task 10C-7.
Not-yet-available is a normal zero-exit result. Terminal state is never rearmed.

A separate **non-network deadline guard** remains scheduled at exactly 08:45 JST
(one-second timer precision, subject to OS scheduling). With Persistent=true it
also finalizes after a machine restart/sleep. Every polling invocation checks the
deadline first, so waking late cannot cause an HTTP request or catch-up Forecast.
At the cutoff the guard records 2026-10-14 in the ignored, hash-verified runtime
NOT_RUN ledger unless independently completed usable history is evidenced in
time. The tracked policy stays read-only and retains the static backfill ban and
10/13 exclusion. All consumers use the static/runtime union. A durable inventory
detects missing published records; readers fail closed during incomplete
publication and a writer can restore the exact reserved record.

Deadline bookkeeping reserves at most three attempts durably, including the
initial attempt, with at least one hour between reservations across restarts.
The explicit driver has two sleeps at most; deadline service uses `Restart=no`.
Final failure latches STOP and scheduled execution disables both timers. An
OnFailure service independently attempts STOP publication and timer disarm if
the deadline service crashes or times out. Retry paths perform no HTTP, even
after clock rollback. Completed results in the retry envelope recover a crash
before latest/STOP publication. Filesystem errors remain fail closed.

Task 10C-6c may supply `task10c6c_completion.json` in the monitor state directory
using the existing canonical `{index, sha256}` envelope. Required index fields:
`task="10C-6c"`, `status="PASS"`, `target_session="2026-10-14"`,
`previous_session="2026-10-13"`, `official_status="OFFICIAL_VALIDATED"`,
`history_ready=true`, an aware actual `completed_at_jst` strictly before cutoff,
and nonempty repository-relative `source_hashes` binding the official registration,
refreshed validated history and preflight evidence. The monitor only verifies
the independently produced receipt and hashes; it never creates a PASS receipt.
Missing, malformed, late or changed evidence cannot claim completed history.
Bindings must include the policy's active historical snapshot,
`state/runtime/recent_sessions.json` (10/13 OFFICIAL_VALIDATED), the retention
manifest and preflight evidence. Publication metadata alone cannot pass.

Manual command (from the repository; shares the same lock/cadence/terminal guard):

```sh
.venv/bin/python scripts/monitor_jpx_publication.py \
  --session 2026-10-13 --deadline 2026-10-14T08:45:00+09:00 --once
```

Latest result: `state/jpx_publication_monitor/2026-10-13/latest.json`.
Integrity/security STOP: `STOP.json` in the same directory. These and all timer
runtime state remain outside Git. Inspect user units with
`systemctl --user list-timers --all` and the service with
`journalctl --user -u jpx-publication-20261013.service`.

Focused offline suite:

```sh
PYTHONPATH=.:tests .venv/bin/python -m unittest test_jpx_publication_monitor
```

Task 10C-OPT-1 changes only the improvement branch. Existing live timers and state
are untouched. `armed.code_commit` remains audit lineage; repository HEAD equality
is no longer an execution condition. Monitor/installer/NOT_RUN module hashes,
protected references, policy/calendar/discovery identities and target/deadline
remain gates. Existing two-module armed receipts remain readable, but cannot
authorize changed code lacking the new ledger hash. Deploy/rearm decisions belong
to a separate task after the current monitor ends.

# Bounded JPX publication monitor

This task explicitly authorizes only metadata polling for 2026-10-13, until
2026-10-14 08:45 JST. The monitor never imports execution modules and never
fetches a PDF. Fresh, Forecast, Freeze, scoring, historical registration,
release generation and prospective PIT writes are absent from this path.

The local `state/jpx_publication_monitor/2026-10-13/context.json` checkpoint was
created offline while the existing historical STOP gate passed at the approved
baseline HEAD. It binds the official HTML → monthly list → October JSON discovery
evidence and 46 existing source/policy/STOP/release files by SHA256. Each run
checks those identities, the validated calendar, branch, armed commit and monitor
code before requesting the **exact observed monthly JSON URL**. No endpoint or
PDF name is guessed. Only a response hash and lightweight publication metadata
are retained; response bodies are not saved.

This independently authorized monitor checkpoint is not a historical release.
Original STOP, generation3 and generation4 remain immutable. A new code commit
does not renew generation4's HEAD binding: Task 10C-6c must explicitly revalidate
the existing historical gate before acquisition. The monitor cannot do that.
Any change to the pinned context stops further metadata requests as well.
The monitor's own persistent security STOP also requires human review before
Task 10C-6c; deleting that file is not an approved release.

`scripts/install_jpx_publication_monitor.py --arm` installs five small user
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
At the cutoff the guard records expiry and appends only 2026-10-14 to the existing
`permanently_not_run_sessions` execution-policy list unless independently
completed usable history is evidenced in time. It disables both timers. This
durable policy edit may leave a local Git modification; scheduled runs never
commit or push. Policy persistence failures retry bookkeeping hourly without HTTP.

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

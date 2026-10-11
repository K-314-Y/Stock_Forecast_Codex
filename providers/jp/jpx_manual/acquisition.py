"""Pure offline acquisition planner. Does not open browsers or download files.

Caller must durably persist each ledger transition before any browser action.
An uncompleted attempt fails closed on restart. No scheduler is installed.
"""
from datetime import date, datetime, timedelta, timezone
import hashlib
from pathlib import Path

JST = timezone(timedelta(hours=9))
RESTRICTIONS = {'CAPTCHA', 'HTTP_403', 'ACCESS_DENIED', 'RATE_LIMIT',
                'SECURITY_CHALLENGE', 'ACCESS_RESTRICTION'}
HARD_FAILURES = {'PAGE_CHANGED', 'PDF_LINK_UNKNOWN', 'SESSION_MISMATCH',
                 'PDF_CORRUPT', 'PARSER_FAILURE', 'CORE6_MISSING',
                 'DUPLICATE_TICKER', 'SCHEMA_FAIL'}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def calendar_sessions(calendar, day):
    if calendar.get('validated') is not True or calendar.get('timezone') != 'Asia/Tokyo':
        raise ValueError('REVIEW_REQUIRED: unverified calendar')
    sessions = calendar['sessions']
    if sessions != sorted(set(sessions)):
        raise ValueError('REVIEW_REQUIRED: invalid calendar ordering')
    for session in sessions:
        date.fromisoformat(session)
    if not calendar['coverage_start'] <= day <= calendar['coverage_end']:
        raise ValueError('REVIEW_REQUIRED: outside calendar coverage')
    return sessions


def publication_due(session, calendar):
    sessions = calendar_sessions(calendar, session)
    if session not in sessions:
        raise ValueError('REVIEW_REQUIRED: target is not a JPX session')
    following = [s for s in sessions if s > session]
    if not following:
        raise ValueError('REVIEW_REQUIRED: next business date outside calendar')
    return following[0]


def publication_observation(session, calendar, checked_at, available_at_check):
    """A page observation is not an exact publication timestamp or backfill order."""
    if checked_at.tzinfo is None or not any(available_at_check is value for value in (True, False, None)):
        raise ValueError('REVIEW_REQUIRED: invalid publication observation')
    day = checked_at.astimezone(JST).date().isoformat()
    calendar_sessions(calendar, day)
    due = publication_due(session, calendar)
    if available_at_check is True:
        state = 'AVAILABLE'
    elif day < due:
        state = 'NOT_YET_AVAILABLE'
    elif available_at_check is None:
        state = 'NOT_CHECKED'
    elif day == due:
        state = 'PENDING_PUBLICATION'
    else:
        state = 'MISSING_SOURCE_CANDIDATE'
    return dict(expected_session=session, published_session=session if available_at_check is True else None,
                missing_session=None, checked_at=checked_at.isoformat(),
                expected_publication_business_date=due, available_at_check=available_at_check,
                publication_state=state, published_exact_time='UNKNOWN',
                backfill_enqueue=False,
                one_shot_availability='AVAILABLE' if available_at_check is True else
                ('NOT_YET_AVAILABLE' if available_at_check is False else 'NOT_CHECKED'))


def plan(day, calendar, entries, missing, root, *, published_sessions=()):
    """Only visibly published sessions are downloadable.

    `missing` is an explicitly approved backlog, never the automatically inferred
    set difference between expected sessions and a page listing.
    """
    sessions = calendar_sessions(calendar, day)
    if day not in sessions:
        return []
    prior = [s for s in sessions if s < day]
    if not prior:
        raise ValueError('REVIEW_REQUIRED: previous session unknown')
    previous = prior[-1]
    published = set(published_sessions)
    if any(s not in sessions or s >= day for s in published):
        raise ValueError('REVIEW_REQUIRED: invalid published session')
    registered = set()
    root = Path(root).resolve()
    filenames = {e['filename'] for e in entries}
    if any(p.name not in filenames for p in root.glob('*.pdf')):
        raise ValueError('REVIEW_REQUIRED: unregistered local PDF; do not download duplicates')
    for e in entries:
        if not e.get('sha256'):
            raise ValueError('REVIEW_REQUIRED: unregistered hash')
        if not e.get('trash_moved_at'):
            name = e['filename']
            p = (root / name).resolve()
            if p.parent != root or not p.is_file() or digest(p) != e['sha256']:
                raise ValueError('REVIEW_REQUIRED: registered file mismatch')
        registered.add(e['session_date'])
    pending = sorted(set(missing))
    if any(s not in sessions or s >= day for s in pending):
        raise ValueError('REVIEW_REQUIRED: invalid missing session')
    result = []
    if previous not in registered and previous in published and publication_due(previous, calendar) <= day:
        result.append({'session_date': previous, 'acquisition_type': 'NORMAL'})
    old = [s for s in pending if s < previous and s not in registered
           and s in published and publication_due(s, calendar) < day]
    if old:
        result.append({'session_date': old[0], 'acquisition_type': 'BACKFILL'})
    return result


def persist_ledger_new(path, ledger):
    """Write a new immutable ledger revision; caller selects latest revision.

    Call before browser use and after every result. Exclusive creation refuses
    overwrite; interrupted IN_PROGRESS revisions prevent blind retry.
    """
    import json
    import os
    with Path(path).open('x', encoding='utf-8') as handle:
        json.dump(ledger, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())


def begin_attempt(ledger, target, now):
    if now.tzinfo is None or now.astimezone(JST).date().isoformat() != ledger['day']:
        raise ValueError('REVIEW_REQUIRED: ledger day mismatch')
    if ledger.get('stopped') or any(a['result'] == 'IN_PROGRESS' for a in ledger['attempts']):
        raise ValueError('STOP_FOR_THE_DAY')
    if target not in ledger['approved_plan']:
        raise ValueError('TARGET_NOT_APPROVED')
    successes = [a for a in ledger['attempts'] if a['result'] == 'SUCCESS']
    if len(successes) >= 2:
        raise ValueError('DAILY_CAP')
    if any(a['target']['session_date'] == target['session_date'] for a in successes):
        raise ValueError('DUPLICATE_BLOCKED')
    if any(a['target']['acquisition_type'] == target['acquisition_type'] for a in successes):
        raise ValueError('CATEGORY_CAP')
    attempts = [a for a in ledger['attempts'] if a['target'] == target]
    if len(attempts) >= 2:
        raise ValueError('RETRY_CAP')
    ledger['attempts'].append({'target': dict(target), 'started_at': now.isoformat(), 'result': 'IN_PROGRESS'})
    return len(ledger['attempts']) - 1


def finish_attempt(ledger, index, result):
    allowed = {'SUCCESS', 'TRANSIENT_FAILURE'} | RESTRICTIONS | HARD_FAILURES
    if result not in allowed or ledger['attempts'][index]['result'] != 'IN_PROGRESS':
        raise ValueError('INVALID_TRANSITION')
    ledger['attempts'][index]['result'] = result
    target = ledger['attempts'][index]['target']
    failures = sum(a['target'] == target for a in ledger['attempts'])
    if result in RESTRICTIONS | HARD_FAILURES or (result == 'TRANSIENT_FAILURE' and failures >= 2):
        ledger['stopped'] = True
        ledger['status'] = 'REVIEW_REQUIRED'
    return ledger

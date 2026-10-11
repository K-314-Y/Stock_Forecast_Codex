"""Post-08:45 research-only diagnostic path.

This module never creates a formal/live/scored forecast. It preserves the actual
run time while forcing all admissible evidence through the normal 08:45 cutoff.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from engine import minimal_trial as core

STATUS = 'LATE_RESEARCH_DIAGNOSTIC'
JST = ZoneInfo('Asia/Tokyo')


def nominal_freeze(target_session: str) -> str:
    return f'{target_session}T08:45:00+09:00'


def build_snapshot(target_session, expected_previous_session, actual_run_time,
                   stock_inputs, market=None, news=None, metrics=None):
    run = datetime.fromisoformat(actual_run_time)
    if run.tzinfo is None:
        raise ValueError('NAIVE_RESEARCH_RUN_TIME')
    run_jst = run.astimezone(JST)
    if run_jst.date().isoformat() != target_session:
        raise ValueError('RESEARCH_RUN_TARGET_DATE_MISMATCH')
    freeze = datetime.fromisoformat(nominal_freeze(target_session))
    if run_jst <= freeze:
        raise ValueError('USE_NORMAL_PRE_0845_PATH')

    # Every price/training dependency must carry its availability evidence.
    # A session date or a later import timestamp cannot prove earlier availability.
    for item in stock_inputs.values():
        evidence = item.get('provenance', {}).get('evidence')
        if not isinstance(evidence, list) or not evidence:
            raise ValueError('MISSING_PRE_0845_STOCK_EVIDENCE')
        for datum in evidence:
            if not isinstance(datum, dict) or not datum.get('source') or not datum.get('available_at'):
                raise ValueError('MISSING_PRE_0845_STOCK_EVIDENCE')
            if not core.available_by(datum['available_at'], freeze):
                raise ValueError('POST_0845_STOCK_EVIDENCE_FORBIDDEN')
            if datum.get('retrieved_at') and not core.available_by(datum['retrieved_at'], freeze):
                raise ValueError('POST_0845_STOCK_EVIDENCE_FORBIDDEN')

    base = core.compact_snapshot(
        target_session,
        expected_previous_session,
        freeze.isoformat(),
        stock_inputs,
        market=market,
        news=news,
        metrics=metrics,
    )
    base['status'] = STATUS
    base['actual_run_time'] = run.isoformat()
    base['nominal_freeze_time'] = freeze.isoformat()
    base['eligibility'] = {
        'formal_forecast': False,
        'forward_trial': False,
        'score': False,
        'prediction_log': False,
        'research_diagnostic_only': True,
    }
    base['late_run_reason'] = 'POST_0845_ACTUAL_RUN_USING_ONLY_PRE_0845_ADMISSIBLE_EVIDENCE'
    return base


def validate_record(record, snapshot):
    if snapshot.get('status') != STATUS:
        raise ValueError('LATE_RESEARCH_SNAPSHOT_REQUIRED')
    if record.get('status') != STATUS or record.get('ticker') not in core.CORE:
        raise ValueError('LATE_RESEARCH_CORE6_RECORD_REQUIRED')
    if record.get('target_session') != snapshot.get('target_session'):
        raise ValueError('RESEARCH_SESSION_MISMATCH')
    if record.get('nominal_freeze_time') != snapshot.get('nominal_freeze_time'):
        raise ValueError('RESEARCH_FREEZE_MISMATCH')
    expected_hash = hashlib.sha256(core.canonical_bytes(snapshot)).hexdigest()
    if record.get('input_snapshot_hash') != expected_hash:
        raise ValueError('RESEARCH_SNAPSHOT_HASH_MISMATCH')

    created = datetime.fromisoformat(record['created_at'])
    actual_run = datetime.fromisoformat(snapshot['actual_run_time'])
    freeze = datetime.fromisoformat(snapshot['nominal_freeze_time'])
    if created.tzinfo is None or created > actual_run or created <= freeze:
        raise ValueError('INVALID_LATE_RESEARCH_CREATED_AT')

    probs = [record.get(key) for key in core.CLASSES]
    if any(type(p) is not int or p < 5 or p > 95 or p % 5 for p in probs) or sum(probs) != 100:
        raise ValueError('INVALID_PROBABILITIES')
    order = ('FLAT', 'UP', 'DOWN')
    winner = max(order, key=lambda key: (record[key], -order.index(key)))
    if record.get('most_likely_class') != winner:
        raise ValueError('CLASS_PROBABILITY_MISMATCH')
    expected_confidence = 'HIGH' if max(probs) >= 60 else 'MEDIUM' if max(probs) >= 45 else 'LOW'
    if record.get('confidence') != expected_confidence:
        raise ValueError('CONFIDENCE_MISMATCH')
    if len(record.get('top_factors', [])) > 3:
        raise ValueError('TOO_MANY_FACTORS')
    for factor in record.get('top_factors', []):
        if (not factor.get('label') or factor.get('direction') not in core.CLASSES
                or datetime.fromisoformat(factor['evidence_timestamp']) > freeze):
            raise ValueError('POST_0845_EVIDENCE_FORBIDDEN')

    eligibility = record.get('eligibility')
    if eligibility != snapshot['eligibility'] or any(
        eligibility[k] for k in ('formal_forecast', 'forward_trial', 'score', 'prediction_log')
    ):
        raise ValueError('RESEARCH_ELIGIBILITY_VIOLATION')
    if not eligibility.get('research_diagnostic_only'):
        raise ValueError('RESEARCH_ONLY_FLAG_REQUIRED')
    return True


def write_record(record, snapshot, directory):
    validate_record(record, snapshot)
    dest = Path(directory) / f"{record['ticker']}_{record['created_at'].replace(':', '').replace('+', '_')}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = core.canonical_bytes(record)
    with dest.open('xb') as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    seal = dest.with_suffix('.sha256')
    with seal.open('x', encoding='ascii') as handle:
        handle.write(hashlib.sha256(payload).hexdigest() + '\n')
        handle.flush()
        os.fsync(handle.fileno())
    return dest

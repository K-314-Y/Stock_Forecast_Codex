"""Synthetic legacy writer for read-only receipt regression fixtures only.

Never import this helper from application code. No acquired market content.
"""
import hashlib
import hmac
import json
import os
import shutil
import tempfile
from pathlib import Path
from engine import fresh_bridge as bridge
from engine.fresh_bridge import (CORE_ISINS, _receipt_key, _receipt_root, _overview,
                                  _bridge_source, normalize_observation, _receipt_time)
from engine import minimal_trial as base


def _receipt_now():
    return bridge._receipt_now()


def synthetic_legacy_receipt(ticker, expected_session, fetch_existing):
    """Opt-in wrapper for the EXISTING ticker-bound connector invocation.

    The acquisition controller supplies the existing tool callable, not a
    timestamp. It must bind that call to ticker/entity as in the provider test.
    No scheduler, automatic call, retroactive receipt import or forecast here.
    Only this producer can authenticate new receipts. Files are create-only.
    """
    if ticker not in base.CORE:
        raise ValueError('CORE_ONLY')
    key = _receipt_key(create=True)
    started = _receipt_now()
    response = fetch_existing()
    completed = _receipt_now()
    if started.tzinfo is None or completed.tzinfo is None or completed < started:
        raise ValueError('BRIDGE_ACQUISITION_CLOCK_INVALID')
    # Copy exactly the response that completed, before extracting any prices.
    payload = base.canonical_bytes(response)
    overview = _overview(json.loads(payload))
    if overview.get('isin') != CORE_ISINS[ticker]:
        raise ValueError('BRIDGE_PROVIDER_TICKER_MISMATCH')
    source = _bridge_source(overview, completed)
    observation = normalize_observation(ticker, source, expected_session, completed.isoformat())
    session_close = _receipt_time(expected_session + 'T15:30:00+09:00')
    if (observation['freshness_state'] != 'FRESH' or completed < session_close
            or _receipt_time(overview['timestamp']) < session_close):
        raise ValueError('BRIDGE_UNVALIDATED_OR_STALE')
    receipt = dict(schema_version='fresh-bridge-receipt-v1', source_role='FORECAST_FEATURE',
                   source='BIGDATA/FMP', source_tool='bigdata_company_tearsheet',
                   source_section='company_overview', ticker=ticker, session=expected_session,
                   close=overview['price'], provider_timestamp=overview['timestamp'],
                   retrieval_started_at=started.isoformat(),
                   retrieval_completed_at=completed.isoformat(),
                   retrieved_at=completed.isoformat(), source_data_sha256=hashlib.sha256(payload).hexdigest())
    signature = hmac.new(key, base.canonical_bytes(receipt), hashlib.sha256).hexdigest()
    envelope = dict(receipt=receipt, hmac_sha256=signature)
    identifier = hashlib.sha256(base.canonical_bytes(envelope)).hexdigest()
    destination = _receipt_root() / identifier
    stage = Path(tempfile.mkdtemp(prefix='.pending-', dir=_receipt_root()))
    try:
        for name, data in [('source.json', payload), ('receipt.json', base.canonical_bytes(envelope))]:
            with (stage / name).open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        # Serialize commits using an exclusive reservation, including concurrent
        # same-content receipts. A partial .pending-* package is never admitted.
        reservation = _receipt_root() / (identifier + '.reserved')
        with reservation.open('xb'):
            if destination.exists():
                raise FileExistsError('BRIDGE_RECEIPT_CREATE_ONLY')
            os.rename(stage, destination)
        fd = os.open(_receipt_root(), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return dict(source_role='FORECAST_FEATURE', source_kind='FRESH_CLOSE_BRIDGE',
                path=str(destination / 'source.json'), sha256=receipt['source_data_sha256'],
                receipt=str(destination / 'receipt.json'), session_end=expected_session)

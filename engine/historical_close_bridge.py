"""Local Historical Gap registration, distinct from live Fresh Close acquisition.

No network, parser changes, forecasts, outcomes or cache mutation. JPX originals
remain OUTCOME_ONLY. Only independently registered, close-only derivatives are
admitted to NON_FORMAL pipeline probes after their actual registration time.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

from engine import minimal_trial as core
from engine import official_gap as gap
from engine import fresh_bridge

JST = ZoneInfo('Asia/Tokyo')


def _now():
    return datetime.now(JST)


def _root():
    return core.ROOT / 'data/historical_close_bridge/jp'


def _key(create=False):
    """Independent Historical producer key; no access to the Fresh producer key."""
    root = _root()
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / '.producer-key'
    if create and not path.exists():
        fd, pending = tempfile.mkstemp(prefix='.pending-key-', dir=root)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(os.urandom(32))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(pending, path, follow_symlinks=False)
            except FileExistsError:
                pass
        finally:
            os.unlink(pending)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        if os.fstat(stream.fileno()).st_mode & 0o077:
            raise ValueError('HISTORICAL_KEY_PERMISSIONS')
        key = stream.read()
    if len(key) != 32:
        raise ValueError('HISTORICAL_KEY_INVALID')
    return key


def _policy():
    path = core.ROOT / 'config/official_gap_policy.json'
    policy = json.loads(path.read_bytes())
    if any(policy.get(field) is not True for field in (
            'HISTORICAL_TO_OFFICIAL_BRIDGE_READY',
            'FEATURE_SERIES_OPERATIONALLY_COMPATIBLE_FOR_MINIMAL_TRIAL',
            'ADJUSTMENT_BASIS_COMPATIBLE_FOR_MINIMAL_TRIAL')):
        raise ValueError('HISTORICAL_BASIS_NOT_APPROVED_FOR_PROBE')
    return policy, path, core.sha256(path)


def register_existing(day, forecast_target):
    """Validate and register existing JPX data now; caller cannot set the clock."""
    from engine import forward_artifacts as probe
    calendar = json.loads((core.ROOT / 'analysis/calendar_validation_2026-09-12.json').read_bytes())
    previous = fresh_bridge.expected_previous(calendar, forecast_target)
    policy, policy_path, policy_hash = _policy()
    allowed = [s for s in calendar['jp_sessions'] if policy['historical_cache_end'] < s < previous]
    if day not in allowed:
        raise ValueError('HISTORICAL_SESSION_NOT_APPROVED_OR_LATEST_FRESH_SESSION')
    cutoff = probe._stamp(forecast_target + 'T08:45:00+09:00')
    if _now() >= cutoff:
        raise ValueError('HISTORICAL_REGISTRATION_AFTER_CUTOFF')
    manifest_path = core.ROOT / 'data/outcome_input/manual_jpx/retention_manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    entries = [e for e in manifest['entries'] if e['session_date'] == day]
    if len(entries) != 1:
        raise ValueError('HISTORICAL_SOURCE_MISSING_OR_AMBIGUOUS')
    entry = entries[0]
    parsed_path = (core.ROOT / entry['normalized_output_path']).resolve()
    parsed, provenance = probe._validated_jpx(parsed_path, day, _now())
    raw_path = core._resolve_jpx_raw_source(parsed).resolve()
    destination = _root() / day
    if destination.exists():
        evidence = dict(source_role='FORECAST_FEATURE', source_kind='HISTORICAL_CLOSE_BRIDGE',
            path=str(destination / 'record.json'), sha256=core.sha256(destination / 'record.json'), session_end=day)
        verify(evidence, core.CORE[0], _now(), cutoff, previous)
        return evidence
    key = _key(create=True)
    def build(stage):
        staged = stage / 'unregistered_layer.json'
        layer = gap.stage_existing_pdf(parsed_path, manifest, calendar, raw_path.parent,
                                       staged, allowed_sessions=allowed)
        staged.unlink()
        record = dict(schema_version='historical-close-bridge-v1',
            source_role='FORECAST_FEATURE', source_kind='HISTORICAL_CLOSE_BRIDGE',
            upstream_source_role='OUTCOME_ONLY', source='JPX_DAILY_REPORT_REGISTERED_HISTORICAL_CLOSE',
            eligibility='NON_FORMAL', session_date=day,
            records=[dict(ticker=r['ticker'], session_date=day, close=r['close']) for r in layer['records']],
            registered_at=_now().isoformat(),
            registered_at_meaning='ACTUAL_LOCAL_VALIDATED_HISTORICAL_REGISTRATION_NOT_ORIGINAL_RETRIEVAL',
            original_retrieved_at=parsed['retrieved_at'], original_downloaded_at=entry.get('downloaded_at'),
            raw_path=str(raw_path), raw_sha256=provenance['raw_sha256'],
            parsed_path=str(parsed_path), parsed_sha256=provenance['parsed_sha256'],
            manifest_entry=entry, manifest_entry_sha256=hashlib.sha256(core.canonical_bytes(entry)).hexdigest(),
            policy_path=str(policy_path.resolve()), policy_sha256=policy_hash)
        record['source_data_sha256'] = hashlib.sha256(core.canonical_bytes(record['records'])).hexdigest()
        record['hmac_sha256'] = hmac.new(key, core.canonical_bytes(record), hashlib.sha256).hexdigest()
        probe._seal(stage / 'record.json', record)
    def confirm(stage):
        if _now() >= cutoff:
            raise ValueError('HISTORICAL_REGISTRATION_AFTER_CUTOFF')
        record = core.read_frozen(stage / 'record.json')
        for field in ('raw', 'parsed', 'policy'):
            if core.sha256(record[field + '_path']) != record[field + '_sha256']:
                raise ValueError('HISTORICAL_SOURCE_CHANGED_DURING_REGISTRATION')
    # Use the existing create-only lock + atomic, durable artifact commit.
    with probe._locked(_root()):
        probe._commit(destination, build, confirm)
    return dict(source_role='FORECAST_FEATURE', source_kind='HISTORICAL_CLOSE_BRIDGE',
        path=str(destination / 'record.json'), sha256=core.sha256(destination / 'record.json'), session_end=day)


def verify_day(evidence, created, cutoff, previous):
    """Accept only authenticated Historical derivatives, never JPX files directly."""
    from engine import forward_artifacts as probe
    day = evidence['session_end']
    path = Path(evidence['path']).resolve()
    if (evidence['source_role'] != 'FORECAST_FEATURE'
            or path != (_root() / day / 'record.json').resolve()):
        raise ValueError('UNTRUSTED_HISTORICAL_BRIDGE_EVIDENCE')
    if core.sha256(path) != evidence['sha256']:
        raise ValueError('INPUT_HASH_MISMATCH')
    record = core.read_frozen(path)
    signed = dict(record)
    signature = signed.pop('hmac_sha256')
    expected = hmac.new(_key(), core.canonical_bytes(signed), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError('HISTORICAL_RECEIPT_AUTHENTICATION_FAILED')
    if (record['source_role'] != 'FORECAST_FEATURE' or record['source_kind'] != 'HISTORICAL_CLOSE_BRIDGE'
            or record['eligibility'] != 'NON_FORMAL' or record['upstream_source_role'] != 'OUTCOME_ONLY'
            or record['session_date'] != day or day >= previous):
        raise ValueError('HISTORICAL_SESSION_OR_ROLE_MISMATCH')
    stamp = probe._stamp(record['registered_at'])
    if stamp > created or stamp >= cutoff:
        raise ValueError('POST_GENERATION_INPUT_EVIDENCE')
    for field in ('retrieved_at', 'available_at'):
        if field in evidence and probe._stamp(evidence[field]) != stamp:
            raise ValueError('CALLER_RECEIPT_TIME_MISMATCH')
    if hashlib.sha256(core.canonical_bytes(record['records'])).hexdigest() != record['source_data_sha256']:
        raise ValueError('HISTORICAL_SOURCE_BINDING_MISMATCH')
    policy, policy_path, policy_hash = _policy()
    if record['policy_sha256'] != policy_hash or record['policy_path'] != str(policy_path.resolve()):
        raise ValueError('HISTORICAL_POLICY_CHANGED')
    parsed, provenance = probe._validated_jpx(Path(record['parsed_path']), day, created)
    raw = core._resolve_jpx_raw_source(parsed).resolve()
    if (str(raw) != record['raw_path'] or provenance['raw_sha256'] != record['raw_sha256']
            or provenance['parsed_sha256'] != record['parsed_sha256']):
        raise ValueError('HISTORICAL_SOURCE_BINDING_MISMATCH')
    manifest_path = core.ROOT / 'data/outcome_input/manual_jpx/retention_manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    entries = [e for e in manifest['entries'] if e['session_date'] == day]
    if len(entries) != 1 or hashlib.sha256(core.canonical_bytes(entries[0])).hexdigest() != record['manifest_entry_sha256']:
        raise ValueError('HISTORICAL_MANIFEST_ENTRY_CHANGED')
    rows = sorted([dict(ticker=r['ticker'], session_date=day, close=r['close']) for r in parsed['records']], key=lambda r:r['ticker'])
    if rows != record['records']:
        raise ValueError('HISTORICAL_PRICE_SOURCE_MISMATCH')
    closes = {r['ticker']: (r['session_date'], r['close']) for r in rows}
    if len(rows) != len(core.CORE) or set(closes) != set(core.CORE):
        raise ValueError('HISTORICAL_EXTENSION_GRAIN_INVALID')
    hashes = {str(path): evidence['sha256'], str(path.with_suffix('.sha256')): core.sha256(path.with_suffix('.sha256')),
              str(raw): record['raw_sha256'], record['parsed_path']: record['parsed_sha256'],
              str(manifest_path.resolve()): provenance['manifest_sha256'], str(policy_path.resolve()): policy_hash}
    return closes, hashes


def verify(evidence, ticker, created, cutoff, previous):
    """Backward-compatible ticker API; no cache survives this call."""
    if ticker not in core.CORE:
        raise ValueError('UNTRUSTED_HISTORICAL_BRIDGE_EVIDENCE')
    closes, hashes = verify_day(evidence, created, cutoff, previous)
    return [closes[ticker]], hashes

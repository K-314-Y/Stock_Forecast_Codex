"""Offline, authenticated Forward history refresh and immutable registration.

The Task 10A anchor never changes. Mutable retention evidence can advance only
through revalidated official entries and authenticated Historical Close records.
These APIs do not retrieve Fresh, produce features, Forecast, Freeze or PIT rows.
"""
from datetime import date, datetime, timedelta
import hashlib
import hmac
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from engine import execution_policy as policy
from engine import forward_artifacts as artifacts
from engine import forward_storage as storage
from engine import historical_close_bridge as historical
from engine import minimal_trial as core

REASON = 'OFFICIAL_HISTORY_ADVANCED'
SCHEMA = 'forward-history-registration-v1'


def _now():
    return datetime.now(ZoneInfo('Asia/Tokyo'))


def _digest(record):
    return hashlib.sha256(core.canonical_bytes(record)).hexdigest()


def _store():
    return core.ROOT / 'data/forward_trial_history/registrations'


def _manifest_path():
    return core.ROOT / 'data/outcome_input/manual_jpx/retention_manifest.json'


def _bytes(path):
    path = Path(path).absolute()
    if '..' in path.parts or not path.is_relative_to(core.ROOT.absolute()):
        raise ValueError('FORWARD_HISTORY_PATH_INVALID')
    with storage.secure_directory(path.parent) as fd:
        return storage.read_at(fd, path.name)


def _hash(path):
    return hashlib.sha256(_bytes(path)).hexdigest()


def _frozen(path, *, with_hashes=False):
    body = _bytes(path)
    sidecar_path = Path(path).with_suffix('.sha256')
    sidecar = _bytes(sidecar_path)
    digest = hashlib.sha256(body).hexdigest()
    if sidecar.decode().strip() != digest:
        raise ValueError('FORWARD_HISTORY_HASH_MISMATCH')
    record = artifacts.strict_json(body)
    if with_hashes:
        return record, {str(path): digest, str(sidecar_path): hashlib.sha256(sidecar).hexdigest()}
    return record


def _unchanged(hashes):
    if any(_hash(path) != expected for path, expected in hashes.items()):
        raise ValueError('INPUT_CHANGED_BEFORE_FREEZE')


def _authenticated(record):
    signed = dict(record)
    signature = signed.pop('hmac_sha256')
    expected = hmac.new(historical._key(), core.canonical_bytes(signed), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError('HISTORICAL_RECEIPT_AUTHENTICATION_FAILED')


def _signed(record):
    return dict(record, hmac_sha256=hmac.new(historical._key(), core.canonical_bytes(record),
                                            hashlib.sha256).hexdigest())


def _anchor(rules, calendar, thresholds, now):
    relative = rules['historical_snapshot']
    expected = 'data/forward_trial_history/' + rules['historical_through'] + '/history.json'
    if relative != expected:
        raise ValueError('FORWARD_HISTORY_ANCHOR_PATH_INVALID')
    path = core.ROOT / relative
    record, bound = _frozen(path, with_hashes=True)
    _authenticated(record)
    if (record['schema_version'] != 'validated-forward-history-v1'
            or record['approved_preparation_sha256'] != rules['approved_preparation_sha256']
            or record['historical_through'] != rules['historical_through']
            or record['fresh_rows_excluded'] is not True
            or artifacts._stamp(record['registered_at']) > now):
        raise ValueError('HISTORICAL_INPUT_INVALID')
    from engine.forward_trial_inputs import _validate_history_rows
    _validate_history_rows(record['stocks'], calendar, thresholds, record['historical_through'])
    retention = str(_manifest_path())
    if retention not in record['source_hashes']:
        raise ValueError('FORWARD_HISTORY_RETENTION_BINDING_REQUIRED')
    refs = {p: digest for p, digest in record['source_hashes'].items() if p != retention}
    _unchanged(refs)
    refs.update(bound)
    return record, refs


def _validated_manifest(now, calendar):
    """Validate every entry, never bless an arbitrary replacement's new hash."""
    path = _manifest_path()
    body = _bytes(path)
    manifest = artifacts.strict_json(body)
    entries = manifest['entries']
    days = [e['session_date'] for e in entries]
    if not entries or days != sorted(set(days)):
        raise ValueError('INPUT_CHANGED_BEFORE_FREEZE:MANIFEST_GRAIN_INVALID')
    for entry in entries:
        day = entry['session_date']
        # Training anchor may include the prior cached session before the calendar
        # validation range; later official entries must be calendar members.
        if day >= calendar['validation_range']['start'] and day not in calendar['jp_sessions']:
            raise ValueError('INPUT_CHANGED_BEFORE_FREEZE:MANIFEST_CALENDAR_INVALID')
        parsed_path = core.ROOT / entry['normalized_output_path']
        _bytes(parsed_path)  # Reject path traversal/symlinks before existing validation.
        try:
            parsed, _ = artifacts._validated_jpx(parsed_path, day, now)
            _bytes(core._resolve_jpx_raw_source(parsed))
            methods = {'HUMAN_MANUAL_DOWNLOAD': 'HUMAN_MANUAL_DOWNLOAD',
                       'COMPUTER_USE_BROWSER': 'BROWSER_ASSISTED_SINGLE_FILE',
                       'OFFICIAL_LISTING_DIRECT_HTTPS': 'OFFICIAL_LISTING_DIRECT_HTTPS'}
            origin = entry.get('acquisition_origin')
            if origin not in methods or parsed.get('acquisition_method') != methods[origin]:
                raise ValueError('SOURCE_IDENTITY_MISMATCH')
            if parsed['acquisition_method'] == 'OFFICIAL_LISTING_DIRECT_HTTPS':
                from providers.jp.jpx_manual import historical_direct as direct
                folder = parsed_path.parent
                for name in ('acquisition.json', 'listing.html'):
                    _bytes(folder / name)
                receipt, _ = direct._verified_acquisition(folder, day)
                if receipt != entry['source_metadata']:
                    raise ValueError('SOURCE_IDENTITY_MISMATCH')
        except (ValueError, KeyError, OSError) as error:
            raise ValueError('INPUT_CHANGED_BEFORE_FREEZE:INVALID_OFFICIAL_MANIFEST') from error
    return dict(path=str(path), sha256=hashlib.sha256(body).hexdigest(),
                entry_hashes={e['session_date']: _digest(e) for e in entries})


def _build_manifest(history_end, now):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('TIMEZONE_REQUIRED')
    policy_path = core.ROOT / 'config/forward_trial_execution_policy.json'
    policy_body = _bytes(policy_path)
    rules = artifacts.strict_json(policy_body)
    policy_hash = hashlib.sha256(policy_body).hexdigest()
    calendar_path = core.ROOT / rules['calendar']
    calendar_body = _bytes(calendar_path)
    if hashlib.sha256(calendar_body).hexdigest() != rules['calendar_sha256']:
        raise ValueError('VALIDATED_CALENDAR_HASH_MISMATCH')
    calendar = artifacts.strict_json(calendar_body)
    days = calendar['jp_sessions']
    if (not days or days != sorted(set(days))
            or any(date.fromisoformat(day).isoformat() != day for day in days)):
        raise ValueError('VALIDATED_CALENDAR_INVALID')
    from engine.forward_trial_inputs import _frozen_candidates, _validate_history_rows
    thresholds, frozen = _frozen_candidates(history_end)
    anchor, refs = _anchor(rules, calendar, thresholds, now)
    if (history_end not in calendar['jp_sessions'] or history_end < rules['historical_through']
            or artifacts._stamp(history_end + 'T15:30:00+09:00') >= now):
        raise ValueError('FORWARD_HISTORY_END_INVALID')
    retention = _validated_manifest(now, calendar)
    stocks = {ticker: list(rows) for ticker, rows in anchor['stocks'].items()}
    # Revalidate the entire official prefix: signed bridge entries certify that
    # original manifest entries and close values have not been substituted.
    next_days = [d for d in calendar['jp_sessions'] if d > history_end]
    if not next_days:
        raise ValueError('VALIDATED_CALENDAR_EXHAUSTED')
    previous = next_days[0]
    # Offline refresh certifies availability now. It does not pretend the new
    # evidence existed before an earlier Forecast cutoff (including NOT_RUN).
    cutoff = now + timedelta(microseconds=1)
    for day in calendar['jp_sessions']:
        if thresholds[core.CORE[0]]['training_end_date'] < day <= history_end:
            path = historical._root() / day / 'record.json'
            if day <= anchor['historical_through'] and str(path) not in anchor['source_hashes']:
                # Retain independently approved Task 10A rows (e.g. a historical
                # anchor day without a JPX bridge). Never invent new provenance
                # or import saved Fresh; all original anchor hashes still hold.
                continue
            record, bound = _frozen(path, with_hashes=True)
            _authenticated(record)
            _bytes(record['parsed_path'])
            _bytes(record['raw_path'])
            evidence = dict(source_role='FORECAST_FEATURE', source_kind='HISTORICAL_CLOSE_BRIDGE',
                            path=str(path), sha256=bound[str(path)], session_end=day)
            closes, hashes = historical.verify_day(evidence, now, cutoff, previous)
            for ticker in core.CORE:
                session, close = closes[ticker]
                if session != day:
                    raise ValueError('HISTORICAL_EXTENSION_GRAIN_INVALID')
                row = dict(session_date=day, close=close)
                if day <= anchor['historical_through']:
                    if row not in stocks[ticker]:
                        raise ValueError('INPUT_CHANGED_BEFORE_FREEZE:ANCHORED_PRICE_MISMATCH')
                else:
                    stocks[ticker].append(row)
            refs.update(hashes)
    refs.update(frozen)
    refs[retention['path']] = retention['sha256']
    refs.update({str(policy_path): policy_hash, str(calendar_path): rules['calendar_sha256']})
    _validate_history_rows(stocks, calendar, thresholds, history_end)
    _unchanged(refs)
    return dict(schema_version='forward-history-manifest-v1', history_end=history_end,
                anchor=dict(path=rules['historical_snapshot'], sha256=refs[str(core.ROOT / rules['historical_snapshot'])]),
                retention_manifest=retention, calendar_identity=dict(path=rules['calendar'], sha256=rules['calendar_sha256']),
                policy_identity=dict(path='config/forward_trial_execution_policy.json', sha256=policy_hash),
                stocks=stocks, source_hashes=refs, fresh_rows_excluded=True)


def _envelope(fd, name, *, with_hash=False):
    body = storage.read_at(fd, name)
    value = artifacts.strict_json(body)
    if set(value) != {'record', 'sha256'} or _digest(value['record']) != value['sha256']:
        raise ValueError('INPUT_CHANGED_BEFORE_FREEZE:ENVELOPE_HASH_MISMATCH')
    result = value['record'], value['sha256']
    return (*result, hashlib.sha256(body).hexdigest()) if with_hash else result


def _identity(value):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise ValueError('FORWARD_HISTORY_IDENTITY_INVALID')
    return value


def _finish_intent(fd, expected):
    try:
        intent, digest = _envelope(fd, '.registration-intent.json')
    except FileNotFoundError:
        return
    _authenticated(intent)
    if digest != expected:
        raise ValueError('FORWARD_HISTORY_REGISTRATION_CONFLICT')
    os.unlink('.registration-intent.json', dir_fd=fd)
    os.fsync(fd)


def refresh_forward_history_manifest(history_end=None):
    """Deterministic offline refresh; identical validated inputs create no files."""
    history_end = policy.load_policy()['historical_through'] if history_end is None else history_end
    with storage.secure_directory(_store(), create=True, lock=True) as fd:
        record = _build_manifest(history_end, _now())
        digest = _digest(record)
        name = 'manifest-' + digest + '.json'
        storage.write_envelope_at(fd, name, record, confirm=lambda: _unchanged(record['source_hashes']))
        _envelope(fd, name)  # Required read-back including hash validation.
    return dict(path=str(_store() / name), sha256=digest, history_end=history_end)


def register_forward_snapshot(manifest_reference, *, previous_snapshot=None, reason=REASON):
    """Authenticate and create a new registration, never overwrite old snapshots.

    The current pointer is a replaceable hash projection. Registrations and
    manifests are create-only. CAS against previous_snapshot fails closed.
    """
    digest = _identity(manifest_reference['sha256'])
    manifest_name = 'manifest-' + digest + '.json'
    if Path(manifest_reference['path']) != _store() / manifest_name or reason != REASON:
        raise ValueError('FORWARD_HISTORY_REGISTRATION_INVALID')
    with storage.secure_directory(_store(), lock=True) as fd:
        manifest, actual = _envelope(fd, manifest_name)
        if actual != digest or _build_manifest(manifest['history_end'], _now()) != manifest:
            raise ValueError('INPUT_CHANGED_BEFORE_FREEZE')
        try:
            intent, intent_sha = _envelope(fd, '.registration-intent.json')
            _authenticated(intent)
        except FileNotFoundError:
            intent = None
        try:
            current, _ = _envelope(fd, 'current.json')
            prior, prior_sha = _envelope(fd, 'snapshot-' + _identity(current['sha256']) + '.json')
            _authenticated(prior)
            if prior['schema_version'] != SCHEMA or prior_sha != current['sha256']:
                raise ValueError('FORWARD_HISTORY_CURRENT_INVALID')
            prior_identity = dict(path=str(_store() / ('snapshot-' + prior_sha + '.json')), sha256=prior_sha)
            if prior['manifest_sha256'] == digest:
                if previous_snapshot not in (None, prior_identity, prior['previous_snapshot_identity']):
                    raise ValueError('FORWARD_HISTORY_REGISTRATION_CONFLICT')
                _unchanged(manifest['source_hashes'])
                storage.write_envelope_at(fd, 'current.json', current)
                _finish_intent(fd, prior_sha)
                return prior_identity
            previous_manifest, previous_digest = _envelope(fd, 'manifest-' + _identity(prior['manifest_sha256']) + '.json')
            if previous_digest != prior['manifest_sha256']:
                raise ValueError('INPUT_CHANGED_BEFORE_FREEZE')
            if manifest['history_end'] < prior['history_end']:
                raise ValueError('FORWARD_HISTORY_REGISTRATION_CONFLICT')
            for ticker in core.CORE:
                prefix = [r for r in manifest['stocks'][ticker] if r['session_date'] <= prior['history_end']]
                if prefix != previous_manifest['stocks'][ticker]:
                    raise ValueError('FORWARD_HISTORY_REGISTRATION_CONFLICT')
            old_entries = previous_manifest['retention_manifest']['entry_hashes']
            new_entries = manifest['retention_manifest']['entry_hashes']
            if any(new_entries.get(day) != sha for day, sha in old_entries.items()):
                raise ValueError('INPUT_CHANGED_BEFORE_FREEZE')
            if manifest['history_end'] == prior['history_end'] and set(new_entries) <= set(old_entries):
                raise ValueError('FORWARD_HISTORY_REGISTRATION_CONFLICT')
        except FileNotFoundError:
            if 'current' in locals() or intent is None and any(n.startswith('snapshot-') for n in os.listdir(fd)):
                raise ValueError('FORWARD_HISTORY_CURRENT_MISSING')
            anchor = manifest['anchor']
            prior_identity = dict(path=str(core.ROOT / anchor['path']), sha256=anchor['sha256'])
        if previous_snapshot is not None and previous_snapshot != prior_identity:
            raise ValueError('FORWARD_HISTORY_REGISTRATION_CONFLICT')
        commit = storage.code_commit()
        fields = dict(schema_version=SCHEMA, previous_snapshot_identity=prior_identity,
                      manifest_sha256=digest, history_end=manifest['history_end'],
                      calendar_identity=manifest['calendar_identity'], policy_identity=manifest['policy_identity'],
                      code_commit=commit, reason=reason)
        if intent is not None:
            if (any(intent.get(key) != value for key, value in fields.items())
                    or artifacts._stamp(intent['registered_at']) > _now()):
                raise ValueError('FORWARD_HISTORY_REGISTRATION_CONFLICT')
            record = intent
        else:
            record = _signed(dict(fields, registered_at=_now().isoformat()))
        sha = _digest(record)
        name = 'snapshot-' + sha + '.json'
        storage.write_envelope_at(fd, '.registration-intent.json', record,
                                  confirm=lambda: _unchanged(manifest['source_hashes']))
        storage.write_envelope_at(fd, name, record, confirm=lambda: _unchanged(manifest['source_hashes']))
        _authenticated(_envelope(fd, name)[0])
        storage.write_envelope_at(fd, 'current.json', dict(sha256=sha), replace=True,
                                  confirm=lambda: _unchanged(manifest['source_hashes']))
        _envelope(fd, 'current.json')
        _finish_intent(fd, sha)
        return dict(path=str(_store() / name), sha256=sha)


def load_registered_history(calendar, thresholds, execution_started):
    """Read current immutable registration; return None only before registration."""
    # Absence is a compatibility path, but an existing damaged store fails closed.
    if not _store().exists() and not _store().is_symlink():
        return None
    with storage.secure_directory(_store(), lock=True) as fd:
        try:
            current, _, current_hash = _envelope(fd, 'current.json', with_hash=True)
        except FileNotFoundError:
            # Refreshed manifests alone never authorize a stale snapshot.
            if any(n.startswith('snapshot-') for n in os.listdir(fd)):
                raise ValueError('FORWARD_HISTORY_CURRENT_MISSING')
            return None
        registration_name = 'snapshot-' + _identity(current['sha256']) + '.json'
        registration, sha, registration_hash = _envelope(fd, registration_name, with_hash=True)
        _authenticated(registration)
        digest = _identity(registration['manifest_sha256'])
        manifest_name = 'manifest-' + digest + '.json'
        manifest, actual, manifest_hash = _envelope(fd, manifest_name, with_hash=True)
        if actual != digest:
            raise ValueError('INPUT_CHANGED_BEFORE_FREEZE')
        rules = policy.load_policy()
        if (sha != current['sha256'] or registration['schema_version'] != SCHEMA
                or registration['reason'] != REASON or registration['history_end'] != manifest['history_end']
                or registration['calendar_identity'] != manifest['calendar_identity']
                or registration['policy_identity'] != manifest['policy_identity']
                or registration['calendar_identity']['sha256'] != rules['calendar_sha256']
                or registration['policy_identity']['sha256'] != _hash(core.ROOT / 'config/forward_trial_execution_policy.json')
                or artifacts._stamp(registration['registered_at']) > execution_started
                or manifest['fresh_rows_excluded'] is not True):
            raise ValueError('HISTORICAL_INPUT_INVALID')
        _unchanged(manifest['source_hashes'])
        # Authentication binds a manifest SHA, so recomputing an attacker-changed
        # envelope SHA can never substitute it for this signed registration.
        from engine.forward_trial_inputs import _validate_history_rows
        _validate_history_rows(manifest['stocks'], calendar, thresholds, manifest['history_end'])
        hashes = dict(manifest['source_hashes'])
        hashes.update({str(_store() / 'current.json'): current_hash,
                       str(_store() / registration_name): registration_hash,
                       str(_store() / manifest_name): manifest_hash})
        _unchanged(hashes)
        return manifest['stocks'], hashes, manifest['history_end']

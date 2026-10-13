"""Historical-only STOP lifecycle; immutable evidence and a small current index.

Callers serialize mutations with the acquisition store lock. No forecast state,
network, or automatic HARD_STOP release is available here.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from engine import minimal_trial as core
from engine import forward_artifacts as frozen

VERSION = 'STOP_RETRY_RESUME_V2'
SOFT_CODES = {'MANIFEST_NOT_VALIDATED', 'LOCAL_METADATA_SCHEMA_MISMATCH',
              'KNOWN_PARSER_FORMAT_DRIFT', 'LOCAL_VALIDATION_BUG_FIXED'}
HOST_CODES = {'HTTP_403', 'SECURITY_CHALLENGE', 'CAPTCHA', 'ACCESS_DENIED',
              'BOT_CHALLENGE', 'AUTHENTICATION_CHALLENGE', 'HOST_PERSISTENT_429',
              'RATE_LIMIT', 'NON_OFFICIAL_HTTPS_URL', 'UNSAFE_URL',
              'TRANSPORT_FOLLOWED_REDIRECT_WITHOUT_CHECK'}
SESSION_CODES = {'SESSION_CONFLICT', 'OHLCV_INVALID', 'DUPLICATE_TICKER',
                 'CORE6_OR_DUPLICATE_CONFLICT', 'UNKNOWN_OFFICIAL_REVISION'}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def reference(path):
    path = Path(path).resolve()
    return dict(path=str(path), sha256=core.sha256(path))


def verify_reference(ref):
    path = Path(ref['path'])
    if not path.is_file() or core.sha256(path) != ref['sha256']:
        raise ValueError('STOP_EVIDENCE_HASH_CONFLICT')
    sidecar = path.with_suffix('.sha256')
    if sidecar.exists() and sidecar.read_text().strip() != ref['sha256']:
        raise ValueError('STOP_EVIDENCE_HASH_CONFLICT')
    return path


def read_reference(ref):
    path = verify_reference(ref)
    return json.loads(path.read_bytes())


def classification(reason, session):
    code = reason.split(':', 1)[0]
    if code in HOST_CODES:
        return 'HARD_STOP', 'host', 'jpx.co.jp'
    if code in SOFT_CODES and session:
        return 'SOFT_STOP', 'session', session
    if session and (code in SESSION_CODES or reason.startswith('DATA_CONFLICT: session date')
                    or reason == 'DATA_CONFLICT: invalid OHLC order'):
        return 'HARD_STOP', 'session', session
    # Hash/provenance scope and unknown parser semantics are conservatively global.
    return 'HARD_STOP', 'global', 'JPX_HISTORICAL'


def write_current(path, index):
    """Atomically persist only the current summary, with a corruption check."""
    path = Path(path)
    envelope = dict(index=index, sha256=hashlib.sha256(core.canonical_bytes(index)).hexdigest())
    fd, name = tempfile.mkstemp(prefix='.current-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(core.canonical_bytes(envelope))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        frozen._sync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def seal(path, record):
    """Historical evidence: atomic create-only files and durable directory entries.

    Unlike frozen._seal, publish each complete file through a hard link. A crash
    between record and sidecar is detected by read_frozen, never treated as PASS.
    """
    path = Path(path)
    data = core.canonical_bytes(record)
    for target, payload in ((path, data), (path.with_suffix('.sha256'),
                            (hashlib.sha256(data).hexdigest() + '\n').encode())):
        fd, name = tempfile.mkstemp(prefix='.evidence-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(name, target)  # EEXIST; never replace prior evidence.
            frozen._sync_dir(path.parent)
        finally:
            if os.path.exists(name):
                os.unlink(name)


class Stops:
    def __init__(self, store):
        self._load_evidence(store)
        self._reviewed_legacy_references()

    def _load_evidence(self, store):
        # Only explicit renewal bootstraps through this loader. gate() still
        # revalidates the current generation, including on a bootstrap object.
        self.store = Path(store).resolve()
        self.path = self.store / 'current_stops_v2.json'
        if self.path.exists():
            envelope = json.loads(self.path.read_bytes())
            self.index = envelope['index']
            if hashlib.sha256(core.canonical_bytes(self.index)).hexdigest() != envelope['sha256']:
                raise ValueError('STOP_INDEX_HASH_CONFLICT')
        else:
            if (self.store / 'stops_v2').exists():
                raise ValueError('STOP_INDEX_MISSING_REVIEW_REQUIRED')
            self.index = dict(policy_version=VERSION, active=[], legacy=None, resumed=[])
        if 'resumed' not in self.index:
            # Older releases without a referenced receipt cannot prove their state.
            if (any((self.store / 'stops_v2').glob('resume-*.json'))
                    or self.index['legacy'] is not None and not any(
                        item['reference'] == self.index['legacy'] for item in self.index['active'])):
                raise ValueError('STOP_RESUME_HISTORY_MISSING_REVIEW_REQUIRED')
            self.index['resumed'] = []
        for release in self.index['resumed']:
            try:
                event = core.read_frozen(verify_reference(release['resume_event_reference']))
                previous = read_reference(release['previous_stop_reference'])
                resolution = read_reference(release['resolution_reference'])
                previous_kind = previous.get('stop_class')
                if previous_kind is None:
                    previous_kind, scope, target = classification(previous.get('reason', ''), previous.get('session'))
                    if (scope, target) != (release['scope'], release['target']):
                        raise ValueError('invalid legacy release scope')
                if (event.get('state') != 'RESUMED' or event.get('evidence_validation') != 'PASS'
                        or any(event.get(key) != release[key] for key in
                               ('previous_stop_reference', 'resolution_reference', 'scope', 'target'))
                        or previous_kind != 'SOFT_STOP'
                        or resolution.get('stop_reference') != release['previous_stop_reference']):
                    raise ValueError('invalid release lineage')
            except (ValueError, OSError, KeyError, TypeError) as error:
                raise ValueError('STOP_RESUME_RECEIPT_CONFLICT') from error
        legacy = self.store / 'STOP.json'
        if self.index['legacy'] is not None:
            verify_reference(self.index['legacy'])  # Removal/edit is never a release.
        elif legacy.exists():
            ref = reference(legacy)
            try:
                old = read_reference(ref)
                reason = old.get('stop_code') or old.get('reason', '')
                session = old.get('session')
                kind, scope, target = classification(reason, session)
                if old.get('stop_class') == 'HARD_STOP':
                    kind = 'HARD_STOP'  # Never downgrade a recorded HARD_STOP.
                known = reason.split(':', 1)[0] in HOST_CODES | SOFT_CODES | SESSION_CODES
                known = known or reason.startswith('DATA_CONFLICT: session date') or reason == 'DATA_CONFLICT: invalid OHLC order'
                code = reason if known else 'HARD_STOP_LEGACY_UNCLASSIFIED'
                record = dict(policy_version=VERSION, stop_id='legacy-' + ref['sha256'],
                              stop_code=code, stop_class=kind, scope=scope, target=target,
                              evidence_refs=[ref], created_at=old.get('failed_at'),
                              last_success=None, recoverability=kind, state='ACTIVE',
                              resume_condition='Explicit resolution and evidence revalidation' if kind == 'SOFT_STOP'
                              else 'Human review; no automatic resume', legacy=True)
            except (ValueError, TypeError, AttributeError):
                record = dict(policy_version=VERSION, stop_id='legacy-' + ref['sha256'],
                              stop_code='HARD_STOP_LEGACY_UNCLASSIFIED', stop_class='HARD_STOP',
                              scope='global', target='JPX_HISTORICAL', evidence_refs=[ref],
                              created_at=None, last_success=None, recoverability='HARD_STOP',
                              resume_condition='Human review; no automatic resume', state='ACTIVE', legacy=True)
            self.index['legacy'] = ref
            self.index['active'].append(dict(reference=ref, record=record))
            self.save()
        for item in self.index['active']:
            if item['record'].get('stop_code') == 'HARD_STOP_LEGACY_UNCLASSIFIED':
                # A damaged legacy sidecar is itself retained STOP evidence.
                raw = Path(item['reference']['path'])
                if not raw.is_file() or core.sha256(raw) != item['reference']['sha256']:
                    raise ValueError('STOP_EVIDENCE_HASH_CONFLICT')
            else:
                raw = verify_reference(item['reference'])
            if not item['record'].get('legacy'):
                try:
                    if core.read_frozen(raw) != item['record']:
                        raise ValueError('STOP_INDEX_RECORD_CONFLICT')
                except (OSError, ValueError) as error:
                    raise ValueError('STOP_EVIDENCE_HASH_CONFLICT') from error
            if item['record']['scope'] not in ('ticker', 'session', 'host', 'global'):
                raise ValueError('STOP_SCOPE_INVALID')
    def _reviewed_legacy_references(self):
        from .legacy_stop_review import verified_releases
        return verified_releases(self)

    def save(self):
        write_current(self.path, self.index)

    def applicable(self, session):
        reviewed = self._reviewed_legacy_references()
        return [item for item in self.index['active'] if item['reference'] not in reviewed
                and (item['record']['scope'] in ('host', 'global') or item['record']['target'] == session)]

    def gate(self, session, resolution=None, *, network=True):
        active = self.applicable(session)
        hard = [item for item in active if item['record']['stop_class'] != 'SOFT_STOP'
                and (network or item['record']['scope'] != 'host')]
        if hard:
            raise ValueError('HARD_STOP_ACTIVE:' + hard[0]['record']['stop_code'])
        active = [item for item in active if item['record']['stop_class'] == 'SOFT_STOP']
        if active and resolution is None:
            raise ValueError('SOFT_STOP_RESOLUTION_REQUIRED:' + active[0]['record']['stop_code'])
        if len(active) > 1:
            raise ValueError('MULTIPLE_STOP_RESOLUTIONS_REQUIRED')
        return active[0] if active else None

    def stop(self, reason, session, *, evidence_refs, last_success, run_id, retry_count,
             previous_stop_reference=None, evidence=None):
        kind, scope, target = classification(reason, session)
        record = dict(policy_version=VERSION, stop_id=uuid4().hex, stop_code=reason.split(':', 1)[0],
                      reason=reason, stop_class=kind, scope=scope, target=target, session=session,
                      evidence_refs=evidence_refs, created_at=stamp(), last_success=last_success,
                      recoverability=kind, state='ACTIVE', run_id=run_id, retry_count=retry_count,
                      resume_condition='Root cause/fix/fixture/regression PASS; scope isolated; evidence revalidation PASS'
                      if kind == 'SOFT_STOP' else 'Human review or independently verified external change; no automatic resume')
        if previous_stop_reference:
            record['previous_stop_reference'] = previous_stop_reference
        if evidence is not None:
            record['evidence'] = json.loads(core.canonical_bytes(evidence))
        folder = self.store / 'stops_v2'
        folder.mkdir(exist_ok=True)
        path = self.store / 'STOP.json' if not (self.store / 'STOP.json').exists() else folder / (record['stop_id'] + '.json')
        ref = dict(path=str(path), sha256=hashlib.sha256(core.canonical_bytes(record)).hexdigest())
        self.index['active'].append(dict(reference=ref, record=record))
        if path.name == 'STOP.json':
            self.index['legacy'] = ref
        self.save()  # A crash before seal leaves a missing reference, hence fail closed.
        seal(path, record)
        return ref

    def check_resolution(self, item, path, root):
        """Bind PASS reports and current code to the unchanged target evidence.

        A PASS claim alone cannot release a stop: acquisition replays its evidence
        validators offline before resume() is called.
        """
        resolution_ref = reference(path)
        record = read_reference(resolution_ref)
        stop = item['record']
        if (record.get('stop_reference') != item['reference'] or record.get('scope') != stop['scope']
                or record.get('target') != stop['target'] or not record.get('root_cause')
                or record.get('scope_isolation') != 'PASS'):
            raise ValueError('RESOLUTION_SCOPE_OR_CAUSE_INVALID')
        fixes = record.get('fix_refs', [])
        if not fixes:
            raise ValueError('RESOLUTION_FIX_REQUIRED')
        for ref in fixes:
            file = Path(ref['path']).resolve()
            relative = file.relative_to(Path(root).resolve()) if file.is_relative_to(Path(root).resolve()) else None
            if (not file.is_relative_to(Path(root).resolve()) or file.parts[-1] == 'STOP.json'
                    or relative.parts[0] not in ('providers', 'config', 'engine')
                    or file.suffix not in ('.py', '.json')
                    or file.is_relative_to(self.store) or not file.is_file() or core.sha256(file) != ref['sha256']):
                raise ValueError('RESOLUTION_CURRENT_FIX_HASH_INVALID')
        reports = record.get('validation_refs', [])
        if not reports:
            raise ValueError('RESOLUTION_VALIDATION_REQUIRED')
        checks = {}
        for ref in reports:
            report = read_reference(ref)
            if report.get('status') != 'PASS' or report.get('fix_refs') != fixes:
                raise ValueError('RESOLUTION_VALIDATION_FAILED')
            checks.update(report.get('checks', {}))
        if any(checks.get(check) != 'PASS' for check in ('fixture', 'regression')):
            raise ValueError('RESOLUTION_REQUIRED_TESTS_FAILED')
        if record.get('evidence_refs') != stop['evidence_refs']:
            raise ValueError('RESOLUTION_EVIDENCE_MISMATCH')
        for ref in stop['evidence_refs']:
            read_reference(ref)
        return resolution_ref

    def resume(self, item, resolution_ref, *, evidence_validation, run_id, retry_count, attempt):
        if item['record']['stop_class'] != 'SOFT_STOP' or evidence_validation != 'PASS':
            raise ValueError('AUTOMATIC_RESUME_FORBIDDEN')
        self.gate(item['record']['session'] if 'session' in item['record'] else item['record']['target'], resolution_ref)
        path = self.store / 'stops_v2' / ('resume-' + uuid4().hex + '.json')
        path.parent.mkdir(exist_ok=True)
        event = dict(policy_version=VERSION, state='RESUMED', previous_stop_reference=item['reference'],
                     resolution_reference=resolution_ref, resumed_at=stamp(),
                     scope=item['record']['scope'], target=item['record']['target'],
                     evidence_validation='PASS', run_id=run_id, retry_count=retry_count,
                     attempt=str(attempt), result_reference=str(Path(attempt) / 'result.json'))
        seal(path, event)
        ref = reference(path)
        self.index['resumed'].append(dict(previous_stop_reference=item['reference'],
            resolution_reference=resolution_ref, resume_event_reference=ref,
            scope=item['record']['scope'], target=item['record']['target']))
        self.index['active'].remove(item)
        self.save()
        return ref

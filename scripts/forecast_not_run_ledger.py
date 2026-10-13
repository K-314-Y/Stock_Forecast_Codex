"""Standard-library-only permanent Forecast exclusions; no execution imports.

Each session is a create-once, hash-verified envelope. Directory-relative opens,
no-follow traversal and one flock serialize writers/readers. Complete files are
published by an exclusive hard link and fsynced before returning. Orphan staging
files never count as exclusions. Static exclusions and this ledger form a union.
"""
from contextlib import contextmanager
from datetime import date, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

SCHEMA = 'forecast-permanent-not-run-v1'
DIRECTORY = ('state', 'forecast_not_run_sessions')
WITNESS = '.forecast_not_run_initialized.json'


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode()


def strict_json(body):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('NOT_RUN_JSON_DUPLICATE_KEY')
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError('NOT_RUN_JSON_NONFINITE')
    return json.loads(body, object_pairs_hook=pairs, parse_constant=nonfinite)


def _session(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('NOT_RUN_SESSION_INVALID')
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError('NOT_RUN_SESSION_INVALID')
    return value


def _time(value):
    if not isinstance(value, str):
        raise ValueError('NOT_RUN_TIME_INVALID')
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('NOT_RUN_TIMEZONE_REQUIRED')
    return stamp


@contextmanager
def _directory(root, *, create=False):
    root = Path(root).absolute()
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError('NOT_RUN_PATH_SYMLINK')
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fd = os.dup(root_fd)
    try:
        try:
            os.stat(WITNESS, dir_fd=root_fd, follow_symlinks=False)
            initialized = True
        except FileNotFoundError:
            initialized = False
        for name in DIRECTORY:
            if create and not initialized:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            try:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if create or initialized:
                    raise ValueError('NOT_RUN_INITIALIZED_DIRECTORY_MISSING')
                yield None
                return
            os.close(fd)
            fd = child
        with _witness(root_fd, fd, create=create) as fresh:
            yield fd, fresh
    finally:
        os.close(fd)
        os.close(root_fd)


@contextmanager
def _witness(root_fd, ledger_fd, *, create):
    """Independent root witness detects loss of the entire state directory."""
    expected = dict(schema_version=SCHEMA, directory=list(DIRECTORY))
    fresh = False
    try:
        lock = os.open(WITNESS, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
    except FileNotFoundError:
        if not create:
            raise ValueError('NOT_RUN_INITIALIZATION_WITNESS_MISSING')
        if os.listdir(ledger_fd):
            # Another initializer may have completed after the failed open.
            # A nonempty ledger never authorizes creation of a new witness.
            lock = os.open(WITNESS, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
        else:
            lock = _new_witness(root_fd, expected)
            fresh = lock is not None
            if lock is None:
                lock = os.open(WITNESS, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
    try:
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('NOT_RUN_INITIALIZATION_WITNESS_INVALID')
        fcntl.flock(lock, fcntl.LOCK_EX if create else fcntl.LOCK_SH)
        info = os.fstat(lock)
        if info.st_nlink != 1:
            raise ValueError('NOT_RUN_INITIALIZATION_WITNESS_INVALID')
        if _read_envelope(root_fd,WITNESS) != expected:
            raise ValueError('NOT_RUN_INITIALIZATION_WITNESS_CONFLICT')
        current=os.stat(WITNESS,dir_fd=root_fd,follow_symlinks=False)
        if (info.st_dev,info.st_ino) != (current.st_dev,current.st_ino):
            raise ValueError('NOT_RUN_INITIALIZATION_WITNESS_REPLACED')
        yield fresh
    finally:
        os.close(lock)


def _new_witness(root_fd, expected):
    """Publish complete, already-locked witness bytes; concurrent callers wait."""
    pending='.pending-not-run-witness-'+uuid4().hex
    fd=os.open(pending,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode=0o600,dir_fd=root_fd)
    published=False
    try:
        try:
            fcntl.flock(fd,fcntl.LOCK_EX)
            envelope=dict(record=expected,sha256=hashlib.sha256(canonical(expected)).hexdigest())
            body=canonical(envelope)
            if os.write(fd,body) != len(body):
                raise ValueError('NOT_RUN_INITIALIZATION_PARTIAL_WRITE')
            os.fsync(fd)
            try:
                os.link(pending,WITNESS,src_dir_fd=root_fd,dst_dir_fd=root_fd,follow_symlinks=False)
                published=True
                os.fsync(root_fd)
            except FileExistsError:
                pass
        finally:
            os.unlink(pending,dir_fd=root_fd)
            os.fsync(root_fd)
    except BaseException:
        os.close(fd)
        raise
    if published:
        return fd
    os.close(fd)
    return None


@contextmanager
def _lock(fd, *, write=False, initialize=False):
    flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if initialize else 0)
    lock = os.open('.lock', flags, mode=0o600, dir_fd=fd)
    try:
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('NOT_RUN_LOCK_INVALID')
        fcntl.flock(lock, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
        marker = os.pread(lock, 128, 0)
        if write and initialize and not marker:
            # Initialization can fail closed, but must never reinterpret a lost
            # inventory as an empty ledger after the durable marker exists.
            if '.inventory.json' not in os.listdir(fd):
                if set(os.listdir(fd)) - {'.lock'}:
                    raise ValueError('NOT_RUN_INVENTORY_MISSING')
                _inventory(fd, {})
            os.write(lock, (SCHEMA + '\n').encode())
            os.fsync(lock)
        elif marker != (SCHEMA + '\n').encode():
            raise ValueError('NOT_RUN_LOCK_MARKER_INVALID')
        if write:
            os.fsync(lock)
            os.fsync(fd)
        yield
    finally:
        os.close(lock)


def _read_envelope(fd, name):
    entry_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(entry_fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 1024 * 1024:
            raise ValueError('NOT_RUN_ENTRY_INVALID')
        body = stream.read(1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
    current = os.stat(name, dir_fd=fd, follow_symlinks=False)
    identity = lambda item: (item.st_dev, item.st_ino, item.st_mode, item.st_size,
                             item.st_mtime_ns, item.st_ctime_ns)
    if identity(before) != identity(after) or identity(before) != identity(current):
        raise ValueError('NOT_RUN_CONCURRENT_CHANGE')
    envelope = strict_json(body)
    if (not isinstance(envelope, dict) or set(envelope) != {'record', 'sha256'}
            or hashlib.sha256(canonical(envelope['record'])).hexdigest() != envelope['sha256']):
        raise ValueError('NOT_RUN_HASH_CONFLICT')
    return envelope['record']


def _inventory(fd, records):
    value = dict(schema_version=SCHEMA, records=records)
    envelope = dict(record=value, sha256=hashlib.sha256(canonical(value)).hexdigest())
    pending = '.pending-not-run-' + uuid4().hex
    pending_fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         mode=0o600, dir_fd=fd)
    try:
        with os.fdopen(pending_fd, 'wb') as stream:
            stream.write(canonical(envelope))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            info = os.stat('.inventory.json', dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('NOT_RUN_INVENTORY_PATH_INVALID')
        except FileNotFoundError:
            pass
        os.replace(pending, '.inventory.json', src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        if pending in os.listdir(fd):
            os.unlink(pending, dir_fd=fd)
            os.fsync(fd)


def _validate_record(record, session):
    if (not isinstance(record, dict)
            or set(record) != {'schema_version', 'target_session', 'reason', 'source', 'recorded_at'}
            or record['schema_version'] != SCHEMA or record['target_session'] != session
            or any(not isinstance(record[k], str) or not record[k].strip() or len(record[k]) > 1024
                   for k in ('reason', 'source'))):
        raise ValueError('NOT_RUN_RECORD_INVALID')
    _time(record['recorded_at'])


def _records(fd, *, recover=False):
    inventory = _read_envelope(fd, '.inventory.json')
    if (not isinstance(inventory, dict) or set(inventory) != {'schema_version', 'records'}
            or inventory['schema_version'] != SCHEMA or not isinstance(inventory['records'], dict)):
        raise ValueError('NOT_RUN_INVENTORY_INVALID')
    expected = inventory['records']
    for session, value in expected.items():
        _validate_record(value, _session(session))
    result = {}
    for name in sorted(os.listdir(fd)):
        if name in {'.lock', '.inventory.json'}:
            continue
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('NOT_RUN_ENTRY_PATH_INVALID')
        if re.fullmatch(r'\.pending-not-run-[0-9a-f]{32}', name):
            continue
        if not name.endswith('.json'):
            raise ValueError('NOT_RUN_ENTRY_NAME_INVALID')
        session = _session(name[:-5])
        record = _read_envelope(fd, name)
        _validate_record(record, session)
        if record != expected.get(session):
            raise ValueError('NOT_RUN_INVENTORY_CONFLICT')
        result[session] = record
    if result != expected:
        if not recover:
            raise ValueError('NOT_RUN_PUBLISHED_RECORD_MISSING')
        # A durable intent precedes publication. Writer recovery restores exact
        # bytes after a crash; readers close until restoration completes.
        for session in sorted(set(expected) - set(result)):
            _publish(fd, session, expected[session])
        return _records(fd)
    return result


def _publish(fd, session, value):
    envelope = dict(record=value, sha256=hashlib.sha256(canonical(value)).hexdigest())
    pending = '.pending-not-run-' + uuid4().hex
    pending_fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         mode=0o600, dir_fd=fd)
    try:
        with os.fdopen(pending_fd, 'wb') as stream:
            stream.write(canonical(envelope))
            stream.flush()
            os.fsync(stream.fileno())
        os.link(pending, session + '.json', src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        os.fsync(fd)
    finally:
        os.unlink(pending, dir_fd=fd)
        os.fsync(fd)


def static_sessions(policy):
    values = policy['permanently_not_run_sessions']
    if not isinstance(values, list) or values != sorted(set(_session(v) for v in values)):
        raise ValueError('NOT_RUN_STATIC_POLICY_INVALID')
    return set(values)


def sessions(root, policy):
    """The authoritative static/runtime union; any ledger damage closes consumers."""
    values = static_sessions(policy)
    try:
        with _directory(root) as handle:
            if handle is None:
                return values
            fd, _ = handle
            with _lock(fd):
                return values | set(_records(fd))
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError('NOT_RUN_LEDGER_INVALID') from error


def record(root, target_session, *, reason, source, recorded_at):
    """Later equivalent repeats retain original bytes/time; conflicts never overwrite."""
    _session(target_session)
    stamp = _time(recorded_at)
    if any(not isinstance(v, str) or not v.strip() or len(v) > 1024 for v in (reason, source)):
        raise ValueError('NOT_RUN_RECORD_INVALID')
    value = dict(schema_version=SCHEMA, target_session=target_session,
                 reason=reason, source=source, recorded_at=recorded_at)
    try:
        with _directory(root, create=True) as (fd, fresh), _lock(fd, write=True, initialize=fresh):
            records = _records(fd, recover=True)
            old = records.get(target_session)
            if old:
                if old['reason'] != reason or old['source'] != source:
                    raise ValueError('NOT_RUN_CONFLICTING_DUPLICATE')
                if stamp < _time(old['recorded_at']):
                    raise ValueError('NOT_RUN_CLOCK_ROLLBACK')
                return old
            if any(stamp < _time(item['recorded_at']) for item in records.values()):
                raise ValueError('NOT_RUN_CLOCK_ROLLBACK')
            _inventory(fd, {**records, target_session: value})
            _publish(fd, target_session, value)
            if _records(fd).get(target_session) != value:
                raise ValueError('NOT_RUN_PUBLICATION_CONFLICT')
            return value
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError('NOT_RUN_LEDGER_WRITE_FAILED:' + str(error)) from error

"""Durable create-only bundles and atomic file replacement; no model semantics."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from engine import minimal_trial as core


def code_commit():
    if subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--', 'engine', 'models', 'config',
                       'scripts/forecast_not_run_ledger.py'],
                      cwd=core.ROOT, check=False).returncode != 0:
        raise ValueError('DIRTY_EXECUTION_CODE')
    value = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=core.ROOT, text=True).strip()
    if len(value) != 40 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('CODE_COMMIT_REQUIRED')
    return value


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def locked(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.lock').open('a+b') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield directory
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def seal(path, record):
    import hashlib
    payload = core.canonical_bytes(record)
    for dest, data in ((Path(path), payload), (Path(path).with_suffix('.sha256'),
                       (hashlib.sha256(payload).hexdigest() + '\n').encode('ascii'))):
        with dest.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())


def commit(destination, builder, confirm=None):
    """Caller holds parent lock. Orphan .pending-* dirs are never final artifacts."""
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError('FORWARD_CREATE_ONLY:' + str(destination))
    stage = Path(tempfile.mkdtemp(prefix='.pending-', dir=destination.parent))
    try:
        builder(stage)
        for file in stage.iterdir():
            with file.open('rb') as stream:
                os.fsync(stream.fileno())
        sync_dir(stage)
        if confirm is not None:
            confirm(stage)
        if destination.exists():
            raise FileExistsError('FORWARD_CREATE_ONLY')
        os.rename(stage, destination)
        sync_dir(destination.parent)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def replace_json(path, record):
    """Durable replacement for runtime projections, never frozen artifacts."""
    path = Path(path)
    fd, pending = tempfile.mkstemp(prefix='.pending-runtime-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(core.canonical_bytes(record))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(pending):
            os.unlink(pending)


@contextmanager
def secure_directory(path, *, create=False, lock=False):
    """Pin each directory with no-follow opens; new history writers share a lock."""
    import stat
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ValueError('FORWARD_PATH_TRAVERSAL')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock_fd = None
    try:
        for name in path.parts[1:]:
            if create:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        if lock:
            lock_fd = os.open('.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                              mode=0o600, dir_fd=fd)
            info = os.fstat(lock_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('FORWARD_LOCK_INVALID')
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            current = os.stat('.lock', dir_fd=fd, follow_symlinks=False)
            if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                raise ValueError('FORWARD_LOCK_REPLACED')
        yield fd
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(fd)


def read_at(directory_fd, name):
    """Read a regular single-link file relative to a pinned directory."""
    import stat
    if Path(name).name != name or name in ('.', '..'):
        raise ValueError('FORWARD_PATH_TRAVERSAL')
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('FORWARD_FILE_INVALID')
        return stream.read()


def write_envelope_at(directory_fd, name, record, *, replace=False, confirm=None):
    """One-file hash envelope, exclusive publication or atomic projection update.

    Caller holds secure_directory(lock=True). No partial file is ever published.
    An fsync failure raises even if publication occurred; retry verifies bytes.
    """
    import hashlib
    from uuid import uuid4
    if Path(name).name != name or name in ('.', '..'):
        raise ValueError('FORWARD_PATH_TRAVERSAL')
    envelope = dict(record=record, sha256=hashlib.sha256(core.canonical_bytes(record)).hexdigest())
    body = core.canonical_bytes(envelope)
    try:
        existing = read_at(directory_fd, name)
    except FileNotFoundError:
        existing = None
    if existing == body:
        os.fsync(directory_fd)
        return envelope['sha256']
    if existing is not None and not replace:
        raise ValueError('FORWARD_IMMUTABLE_CONFLICT')
    pending = '.pending-history-' + uuid4().hex
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 mode=0o600, dir_fd=directory_fd)
    try:
        with os.fdopen(fd, 'wb') as stream:
            if stream.write(body) != len(body):
                raise ValueError('FORWARD_PARTIAL_WRITE')
            stream.flush()
            os.fsync(stream.fileno())
        if read_at(directory_fd, pending) != body:
            raise ValueError('FORWARD_READBACK_MISMATCH')
        if confirm is not None:
            confirm()
        if replace:
            # Refuse symlink/nonregular destinations, including concurrent swaps.
            try:
                read_at(directory_fd, name)
            except FileNotFoundError:
                pass
            os.replace(pending, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        else:
            os.link(pending, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                    follow_symlinks=False)
            os.unlink(pending, dir_fd=directory_fd)
        os.fsync(directory_fd)
        if read_at(directory_fd, name) != body:
            raise ValueError('FORWARD_READBACK_MISMATCH')
        return envelope['sha256']
    finally:
        try:
            os.unlink(pending, dir_fd=directory_fd)
        except FileNotFoundError:
            pass

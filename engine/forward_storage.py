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
    if subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--', 'engine', 'models', 'config'],
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


def commit(destination, builder, confirm):
    """Caller holds parent lock. Orphan .pending-* dirs are never final artifacts."""
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError('FORWARD_CREATE_ONLY:' + str(destination))
    stage = Path(tempfile.mkdtemp(prefix='.pending-', dir=destination.parent))
    try:
        builder(stage)
        sync_dir(stage)
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

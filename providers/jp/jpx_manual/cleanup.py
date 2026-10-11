"""Fail-closed retention review. CLI is dry-run unless explicitly enabled.

No permanent-delete implementation or fallback exists. Only raw PDFs may be
passed to the Windows recycle backend; supporting artifacts are read-only.
"""
import argparse
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import uuid

JST = timezone(timedelta(hours=9))
CORE = {'6857', '7203', '8306', '1605', '9433', '3197'}
ROOT = Path(__file__).resolve().parents[3]
INBOX = Path('data/outcome_input/manual_jpx')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def timestamp(value):
    t = datetime.fromisoformat(value)
    if t.tzinfo is None:
        raise ValueError('Timezone required')
    return t


def trash_date(downloaded_at):
    return (timestamp(downloaded_at).astimezone(JST).date() + timedelta(days=30)).isoformat()


def bounded(root, relative):
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts:
        raise ValueError('UNSAFE_PATH')
    base = root.resolve()
    p = base
    for part in rel.parts:
        p = p / part
        if p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()):
            raise ValueError('REPARSE_PATH')
    if not p.resolve().is_relative_to(base):
        raise ValueError('OUTSIDE_WORKSPACE')
    return p


def evidence(root, e, kind):
    p = bounded(root, e[kind + '_path'])
    if p.resolve().is_relative_to((root / INBOX).resolve()):
        raise ValueError('EVIDENCE_MUST_BE_OUTSIDE_RAW_INBOX')
    if sha(p) != e[kind + '_sha256']:
        raise ValueError('EVIDENCE_HASH_MISMATCH')
    return load(p)


def eligible(root, e, now):
    reasons = []
    try:
        if now.tzinfo is None:
            raise ValueError('UNKNOWN_NOW_TIMEZONE')
        if e.get('retention_class') != 'DAILY' or e.get('acquisition_type') not in ('NORMAL', 'BACKFILL'):
            reasons.append('PROTECTED_CLASS')
        for key in ('data_conflict', 'parser_error', 'manual_review', 'scoring_discrepancy', 'schema_migration', 'user_keep'):
            if e.get(key) is not False:
                reasons.append(key.upper())
        if e.get('corporate_action_flag') not in ('NONE_CONFIRMED', 'RESOLVED'):
            reasons.append('UNRESOLVED_CORPORATE_ACTION')
        if e.get('parser_status') != 'PASS' or e.get('core6_count') != 6 or e.get('schema_validation') != 'PASS':
            reasons.append('PARSER_OR_SCHEMA_NOT_PASS')
        if e.get('scoring_status') != 'SCORED' or not e.get('scored_at'):
            reasons.append('SCORING_INCOMPLETE')
        if e.get('trash_moved_at') or e.get('trash_state') not in (None, 'PRESENT'):
            reasons.append('TRASH_STATE_REQUIRES_REVIEW')
        if not e.get('downloaded_at'):
            reasons.append('UNKNOWN_DOWNLOAD_TIME')
        else:
            due = trash_date(e['downloaded_at'])
            if e.get('trash_after') != due or now.astimezone(JST).date() < date.fromisoformat(due):
                reasons.append('RETENTION_NOT_DUE_OR_DATE_CONFLICT')
        if reasons:
            return False, reasons
        downloaded = timestamp(e['downloaded_at'])
        parsed = timestamp(e['parsed_at'])
        scored = timestamp(e['scored_at'])
        if not downloaded <= parsed <= scored <= now:
            raise ValueError('TIMESTAMP_ORDER')
        if e['session_date'] != e['expected_session_date']:
            raise ValueError('SESSION_MISMATCH')
        raw = bounded(root, str(INBOX / e['filename']))
        if Path(e['filename']).name != e['filename'] or raw.suffix.lower() != '.pdf':
            raise ValueError('RAW_FILENAME_INVALID')
        if sha(raw) != e['sha256']:
            raise ValueError('RAW_HASH_MISMATCH')
        normalized = evidence(root, e, 'normalized_output')
        forecasts = evidence(root, e, 'forecast_record')['records']
        scores = evidence(root, e, 'scoring_record')['records']
        rows = normalized['records']
        from jsonschema import Draft202012Validator, FormatChecker
        for batch, schema_name in ((forecasts, 'minimal_forecast_schema.json'), (scores, 'trial_score_schema.json')):
            validator = Draft202012Validator(load(root / 'config' / schema_name), format_checker=FormatChecker())
            for record in batch:
                if list(validator.iter_errors(record)):
                    raise ValueError('EVIDENCE_SCHEMA_FAILURE')
                if record.get('record_kind') != 'TRIAL':
                    raise ValueError('SYNTHETIC_RECORD_NOT_RETENTION_EVIDENCE')
        if normalized['source_file_hash'] != e['sha256'] or normalized['normalized_schema_validation'] != 'PASS':
            raise ValueError('NORMALIZED_PROVENANCE')
        for batch in (rows, forecasts, scores):
            if len(batch) != 6 or {r['ticker'] for r in batch} != CORE:
                raise ValueError('CORE6_NOT_UNIQUE')
            if any(r['session_date'] != e['session_date'] for r in batch):
                raise ValueError('EVIDENCE_SESSION_MISMATCH')
        f = {r['ticker']: r for r in forecasts}
        n = {r['ticker']: r for r in rows}
        import math
        for row in rows:
            values = [row[k] for k in ('open', 'high', 'low', 'close', 'volume')]
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
                raise ValueError('NORMALIZED_OHLCV_INVALID')
            if not (0 < row['low'] <= row['open'] <= row['high'] and row['low'] <= row['close'] <= row['high'] and row['volume'] > 0):
                raise ValueError('NORMALIZED_OHLCV_CONFLICT')
        ids = set(e['forecast_ids'])
        if len(ids) != 6 or ids != {r['forecast_id'] for r in forecasts}:
            raise ValueError('FORECAST_LINKS_MISSING')
        for s in scores:
            forecast = f[s['ticker']]
            if s['status'] != 'SCORED' or s['forecast_id'] != forecast['forecast_id'] or s['forecast_version'] != forecast['forecast_version']:
                raise ValueError('SCORE_LINK_MISMATCH')
            if s['actual_open'] != n[s['ticker']]['open'] or s['actual_close'] != n[s['ticker']]['close']:
                raise ValueError('SCORING_DISCREPANCY')
            if not s.get('ai_score') or s['ai_score'].get('brier') is None:
                raise ValueError('SCORE_RESULT_MISSING')
        return True, []
    except (KeyError, TypeError, ValueError, OSError, ImportError) as exc:
        return False, ['REVIEW_REQUIRED: ' + str(exc)]


@contextmanager
def manifest_lock(path):
    # A persistent lock file avoids deletion and protects concurrent workers.
    with path.open('a+b') as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def append_event(path, value):
    with path.open('a', encoding='utf-8') as h:
        h.write(json.dumps(value, ensure_ascii=False) + '\n')
        h.flush()
        os.fsync(h.fileno())


def windows_recycle(path):
    """Windows 8+ IFileOperation, reject any non-recycle operation in callback.

    Requires pywin32. No legacy API or direct deletion fallback.
    Native integration is intentionally disabled pending a separate fake-file test.
    """
    if os.name != 'nt':
        raise RuntimeError('WINDOWS_ONLY')
    import ctypes
    import sys
    if sys.getwindowsversion().major < 6 or (sys.getwindowsversion().major == 6 and sys.getwindowsversion().minor < 2):
        raise RuntimeError('WINDOWS_8_OR_NEWER_REQUIRED')
    if ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) != 3:
        raise RuntimeError('FIXED_LOCAL_DRIVE_REQUIRED')
    import pythoncom
    from win32com.shell import shell, shellcon
    from win32com.server.exception import COMException
    from win32com.server.policy import DesignatedWrapPolicy

    class Sink(DesignatedWrapPolicy):
        _com_interfaces_ = [shell.IID_IFileOperationProgressSink]
        _public_methods_ = ['StartOperations', 'FinishOperations', 'PreRenameItem', 'PostRenameItem',
                            'PreMoveItem', 'PostMoveItem', 'PreCopyItem', 'PostCopyItem',
                            'PreDeleteItem', 'PostDeleteItem', 'PreNewItem', 'PostNewItem',
                            'UpdateProgress', 'ResetTimer', 'PauseTimer', 'ResumeTimer']

        def __init__(self):
            self._wrap_(self)
            self.recycled = False

        def PreDeleteItem(self, flags, item):
            if not flags & shellcon.TSF_DELETE_RECYCLE_IF_POSSIBLE:
                raise COMException('Permanent deletion prohibited', scode=-2147467260)
            return 0

        def PostDeleteItem(self, flags, item, result, new_item):
            if result != 0 or not new_item:
                raise COMException('Recycle not confirmed', scode=-2147467260)
            self.recycled = True
            return 0

    for name in Sink._public_methods_:
        if not hasattr(Sink, name):
            setattr(Sink, name, lambda self, *args: 0)
    pythoncom.CoInitialize()
    try:
        op = pythoncom.CoCreateInstance(shell.CLSID_FileOperation, None, pythoncom.CLSCTX_ALL, shell.IID_IFileOperation)
        op.SetOperationFlags(shellcon.FOF_NOERRORUI | shellcon.FOF_SILENT | shellcon.FOFX_EARLYFAILURE | 0x20000000 | 0x00080000)
        sink = Sink()
        op.DeleteItem(shell.SHCreateItemFromParsingName(str(path), None, shell.IID_IShellItem),
                      pythoncom.WrapObject(sink, shell.IID_IFileOperationProgressSink))
        result = op.PerformOperations()
        if result or op.GetAnyOperationsAborted() or not sink.recycled:
            raise RuntimeError('RECYCLE_NOT_CONFIRMED')
    finally:
        pythoncom.CoUninitialize()


def apply_one(root, manifest_path, filename, policy, now, recycle=windows_recycle):
    if policy.get('cleanup_enabled') is not True or policy.get('windows_trash_backend_validated') is not True:
        raise ValueError('CLEANUP_DISABLED')
    with manifest_lock(manifest_path.with_suffix('.lock')):
        manifest = load(manifest_path)
        found = [e for e in manifest['entries'] if e['filename'] == filename]
        if len(found) != 1:
            raise ValueError('MANIFEST_ENTRY_NOT_UNIQUE')
        e = found[0]
        ok, reasons = eligible(root, e, now)
        if not ok:
            raise ValueError('; '.join(reasons))
        journal = manifest_path.with_suffix('.cleanup.jsonl')
        if journal.exists():
            events = [json.loads(line) for line in journal.read_text(encoding='utf-8').splitlines()]
            started = {r['operation_id'] for r in events if r['filename'] == filename and r['event'] == 'PREPARE'}
            completed = {r['operation_id'] for r in events if r['event'] == 'MOVED'}
            if started - completed:
                raise ValueError('REVIEW_REQUIRED: unfinished prior operation')
        operation = str(uuid.uuid4())
        snapshot = manifest_path.with_name(manifest_path.stem + '.' + operation + '.before.json')
        with snapshot.open('x', encoding='utf-8') as h:
            json.dump(manifest, h, ensure_ascii=False, indent=2)
        event = dict(operation_id=operation, filename=filename, sha256=e['sha256'], at=now.isoformat())
        append_event(journal, dict(event, event='PREPARE'))
        try:
            # Re-read all evidence immediately before touching the raw PDF.
            ok, reasons = eligible(root, e, now)
            if not ok:
                raise ValueError(str(reasons))
            raw = bounded(root, str(INBOX / filename))
            recycle(raw)
            if raw.exists():
                raise RuntimeError('RAW_STILL_PRESENT')
            e.update(trash_moved_at=now.isoformat(), trash_state='MOVED_TO_OS_TRASH', eligible_for_trash=False)
            temp = manifest_path.with_name(manifest_path.name + '.' + operation + '.tmp')
            with temp.open('x', encoding='utf-8') as h:
                json.dump(manifest, h, ensure_ascii=False, indent=2)
                h.flush()
                os.fsync(h.fileno())
            os.replace(temp, manifest_path)
            append_event(journal, dict(event, event='MOVED'))
        except Exception:
            append_event(journal, dict(event, event='REVIEW_REQUIRED'))
            raise


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument('--apply', action='store_true')
    cli.add_argument('--filename')
    args = cli.parse_args()
    manifest_path = ROOT / INBOX / 'retention_manifest.json'
    now = datetime.now(timezone.utc)
    policy = load(ROOT / 'config/raw_data_retention_policy.json')
    if args.apply:
        if not args.filename:
            cli.error('--filename required')
        apply_one(ROOT, manifest_path, args.filename, policy, now)
    else:
        for e in load(manifest_path)['entries']:
            ok, reasons = eligible(ROOT, e, now)
            print(json.dumps({'filename': e['filename'], 'eligible_for_trash': ok, 'reasons': reasons}))


if __name__ == '__main__':
    main()

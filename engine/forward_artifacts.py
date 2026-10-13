"""Shared read-only artifact validation. No Probe or Forward model semantics."""
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator, FormatChecker
from engine import minimal_trial as core
from engine.forward_storage import locked as _locked, seal as _seal, commit as _commit, sync_dir as _sync_dir

JST = ZoneInfo('Asia/Tokyo')


def strict_json(body):
    """Reject ambiguous JSON before hash-bound evidence is interpreted."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('JSON_DUPLICATE_KEY')
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError('JSON_NONFINITE')
    return json.loads(body, object_pairs_hook=pairs, parse_constant=nonfinite)


def _stamp(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError('NAIVE_PROBE_TIME')
    return stamp.astimezone(JST)



def _load(path, hashes):
    path = Path(path).resolve()
    payload = path.read_bytes()
    hashes[str(path)] = hashlib.sha256(payload).hexdigest()
    return json.loads(payload)



def _unchanged(hashes):
    if any(core.sha256(path) != expected for path, expected in hashes.items()):
        raise ValueError('INPUT_CHANGED_BEFORE_FREEZE')



def _validated_jpx(parsed_path, target_session, now):
    parsed_hash = core.sha256(parsed_path)
    parsed = core.load_parsed_jpx(parsed_path)  # Includes the existing Linux raw resolver.
    manifest_path = core.ROOT / 'data/outcome_input/manual_jpx/retention_manifest.json'
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    entries = [entry for entry in manifest['entries']
               if entry.get('session_date') == target_session
               and entry.get('sha256') == parsed['source_file_hash']
               and entry.get('normalized_output_sha256') == parsed_hash]
    if (len(entries) != 1 or entries[0].get('parser_status') != 'PASS'
            or entries[0].get('schema_validation') != 'PASS'
            or entries[0].get('core6_count') != 6
            or parsed.get('source_file_hash_after') != parsed['source_file_hash']
            or parsed.get('manual_file_integrity') != 'PASS'
            or parsed.get('duplicate_count') != 0
            or parsed.get('session_date') != target_session
            or parsed.get('normalized_schema_validation') != 'PASS'
            or parsed.get('core6_extraction') != '6/6'):
        raise ValueError('JPX_MANIFEST_OR_VALIDATION_MISMATCH')
    close = _stamp(target_session + 'T15:30:00+09:00')
    if not close <= _stamp(parsed['retrieved_at']) <= now or _stamp(entries[0]['parsed_at']) > now:
        raise ValueError('JPX_OUTCOME_AVAILABILITY_INVALID')
    records = parsed['records']
    if len(records) != 6 or {r['ticker'] for r in records} != set(core.CORE):
        raise ValueError('JPX_CORE6_MISMATCH')
    schema = json.loads((core.ROOT / 'config/normalized_market_schema.json').read_text(encoding='utf-8-sig'))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    for row in records:
        validator.validate(row['normalized_observation'])
        if (row['session_date'] != target_session
                or row['source_file_hash'] != parsed['source_file_hash']
                or row['series_type'] != 'RAW_OHLC'
                or row['normalized_observation']['symbol'] != row['ticker']
                or row['normalized_observation']['exchange'] != 'XTKS'
                or row['normalized_observation']['currency'] != 'JPY'
                or row['normalized_observation']['series_type'] != 'RAW_OHLC'
                or row['normalized_observation']['normalization']['basis_session'] != row['session_date']
                or 'sha256=' + parsed['source_file_hash'] not in row['normalized_observation']['source_reference']['reference']
                or row['open_semantics'] != 'OFFICIAL_SESSION_OPEN'
                or row['close_semantics'] != 'OFFICIAL_SESSION_CLOSE'):
            raise ValueError('JPX_ROW_PROVENANCE_MISMATCH')
        o, h, l, c = (row[k] for k in ('open', 'high', 'low', 'close'))
        if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (o, h, l, c))
                or not 0 < l <= min(o, c) <= max(o, c) <= h
                or any(row[k] != row['normalized_observation'][k] for k in ('open', 'high', 'low', 'close'))
                or row['normalized_observation']['session_date'] != row['session_date']):
            raise ValueError('JPX_ROW_PRICE_MISMATCH')
    if core.sha256(parsed_path) != parsed_hash:
        raise ValueError('JPX_PARSED_CHANGED_DURING_VALIDATION')
    return parsed, dict(parsed_sha256=parsed_hash, manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                       raw_sha256=parsed['source_file_hash'])

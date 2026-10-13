"""Acquisition component binding; Git identities are immutable audit lineage.

Full provider/config bytes and only the engine helpers reached by acquisition
are bound, including package initializer presence and bytes. The v2 compatibility
mapping admits only this reviewed refactor. Both verifier and mapping stay
byte-identical to the v2 mapping's introduction, descended from the declared base.
"""
import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess

VERSION = 'jpx-acquisition-components-v2'
COMPATIBILITY = 'config/jpx_acquisition_binding_v2.json'
BINDER = 'providers/jp/jpx_manual/acquisition_binding.py'
PACKAGE_INITIALIZERS = ('engine/__init__.py', 'providers/__init__.py',
                        'providers/jp/__init__.py', 'providers/jp/jpx_manual/__init__.py')
FILES = (
    'providers/jp/jpx_manual/historical_direct.py',
    'providers/jp/jpx_manual/historical_policy_v2.py',
    'providers/jp/jpx_manual/legacy_stop_review.py',
    'providers/jp/jpx_manual/parser.py',
    'providers/jp/jpx_manual/acquisition.py',
    'config/jpx_daily_acquisition_config.json',
    'config/normalized_market_schema.json',
    'config/operation_policy_v2.md',
)
NODES = {
    'engine/minimal_trial.py': ('CORE', 'ROOT', 'canonical_bytes', 'sha256', 'read_frozen',
                              '_resolve_jpx_raw_source', 'load_parsed_jpx'),
    'engine/official_gap.py': ('ROOT', 'MISSING_ALLOWLIST', 'EXISTING_SESSIONS', 'GAP_START',
                             'GAP_THROUGH', 'expected_gap_sessions', 'stage_existing_pdf'),
}
HELPERS = ('_locked', '_seal', '_sync_dir')


def git(root, *arguments):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS='0')
    result = subprocess.run(['git', '--no-replace-objects', '-C', str(root), *arguments],
                            capture_output=True, env=env, timeout=20)
    if result.returncode:
        raise ValueError('LEGACY_RENEWAL_CODE_COMMIT_INVALID')
    return result.stdout


def _bytes(root, relative):
    path = root / relative
    if (not path.is_file() or any(p.is_symlink() for p in (path, *path.parents))
            or not path.resolve().is_relative_to(root)):
        raise ValueError('ACQUISITION_COMPONENT_PATH_INVALID:' + relative)
    return path.read_bytes()


def _parts(body, names):
    tree = ast.parse(body)
    lines = body.decode().splitlines(keepends=True)
    values = {}
    context = copy.deepcopy(tree)
    for node in context.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Function bodies outside the call graph are irrelevant. Their
            # headers/decorators/defaults and all module executable statements
            # remain bound: they can rebind globals or alter imported helpers.
            node.body = [ast.Pass()]
    values['module_context'] = ast.unparse(context).encode()
    for node in tree.body:
        key = getattr(node, 'name', None)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            key = node.target.id
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            key = node.targets[0].id
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.asname in names:
                    values[alias.asname] = ast.unparse(ast.ImportFrom(module=node.module,
                        names=[alias], level=node.level)).encode()
        if key in names or isinstance(node, (ast.Import, ast.ImportFrom)):
            start = node.decorator_list[0].lineno if getattr(node, 'decorator_list', None) else node.lineno
            values[key or 'import:' + str(node.lineno)] = ''.join(lines[start-1:node.end_lineno]).encode()
    if not set(names).issubset(values):
        raise ValueError('ACQUISITION_COMPONENT_NODE_MISSING')
    return values


def components(root, *, commit=None, infrastructure=False):
    root = Path(root).resolve()
    def read(relative):
        return git(root, 'show', commit + ':' + relative) if commit else _bytes(root, relative)
    values = {name: read(name) for name in FILES}
    for path in PACKAGE_INITIALIZERS:
        if commit:
            present = bool(git(root, 'ls-tree', commit, '--', path).strip())
        else:
            target = root / path
            if any(parent.is_symlink() for parent in target.parents):
                raise ValueError('ACQUISITION_COMPONENT_PATH_INVALID:' + path)
            try:
                target.lstat()
                present = True
            except FileNotFoundError:
                present = False
        marker = b'jpx-acquisition-package-initializer:v2:'
        values['package:' + path] = marker + (b'present\n' + read(path) if present else b'absent\n')
    for path, names in NODES.items():
        parts = _parts(read(path), names)
        values[path + ':module_context'] = parts['module_context']
        # Imports are part of the projection; line-number changes are irrelevant.
        imports = b'\n'.join(value for key, value in parts.items() if key.startswith('import:'))
        values[path + ':imports'] = imports
        for name in names:
            values[path + ':' + name] = parts[name]
    # Stable logical keys across the mechanical Probe -> library extraction.
    try:
        artifacts = read('engine/forward_artifacts.py')
    except ValueError:
        if not commit:
            raise
        # Missing at the historical commit is the only valid legacy fallback.
        if git(root, 'ls-tree', commit, '--', 'engine/forward_artifacts.py').strip():
            raise
        artifacts = None
    if artifacts is None:
        helpers = _parts(read('engine/one_session_dry_run.py'), HELPERS)
        values['artifact:module_context'] = helpers['module_context']
        for name in HELPERS:
            values['artifact:' + name] = helpers[name]
    else:
        # Acquisition uses only these storage aliases; Forward-only validation
        # and Probe semantics do not invalidate a reviewed acquisition release.
        aliases = _parts(artifacts, HELPERS)
        values['artifact:alias_context'] = aliases['module_context']
        for name in HELPERS:
            values['artifact:alias:' + name] = aliases[name]
        selected = ('locked', 'seal', 'sync_dir')
        parts = _parts(read('engine/forward_storage.py'), selected)
        values['artifact:storage_context'] = parts['module_context']
        values['artifact:storage_imports'] = b'\n'.join(v for k, v in parts.items() if k.startswith('import:'))
        for name in selected:
            values['artifact:storage:' + name] = parts[name]
    if infrastructure:
        values[BINDER] = read(BINDER)
        values[COMPATIBILITY] = read(COMPATIBILITY)
    return {name: hashlib.sha256(body).hexdigest() for name, body in sorted(values.items())}


def fingerprint(values):
    payload = json.dumps(values, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return dict(schema_version=VERSION, components=values, sha256=hashlib.sha256(payload).hexdigest())


def _compatibility(root):
    base = 'f60fbba965ee858ca6e7a3afefbdb4e09655a4f0'
    # Do not permit a dirty or later-edited mapping/verifier to grant an old release.
    # This is a local immutable Git trust checkpoint, not whole-HEAD validity.
    introductions = git(root, 'log', '--diff-filter=A', '--format=%H', 'HEAD', '--', COMPATIBILITY).decode().splitlines()
    if len(introductions) != 1:
        raise ValueError('ACQUISITION_COMPATIBILITY_CHECKPOINT_INVALID')
    introduced = introductions[0]
    git(root, 'merge-base', '--is-ancestor', base, introduced)
    mapping_bytes = None
    # v2 pins both blobs at the v2 mapping's first introduction. The original
    # v1 mapping remains immutable history; no old receipt is rewritten.
    for path in (BINDER, COMPATIBILITY):
        checked = _bytes(root, path)
        if checked != git(root, 'show', introduced + ':' + path):
            raise ValueError('ACQUISITION_COMPATIBILITY_CHANGED')
        if path == COMPATIBILITY:
            mapping_bytes = checked
    # Parse only after the verifier and mapping pass their immutable checkpoint.
    # The parser belongs to this fully bound module, not a projected helper.
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('ACQUISITION_COMPATIBILITY_DUPLICATE_KEY')
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError('ACQUISITION_COMPATIBILITY_NONFINITE')
    value = json.loads(mapping_bytes, object_pairs_hook=pairs, parse_constant=nonfinite)
    if (not isinstance(value, dict)
            or set(value) != {'schema_version', 'base_commit', 'before', 'after'}
            or value['schema_version'] != VERSION or value['base_commit'] != base):
        raise ValueError('ACQUISITION_COMPATIBILITY_INVALID')
    if components(root, commit=base) != value['before']:
        raise ValueError('ACQUISITION_COMPATIBILITY_BASE_CONFLICT')
    return value


def validate(root, receipt):
    root = Path(root).resolve()
    if 'component_fingerprint' in receipt:
        expected = receipt['component_fingerprint']
        if expected != fingerprint(components(root, infrastructure=True)):
            raise ValueError('LEGACY_ACQUISITION_COMPONENT_CHANGED')
        return
    commit = receipt.get('current_code_commit')
    if not commit:
        # Old generation 1 has no commit anchor. Only lineage validation is
        # possible for it; a latest unanchored legacy receipt cannot fail open.
        raise ValueError('LEGACY_ACQUISITION_COMPONENT_ANCHOR_REQUIRED')
    expected = components(root, commit=commit)
    actual = components(root)
    if expected == actual:
        return
    compatibility = _compatibility(root)
    if expected != compatibility['before'] or actual != compatibility['after']:
        raise ValueError('LEGACY_ACQUISITION_COMPONENT_CHANGED')

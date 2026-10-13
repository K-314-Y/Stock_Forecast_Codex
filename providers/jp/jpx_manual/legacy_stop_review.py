"""Explicit, offline manual review of one narrowly allowed legacy STOP.

No transport, automatic release, reviewer identity, or signature is provided.
The acquisition lock serializes the create-only receipt and index registration.
"""
from datetime import datetime
import hashlib
from pathlib import Path
import re

from engine import minimal_trial as core
from engine import forward_artifacts as frozen
from . import historical_policy_v2 as policy
from . import acquisition_binding as binding

SCHEMA = 'legacy-hard-stop-reviewed-release-v1'
POLICY = 'STOP_RETRY_RESUME_V2_REVIEWED_LEGACY_V1'
VALIDATION_SCHEMA = 'legacy-stop-validation-v1'
REASON = 'LISTING_EMPTY_OR_AMBIGUOUS'
FOLDER = 'legacy_reviews_v1'
RENEWAL_SCHEMA = 'legacy-hard-stop-reviewed-release-renewal-v1'
RENEWAL_POLICY = 'STOP_RETRY_RESUME_V2_REVIEWED_LEGACY_RENEWAL_V1'
RENEWAL_VALIDATION_SCHEMA = 'legacy-stop-renewal-validation-v1'
CHECKS = ('html_table_empty', 'json_endpoints', 'monthly_list', 'monthly_json',
          'exact_session_href', 'no_url_guessing', 'official_host')
SUITES = ('legacy_release', 'stop_policy', 'crash_safety', 'challenge_ledger',
          'historical_direct', 'json_discovery', 'partial_ticker', 'full_offline', 'public_tree')
RENEWAL_SUITES = (*SUITES, 'renewal', 'interrupted_attempt')
FIX_PATHS = ('providers/jp/jpx_manual/historical_direct.py',
             'providers/jp/jpx_manual/historical_policy_v2.py',
             'providers/jp/jpx_manual/legacy_stop_review.py', 'config/operation_policy_v2.md')


def _inside(ref, root, prefixes, *, body=False):
    if body:
        # *.http1.sha256 seals the metadata JSON, not the sibling *.http1.body.
        # Body integrity is bound by both its reference and the sealed discovery.
        path = Path(ref['path'])
        if path.suffix != '.body' or not path.is_file() or core.sha256(path) != ref['sha256']:
            raise ValueError('LEGACY_REVIEW_BODY_HASH_CONFLICT')
    else:
        path = policy.verify_reference(ref)
    if str(path.resolve()) != ref['path'] or not path.is_relative_to(root):
        raise ValueError('REVIEW_REFERENCE_SCOPE_INVALID')
    if path.relative_to(root).parts[0] not in prefixes:
        raise ValueError('REVIEW_REFERENCE_SCOPE_INVALID')
    return path


def _sealed(ref, root, prefixes):
    try:
        return core.read_frozen(_inside(ref, root, prefixes))
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise ValueError('LEGACY_REVIEW_EVIDENCE_INVALID') from error


def _eligible(stops, expected_sha):
    ref = stops.index['legacy']
    if (not ref or ref != dict(path=str(stops.store / 'STOP.json'), sha256=expected_sha)):
        raise ValueError('LEGACY_REVIEW_STOP_REFERENCE_INVALID')
    old = _sealed(ref, stops.store.parents[1], ('data',))
    keys = {'acquisition_method', 'automatic_retry', 'failed_at', 'reason', 'session', 'status'}
    if (set(old) != keys or old['reason'] != REASON or old['automatic_retry'] is not False
            or old['status'] != 'REVIEW_REQUIRED'
            or old['acquisition_method'] != 'OFFICIAL_LISTING_DIRECT_HTTPS'):
        raise ValueError('LEGACY_REVIEW_REASON_OR_FORMAT_FORBIDDEN')
    if datetime.fromisoformat(old['failed_at']).tzinfo is None:
        raise ValueError('LEGACY_REVIEW_STOP_TIME_INVALID')
    datetime.strptime(old['session'], '%Y-%m-%d')
    items = [item for item in stops.index['active'] if item['reference'] == ref]
    if len(items) != 1:
        raise ValueError('LEGACY_REVIEW_ACTIVE_REFERENCE_INVALID')
    record = items[0]['record']
    required = dict(legacy=True, policy_version=policy.VERSION, stop_id='legacy-' + expected_sha,
                    stop_code='HARD_STOP_LEGACY_UNCLASSIFIED', stop_class='HARD_STOP',
                    recoverability='HARD_STOP', state='ACTIVE', scope='global',
                    target='JPX_HISTORICAL', evidence_refs=[ref], created_at=old['failed_at'])
    if any(record.get(k) != v for k, v in required.items()):
        raise ValueError('LEGACY_REVIEW_CLASSIFICATION_FORBIDDEN')
    return ref, old


def replay_discovery(root, review_evidence, discovery_inputs, session):
    """Replay retained, hash-bound official bytes; never construct a PDF URL.

    The known legacy discovery schema used `sha256` for some chain entries.
    Normalize that explicit field alias in memory only, preserving originals.
    """
    from . import historical_direct as direct
    root = Path(root).resolve()
    review = _sealed(review_evidence, root, ('analysis',))
    if (review.get('schema_version') != 'jpx-json-discovery-v1'
            or review.get('status') != 'HREF_DISCOVERED' or review.get('session') != session
            or review.get('acquisition_method') != direct.METHOD or review.get('pdf_requests') != 0
            or set(discovery_inputs) != {'listing', 'monthly_list', 'monthly'}):
        raise ValueError('LEGACY_REVIEW_DISCOVERY_INVALID')

    def load(label, first, digest_key, url_key):
        path = _inside(discovery_inputs[label], root, ('analysis',), body=True)
        digest = review[digest_key]
        if discovery_inputs[label]['sha256'] != digest:
            raise ValueError('LEGACY_REVIEW_DISCOVERY_HASH_CONFLICT')
        chain = []
        for original in review['redirect_chains'][label]:
            entry = dict(original)
            if 'sha256' in entry:
                if entry.get('response_sha256', entry['sha256']) != entry['sha256']:
                    raise ValueError('LEGACY_REVIEW_CHAIN_HASH_CONFLICT')
                entry['response_sha256'] = entry['sha256']
            chain.append(entry)
        direct._verify_redirect_evidence(chain, first, digest)
        last = chain[-1]
        if review[url_key] != last['url']:
            raise ValueError('LEGACY_REVIEW_DISCOVERY_PROVENANCE_CONFLICT')
        return direct.Response(last['url'], last['status'], last['headers'], path.read_bytes())

    listing = load('listing', direct.LISTING, 'listing_sha256', 'listing_url')
    try:
        direct.listing_link(listing, session)
    except ValueError as error:
        if str(error) != REASON:
            raise
    else:
        raise ValueError('LEGACY_REVIEW_ROOT_CAUSE_NOT_REPRODUCED')
    discovery = direct.json_endpoints(listing)
    if discovery != review['discovery']:
        raise ValueError('LEGACY_REVIEW_ENDPOINT_CONFLICT')
    monthly_list = load('monthly_list', discovery['monthly_list_endpoint'],
                        'monthly_list_sha256', 'monthly_list_url')
    month_url = direct.monthly_json_url(monthly_list, discovery, session)
    if not month_url or month_url != review['monthly_json_reference_url']:
        raise ValueError('LEGACY_REVIEW_MONTH_CONFLICT')
    monthly = load('monthly', month_url, 'monthly_json_sha256', 'monthly_json_reference_url')
    link = direct.json_stock_link(monthly, session)
    if not link or link != review['link']:
        raise ValueError('LEGACY_REVIEW_EXACT_HREF_CONFLICT')
    direct.official_url(link['resolved_pdf_url'])
    return dict(checks={key: 'PASS' for key in CHECKS}, link=link, discovery=discovery,
                live_requests=0)


def _fixes(root, fixes, *, current):
    """Old source references are lineage values; latest references bind bytes."""
    paths = set()
    for fix in fixes:
        path = Path(fix['path'])
        if (set(fix) != {'path', 'sha256'} or not path.is_absolute()
                or str(path) != fix['path'] or '..' in path.parts
                or not path.is_relative_to(root)
                or path.relative_to(root).parts[0] not in ('providers', 'engine', 'config', 'tests')
                or path.suffix not in ('.py', '.json', '.md')
                or not re.fullmatch(r'[0-9a-f]{64}', fix['sha256'])):
            raise ValueError('LEGACY_REVIEW_FIX_REFERENCE_INVALID')
        if current:
            _inside(fix, root, ('providers', 'engine', 'config', 'tests'))
        paths.add(str(path.relative_to(root)))
    if not set(FIX_PATHS).issubset(paths) or len(paths) != len(fixes):
        raise ValueError('LEGACY_REVIEW_CURRENT_FIX_REQUIRED')


def _git(root, *arguments):
    return binding.git(root, *arguments)


def _code_binding(root, receipt, *, authoring=False):
    """Commits authenticate audit lineage; component fingerprints gate execution."""
    base, current = receipt['base_code_commit'], receipt['current_code_commit']
    if (not re.fullmatch(r'[0-9a-f]{40}', base)
            or not re.fullmatch(r'[0-9a-f]{40}', current)
            or _git(root, 'rev-parse', '--show-toplevel').decode().strip() != str(root)):
        raise ValueError('LEGACY_RENEWAL_CODE_COMMIT_INVALID')
    if authoring and (_git(root, 'rev-parse', 'HEAD').decode().strip() != current
                      or _git(root, 'status', '--porcelain=v1')):
        raise ValueError('LEGACY_RENEWAL_CLEAN_CURRENT_COMMIT_REQUIRED')
    _git(root, 'merge-base', '--is-ancestor', base, current)
    for commit, fixes in ((base, receipt['previous_fix_refs']), (current, receipt['fix_refs'])):
        for ref in fixes:
            relative = str(Path(ref['path']).relative_to(root))
            if hashlib.sha256(_git(root, 'show', commit + ':' + relative)).hexdigest() != ref['sha256']:
                raise ValueError('LEGACY_RENEWAL_COMMITTED_FIX_HASH_CONFLICT')


def _validate(stops, receipt, *, current=True, previous=None, previous_reference=None):
    root = stops.store.parents[1]
    ref, old = _eligible(stops, receipt['legacy_stop_sha256'])
    renewal = previous is not None
    exact = dict(previous_stop_reference=ref,
                 previous_stop_contents=old, original_reason=REASON, session=old['session'],
                 scope='global', target='JPX_HISTORICAL', review_decision='APPROVED',
                 review_action='EXPLICIT_MANUAL_FUNCTION', project_root=str(root))
    if renewal:
        exact.update(schema_version=RENEWAL_SCHEMA, policy_version=RENEWAL_POLICY,
                     state='REVIEWED_RELEASE_RENEWAL', release_type='LEGACY_HARD_STOP_MANUAL_REVIEW_RENEWAL',
                     generation=previous.get('generation', 1) + 1,
                     previous_release_reference=previous_reference,
                     previous_release_sha256=previous_reference['sha256'],
                     previous_fix_refs=previous['fix_refs'], review_evidence=previous['review_evidence'],
                     discovery_inputs=previous['discovery_inputs'],
                     discovery_validation=previous['discovery_validation'])
        if type(receipt.get('generation')) is not int:
            raise ValueError('LEGACY_RENEWAL_GENERATION_INVALID')
        for key in ('base_code_commit', 'current_code_commit'):
            if not re.fullmatch(r'[0-9a-f]{40}', receipt[key]):
                raise ValueError('LEGACY_RENEWAL_CODE_COMMIT_INVALID')
        if previous.get('current_code_commit') and receipt['base_code_commit'] != previous['current_code_commit']:
            raise ValueError('LEGACY_RENEWAL_BASE_COMMIT_LINEAGE_INVALID')
    else:
        exact.update(schema_version=SCHEMA, policy_version=POLICY, state='REVIEWED_RELEASE',
                     release_type='LEGACY_HARD_STOP_MANUAL_REVIEW')
        if type(receipt.get('generation', 1)) is not int or receipt.get('generation', 1) != 1:
            raise ValueError('LEGACY_RENEWAL_GENERATION_INVALID')
    if any(receipt.get(k) != v for k, v in exact.items()):
        raise ValueError('LEGACY_REVIEW_RECEIPT_LINEAGE_INVALID')
    for key in (('root_cause', 'resolution', 'renewal_reason') if renewal else ('root_cause', 'resolution')):
        if not isinstance(receipt.get(key), str) or not receipt[key].strip():
            raise ValueError('LEGACY_REVIEW_CAUSE_OR_RESOLUTION_REQUIRED')
    timestamp = datetime.fromisoformat(receipt['renewed_at' if renewal else 'released_at'])
    if timestamp.tzinfo is None:
        raise ValueError('LEGACY_REVIEW_RELEASE_TIME_INVALID')
    if renewal and timestamp < datetime.fromisoformat(previous.get('renewed_at', previous.get('released_at'))):
        raise ValueError('LEGACY_RENEWAL_TIME_LINEAGE_INVALID')
    fixes = receipt['fix_refs']
    _fixes(root, fixes, current=False)
    if renewal:
        _code_binding(root, receipt)
    if current:
        binding.validate(root, receipt)
    replay = replay_discovery(root, receipt['review_evidence'], receipt['discovery_inputs'], old['session'])
    if receipt.get('discovery_validation') != replay:
        raise ValueError('LEGACY_REVIEW_REPLAY_CONFLICT')
    checks, suites = {}, {}
    if not receipt['validation_refs']:
        raise ValueError('LEGACY_REVIEW_VALIDATION_REQUIRED')
    for validation in receipt['validation_refs']:
        report = _sealed(validation, root, ('data',))
        if (report.get('schema_version') != (RENEWAL_VALIDATION_SCHEMA if renewal else VALIDATION_SCHEMA)
                or report.get('status') != 'PASS'
                or report.get('fix_refs') != fixes or report.get('previous_stop_reference') != ref
                or report.get('review_evidence') != receipt['review_evidence']
                or report.get('discovery_inputs') != receipt['discovery_inputs']
                or report.get('live_jpx_requests') != 0):
            raise ValueError('LEGACY_REVIEW_VALIDATION_FAILED')
        if renewal and any(report.get(key) != receipt[key] for key in (
                'generation', 'previous_release_reference', 'previous_fix_refs',
                'base_code_commit', 'current_code_commit')):
            raise ValueError('LEGACY_RENEWAL_VALIDATION_LINEAGE_INVALID')
        checks.update(report.get('checks', {}))
        for name, result in report.get('suites', {}).items():
            if (result.get('status') != 'PASS' or type(result.get('tests_run')) is not int
                    or result['tests_run'] <= 0
                    or type(result.get('failures')) is not int or result['failures'] != 0
                    or type(result.get('errors')) is not int or result['errors'] != 0):
                raise ValueError('LEGACY_REVIEW_TESTS_FAILED')
            suites[name] = result
    if (any(checks.get(key) != 'PASS' for key in CHECKS)
            or not set(RENEWAL_SUITES if renewal else SUITES).issubset(suites)):
        raise ValueError('LEGACY_REVIEW_REQUIRED_TESTS_MISSING')
    return ref


def _verified_chain(stops, *, bind_latest=True):
    """All generations retain evidence; only the latest binds current source.

    bind_latest=False is used only inside explicit renewal bootstrap. This does
    not disable gate(): gate always re-enters with current binding enabled.
    """
    try:
        folder = stops.store / FOLDER
        entries = stops.index.get('reviewed_legacy_releases', [])
        expected, chain = set(), []
        for generation, entry in enumerate(entries, 1):
            receipt_ref = entry['receipt_reference']
            path = policy.verify_reference(receipt_ref)
            name = ('reviewed-release-' + stops.index['legacy']['sha256']
                    + ('' if generation == 1 else '-generation-' + str(generation).zfill(6)) + '.json')
            if (path.parent != folder or str(path.resolve()) != receipt_ref['path']
                    or path.name != name or type(entry.get('generation', 1)) is not int
                    or entry.get('generation', 1) != generation):
                raise ValueError('invalid receipt path')
            receipt = core.read_frozen(path)
            ref = _validate(stops, receipt, current=bind_latest and generation == len(entries),
                            previous=chain[-1][1] if chain else None,
                            previous_reference=chain[-1][0] if chain else None)
            if entry['previous_stop_reference'] != ref:
                raise ValueError('duplicate or mismatched release')
            expected.update((path.name, path.with_suffix('.sha256').name))
            chain.append((receipt_ref, receipt))
        actual = {path.name for path in folder.iterdir()} if folder.exists() else set()
        # Interrupted seal/index writes never imply approval. Retain orphans for review.
        if actual != expected:
            raise ValueError('unregistered or missing receipt evidence')
        return chain
    except (ValueError, OSError, KeyError, TypeError, AttributeError, IndexError) as error:
        raise ValueError('REVIEWED_LEGACY_RELEASE_INVALID') from error


def verified_releases(stops):
    """Normal startup/gate: latest current binding is mandatory."""
    chain = _verified_chain(stops)
    return [chain[-1][1]['previous_stop_reference']] if chain else []


def reviewed_legacy_root_attempt(stops, session, folder):
    """Read-only recognition. Never starts an attempt, renews, or does network."""
    chain = _verified_chain(stops)
    if not chain:
        return False
    receipt = chain[-1][1]
    folder = Path(folder)
    if (session != receipt['session'] or folder != stops.store / session
            or folder.is_symlink() or not folder.is_dir()):
        return False
    expected = {'start.json', 'start.sha256', 'failure.json', 'failure.sha256'}
    files = list(folder.iterdir())
    if {path.name for path in files} != expected or any(
            path.is_symlink() or not path.is_file() for path in files):
        return False
    try:
        start = core.read_frozen(folder / 'start.json')
        failure = core.read_frozen(folder / 'failure.json')
        old = _sealed(receipt['previous_stop_reference'], stops.store.parents[1], ('data',))
        return (start.get('session') == session
                and start.get('acquisition_method') == old['acquisition_method']
                and failure == old
                and core.sha256(folder / 'failure.json') == receipt['legacy_stop_sha256'])
    except (ValueError, OSError, KeyError, TypeError, AttributeError):
        return False


def manual_review_release(root, *, expected_stop_sha256, review_decision, root_cause,
                          resolution, review_evidence, discovery_inputs, fix_refs, validation_refs):
    """Explicit approval invocation only; no acquisition path calls this function."""
    if review_decision != 'APPROVED':
        raise ValueError('EXPLICIT_MANUAL_APPROVAL_REQUIRED')
    root = Path(root).resolve()
    store = root / 'data/jpx_historical_direct'
    with frozen._locked(store):
        stops = policy.Stops(store)
        ref, old = _eligible(stops, expected_stop_sha256)
        if ref in verified_releases(stops):
            raise FileExistsError('LEGACY_STOP_ALREADY_REVIEWED')
        # Initial authoring must bind supplied PASS reports to current fix bytes.
        # Historical execution validates their audit identity instead.
        _fixes(root, fix_refs, current=True)
        receipt = dict(schema_version=SCHEMA, policy_version=POLICY, state='REVIEWED_RELEASE',
                       release_type='LEGACY_HARD_STOP_MANUAL_REVIEW', previous_stop_reference=ref,
                       legacy_stop_sha256=expected_stop_sha256, previous_stop_contents=old,
                       original_reason=REASON, session=old['session'], scope='global', target='JPX_HISTORICAL',
                       project_root=str(root), root_cause=root_cause, resolution=resolution,
                       review_evidence=review_evidence, discovery_inputs=discovery_inputs,
                       fix_refs=fix_refs, validation_refs=validation_refs, review_decision=review_decision,
                       review_action='EXPLICIT_MANUAL_FUNCTION', released_at=policy.stamp(),
                       discovery_validation=replay_discovery(root, review_evidence, discovery_inputs, old['session']),
                       component_fingerprint=binding.fingerprint(binding.components(root, infrastructure=True)))
        _validate(stops, receipt)
        folder = store / FOLDER
        folder.mkdir(exist_ok=True)
        frozen._sync_dir(store)
        path = folder / ('reviewed-release-' + expected_stop_sha256 + '.json')
        policy.seal(path, receipt)
        receipt_ref = policy.reference(path)
        core.read_frozen(path)
        stops.index.setdefault('reviewed_legacy_releases', []).append(
            dict(previous_stop_reference=ref, receipt_reference=receipt_ref))
        verified_releases(stops)
        stops.save()  # Old STOP and active history stay unchanged.
        return receipt_ref


def manual_review_renewal(root, *, expected_stop_sha256, expected_previous_release_sha256,
                          review_decision, renewal_reason, root_cause, resolution,
                          fix_refs, validation_refs, base_code_commit, current_code_commit):
    """Explicit offline renewal; bootstrap preserves every previous evidence check.

    No normal acquisition caller invokes this function. A stale previous source
    is allowed only here; its recorded refs, commits and immutable evidence must
    still validate. An incomplete publication never authorizes a later gate.
    """
    if review_decision != 'APPROVED':
        raise ValueError('EXPLICIT_MANUAL_APPROVAL_REQUIRED')
    root = Path(root).resolve()
    store = root / 'data/jpx_historical_direct'
    with frozen._locked(store):
        stops = policy.Stops.__new__(policy.Stops)
        stops._load_evidence(store)
        ref, old = _eligible(stops, expected_stop_sha256)
        chain = _verified_chain(stops, bind_latest=False)
        if not chain or chain[-1][0]['sha256'] != expected_previous_release_sha256:
            raise ValueError('LEGACY_RENEWAL_PREVIOUS_RELEASE_INVALID')
        previous_ref, previous = chain[-1]
        if previous['previous_stop_reference'] != ref:
            raise ValueError('LEGACY_RENEWAL_STOP_LINEAGE_INVALID')
        for item in stops.index['active']:
            record = item['record']
            if item['reference'] != ref and (record['scope'] in ('host', 'global') or record['target'] == old['session']):
                raise ValueError('LEGACY_RENEWAL_OTHER_STOP_ACTIVE')
        generation = len(chain) + 1
        receipt = dict(schema_version=RENEWAL_SCHEMA, policy_version=RENEWAL_POLICY,
            state='REVIEWED_RELEASE_RENEWAL', release_type='LEGACY_HARD_STOP_MANUAL_REVIEW_RENEWAL',
            generation=generation, previous_release_reference=previous_ref,
            previous_release_sha256=previous_ref['sha256'], previous_stop_reference=ref,
            legacy_stop_sha256=expected_stop_sha256, previous_stop_contents=old,
            original_reason=REASON, session=old['session'], scope='global', target='JPX_HISTORICAL',
            project_root=str(root), renewal_reason=renewal_reason, root_cause=root_cause,
            resolution=resolution, previous_fix_refs=previous['fix_refs'], fix_refs=fix_refs,
            validation_refs=validation_refs, review_evidence=previous['review_evidence'],
            discovery_inputs=previous['discovery_inputs'], discovery_validation=previous['discovery_validation'],
            review_decision=review_decision, review_action='EXPLICIT_MANUAL_FUNCTION',
            base_code_commit=base_code_commit, current_code_commit=current_code_commit,
            renewed_at=policy.stamp(),
            component_fingerprint=binding.fingerprint(binding.components(root, infrastructure=True)))
        _code_binding(root, receipt, authoring=True)
        _validate(stops, receipt, previous=previous, previous_reference=previous_ref)
        folder = store / FOLDER
        path = folder / ('reviewed-release-' + expected_stop_sha256
                         + '-generation-' + str(generation).zfill(6) + '.json')
        policy.seal(path, receipt)
        receipt_ref = policy.reference(path)
        core.read_frozen(path)
        stops.index['reviewed_legacy_releases'].append(dict(
            generation=generation, previous_stop_reference=ref, receipt_reference=receipt_ref))
        verified_releases(stops)
        stops.save()
        return receipt_ref

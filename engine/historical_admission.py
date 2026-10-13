"""Explicit historical source/feature admission; pure local evidence, no retrieval."""
import copy
import csv
import io
import json
import math
from collections import Counter, defaultdict
from datetime import datetime

from engine import minimal_trial as core
from engine.parallel_data import PRICE9, hash_valid, stamp

POLICY = core.ROOT / 'config/historical_pit_admission_v1.json'
VERSION = 'historical-pit-admission-v1'
YAHOO = 'YAHOO_FINANCE_YFINANCE'
JPX = {'JPX_DAILY_REPORT', 'JPX_DAILY_REPORT_MANUAL'}
FIELDS = ('Date', 'Open', 'High', 'Low', 'Close', 'Adj Close', 'Volume', 'Dividends', 'Stock Splits')


def policy():
    return json.loads(POLICY.read_bytes())


def combine(states):
    states = list(states)
    if not states or 'UNKNOWN' in states:
        return 'UNKNOWN'
    if any(s not in ('PASS', 'FAIL', 'NOT_APPLICABLE') for s in states):
        raise ValueError('ADMISSION_GATE_INVALID')
    return 'FAIL' if 'FAIL' in states else 'PASS'


def classify(gates):
    state = combine(gates.values())
    return {'UNKNOWN': 'UNKNOWN_PIT', 'FAIL': 'REJECTED_PIT', 'PASS': 'ADMITTED_PIT'}[state]


def detect_yfinance_schema(fields, meta):
    """Fail closed on unknown versions/options; filename is never option evidence."""
    requested = meta.get('requested', {})
    identity = meta.get('metadata', {})
    schema = (len(fields) == len(set(fields)) and set(fields) == set(FIELDS))
    version_known = meta.get('yfinance_version') == '1.7.0'
    # Verified against the library's 1.7.0 source, not today's default.
    back = requested.get('back_adjust', False) if version_known else requested.get('back_adjust')
    auto = requested.get('auto_adjust')
    supported = (schema and version_known and auto is False and back is False
                 and requested.get('repair') is False and requested.get('actions') is True
                 and requested.get('interval') == '1d' and requested.get('keepna') is True
                 and requested.get('prepost', False) is False)
    return dict(schema='PASS' if schema else 'FAIL',
        source_identity='PASS' if (identity.get('symbol') == meta.get('symbol')
            and identity.get('symbol') in {t + '.T' for t in core.CORE}
            and identity.get('currency') == 'JPY' and identity.get('exchangeTimezoneName') == 'Asia/Tokyo'
            and meta.get('status') == 'RETRIEVED') else 'UNKNOWN',
        field_semantics='PASS' if supported else 'UNKNOWN',
        library_version=meta.get('yfinance_version'), auto_adjust=auto, back_adjust=back,
        back_adjust_evidence='EXPLICIT' if 'back_adjust' in requested else 'VERIFIED_1.7.0_DEFAULT' if version_known else 'UNKNOWN',
        repair=requested.get('repair'), native_unadjusted=False,
        price_basis='PROVIDER_SPLIT_BASIS_CANDIDATE;NOT_HISTORICAL_RAW',
        fields_used={'Open': ['OUTCOME_RETURN', 'THRESHOLD_BASE', 'QUALITY'],
            'High': ['THRESHOLD_ATR', 'QUALITY'], 'Low': ['THRESHOLD_ATR', 'QUALITY'],
            'Close': ['PRICE_FEATURES', 'OUTCOME_RETURN', 'THRESHOLD', 'QUALITY'],
            'Volume': ['QUALITY_ONLY'], 'Adj Close': [],
            'Dividends': ['ACTION_AUDIT_ONLY'], 'Stock Splits': ['ACTION_AUDIT_ONLY']},
        same_basis='UNKNOWN', historical_vintage_known=False,
        revision_note='Library option semantics do not certify historical provider corrections or complete per-row factors.')


def feature_proofs():
    """Proof scope refers to actual core.features/quality.threshold_candidate formulas."""
    proofs = []
    formulas = {'ret_1d': 'C[-1]/C[-2]-1', 'ret_5d': 'C[-1]/C[-6]-1',
        'momentum_20d': 'C[-1]/C[-21]-1', 'momentum_60d': 'C[-1]/C[-61]-1',
        'realized_vol_20d': 'sample_stdev(last 20 adjacent close ratios minus one)',
        'MA20': 'mean(last 20 closes)', 'MA60': 'mean(last 60 closes)',
        'MA_state': 'BULL if MA20>MA60, BEAR if MA20<MA60, else NEUTRAL',
        'threshold': 'p33(abs(C/O-1)); if abs(recent_vol/median_rolling_vol-1)>=.30: (p33+ATR14_pct)/2'}
    for name in PRICE9:
        absolute = name in ('MA20', 'MA60')
        threshold = name == 'threshold'
        proofs.append(dict(feature=name, implementation='engine.historical_quality.threshold_candidate' if threshold else 'engine.minimal_trial.features',
            formula=formulas[name], transformation="(O_i,H_i,L_i,C_i)'=k_i*(O_i,H_i,L_i,C_i); k_i>0",
            revision_invariant='NO' if absolute else 'CONDITIONAL',
            uniform_positive_factor='NO' if absolute else 'YES',
            invariance_condition=('k=1 or observed historical vintage; no rescaling/redefinition authorized' if absolute else
                'One common k across entire threshold prefix including every volatility/ATR support; unchanged row eligibility, no additive corrections' if threshold else
                'Same positive k across the full feature support; no additive or unequal-field revisions'),
            proof=('MA_n(k*C)=k*MA_n(C)' if absolute else
                'Each intraday ratio and adjacent close ratio is unchanged; stdev/median ratio and branch unchanged; TR scales by k and ATR/last Close cancels k' if threshold else
                'Ratios cancel k; stdev of identical ratios unchanged; positive k preserves MA ordering'),
            nonuniform_factor='NOT_GUARANTEED',
            counterexample=('All C=100, k=0.5: MA 100 becomes 50' if absolute else
                'O=C with varying positive prices: per-day factors flatten the prefix, changing volatility and potentially making normal volatility invalid; unequal high/close factors also change ATR' if threshold else
                'C=[100,110], factors=[0.5,1]: return 0.10 becomes 1.20; scaling older MA60-only observations can reverse MA ordering'),
            split_before_window='No new factor in window; invariance still needs fixed vintage/basis',
            split_after_window='Common scaling cancels only for ratio/order features; absolute MAs change',
            split_inside_window='Piecewise scaling is not a general invariance proof; at-target existing split adjustment must be distinguished from later corrections',
            final_admission_status='NOT_ADMITTED_BY_INVARIANCE' if absolute else 'CANDIDATE_ONLY_REQUIRES_SOURCE_AND_AVAILABILITY_EVIDENCE',
            floating_point='Algebra over real numbers; numerical tests use tolerances, not a claim of bitwise equality or immunity at a classification boundary'))
    return dict(version='price9-revision-proof-v1', features=proofs,
        outcome=dict(
            formula='C_T/O_T-1', transformation="O_T'=k*O_T; C_T'=k*C_T; k>0",
            proof='(k*C_T)/(k*O_T)-1=C_T/O_T-1', same_factor_invariant=True,
            different_factor_counterexample='O=100,C=110,kO=.5,kC=1: .10 becomes 1.20',
            condition='Same ticker/session, same basis, finite positive O/C, resolved actions, no unequal/additive correction',
            split_boundary='Never divide target Close by previous-session Open; unresolved intraday/basis boundary excluded',
            final_admission_status='CONDITIONAL_SOURCE_EVIDENCE_REQUIRED'))


def passthrough_deltas(values, normalized, tolerance):
    """Allow existing serialization tolerance only, without modifying either input."""
    deltas = [abs(normalized[k] - values[k]) for k in ('open', 'high', 'low', 'close', 'volume')]
    if any(not math.isfinite(v) or v > tolerance for v in deltas):
        raise ValueError('HISTORICAL_NORMALIZED_VALUE_CONFLICT')
    return [v for v in deltas if v != 0]


def outcome_decision(row):
    e = row.get('outcome_evidence', {})
    v = row['values']
    integrity = 'FAIL' if row['quality_issues'] or any(
        type(v.get(k)) not in (int, float) or not math.isfinite(v[k]) or v[k] <= 0 for k in ('open', 'close')) else 'PASS'
    gates = {k: e.get(k, 'UNKNOWN') for k in ('source_identity', 'field_semantics', 'same_basis', 'revision_safety', 'corporate_action')}
    factors = [e.get('open_revision_factor'), e.get('close_revision_factor')]
    if any(v is not None for v in factors):
        valid = all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in factors)
        if not valid or factors[0] != factors[1]:
            gates['same_basis'] = 'FAIL'
            gates['revision_safety'] = 'FAIL'
    gates['source_hash'] = 'PASS' if hash_valid(row.get('source_hash')) else 'UNKNOWN'
    gates['grain'] = 'PASS' if row['ticker'] in core.CORE and datetime.fromisoformat(row['target_session']).date().isoformat() == row['target_session'] else 'FAIL'
    gates['outcome_integrity'] = integrity
    known = any(q['ticker'] == row['ticker'] and q['session'] == row['target_session'] for q in policy()['known_quarantine'])
    if known:
        gates['outcome_integrity'] = 'FAIL'
    state = combine(gates.values())
    return dict(gates=gates, status=state, admitted=state == 'PASS',
                reasons=sorted([k.upper() + '_' + v for k, v in gates.items() if v not in ('PASS', 'NOT_APPLICABLE')]
                    + (['SKIPPED_OUTCOME_INTEGRITY'] if gates['outcome_integrity'] == 'FAIL' else [])))


def select_outcomes(candidates):
    """Preference is label-only. A conflict never chooses a more convenient source."""
    grouped = defaultdict(list)
    for r in candidates:
        grouped[(r['target_session'], r['ticker'])].append(r)
    output, comparisons = [], []
    tolerance = policy()['overlap_price_absolute_tolerance']
    for key, rows in sorted(grouped.items()):
        if len({r['source'] for r in rows}) != len(rows):
            raise ValueError('DUPLICATE_OUTCOME_SOURCE_GRAIN')
        if any(r['source'] not in JPX | {YAHOO} for r in rows):
            raise ValueError('OUTCOME_SOURCE_IDENTITY_UNRESOLVED')
        decisions = {r['source']: outcome_decision(r) for r in rows}
        ordered = sorted(rows, key=lambda r: (not (r['source'] in JPX and decisions[r['source']]['admitted']),
                                             not decisions[r['source']]['admitted'], r['source'] != YAHOO, r['source']))
        chosen = copy.deepcopy(ordered[0])
        conflict = False
        if len(rows) > 1:
            for other in ordered[1:]:
                differences = {f: chosen['values'][f] - other['values'][f] for f in ('open', 'high', 'low', 'close')}
                finite = all(math.isfinite(v) for v in differences.values())
                match = finite and all(abs(v) <= tolerance for v in differences.values())
                conflict |= not match
                return_delta = (chosen['values']['close'] / chosen['values']['open'] - other['values']['close'] / other['values']['open']
                                if all(r['values']['open'] > 0 for r in (chosen, other)) else None)
                comparisons.append(dict(target_session=key[0], ticker=key[1],
                    sources=[chosen['source'], other['source']], source_hashes=[chosen['source_hash'], other['source_hash']],
                    price_differences=differences if finite else None, return_difference=return_delta,
                    tolerance=tolerance, status='MATCH_WITHIN_TOLERANCE' if match else 'OUTCOME_CONFLICT',
                    proves_historical_vintage=False, admitted_official_preference=chosen['source'] in JPX and decisions[chosen['source']]['admitted']))
        decision = decisions[chosen['source']]
        if conflict:
            decision = copy.deepcopy(decision)
            decision['gates']['outcome_integrity'] = 'FAIL'
            decision.update(status=combine(decision['gates'].values()), admitted=False,
                            reasons=sorted(set(decision['reasons'] + ['OUTCOME_CONFLICT', 'SKIPPED_OUTCOME_INTEGRITY'])))
        chosen['outcome_admission'] = decision
        chosen['outcome_source_admitted'] = decision['admitted']
        chosen['outcome_integrity'] = decision['gates']['outcome_integrity'] == 'PASS'
        chosen['source_refs'] = sorted({ref for r in rows for ref in r['source_refs']})
        chosen['selection_reason'] = ('ADMITTED_OFFICIAL_JPX_PREFERRED' if chosen['source'] in JPX and decision['admitted'] else
            'NO_ADMITTED_SOURCE' if not decision['admitted'] else 'ADMITTED_PROVIDER_SOURCE')
        output.append(chosen)
    return output, comparisons


def feature_checks(known, prior, day, bounds):
    """No absence-of-action inference. An observed pre-cutoff vintage may be used."""
    cutoff = stamp(day + 'T08:45:00+09:00')
    available = len(known) == len(prior) == 61 and all(
        b['available_at'] is not None and stamp(b['available_at']) <= cutoff for b in bounds)
    integrity = ('FAIL' if len(known) != 61 or any(r['quality_issues'] for r in known) else
                 'PASS' if all(r.get('feature_integrity') is True for r in known) else 'UNKNOWN')
    future_split = any(s >= day for r in known for s in r.get('split_sessions', []))
    observed_vintage = available and all(b.get('status') == 'OBSERVED' for b in bounds)
    revision = 'PASS' if observed_vintage else 'FAIL' if future_split else 'UNKNOWN'
    return dict(source_identity=combine(r.get('source_identity', 'UNKNOWN') for r in known),
        historical_availability='PASS' if available else 'UNKNOWN',
        revision_safety=revision, feature_integrity=integrity,
        corporate_action=combine(r.get('feature_corporate_action', 'UNKNOWN') for r in known))


def row_checks(feature, outcome, bound, asof):
    f = feature.get('admission_checks', {}) if feature else {}
    o = outcome.get('outcome_admission', {}) if outcome else {}
    gates = {k: f.get(k, 'UNKNOWN') for k in ('source_identity', 'historical_availability', 'revision_safety', 'feature_integrity', 'corporate_action')}
    gates['source_identity'] = combine([gates['source_identity'], o.get('gates', {}).get('source_identity', 'UNKNOWN')])
    gates['corporate_action'] = combine([gates['corporate_action'], o.get('gates', {}).get('corporate_action', 'UNKNOWN')])
    gates['outcome_integrity'] = o.get('gates', {}).get('outcome_integrity', 'FAIL' if not outcome else 'UNKNOWN')
    gates['outcome_source_admitted'] = o.get('status', 'UNKNOWN')
    gates['training_persistence'] = 'PASS' if outcome and outcome.get('derived_training_persistence_allowed') else 'UNKNOWN'
    gates['PIT'] = ('UNKNOWN' if not feature or not bound['available_at'] or feature['feature_available_at'] is None else
        'PASS' if stamp(feature['feature_available_at']) <= stamp(feature['cutoff'])
            and stamp(bound['available_at']) < asof and feature['feature_session'] < feature['target_session'] else 'FAIL')
    return gates


def provenance_csv(groups):
    """Expand compact artifact/field/session groups without copying price values."""
    stream = io.StringIO(newline='')
    columns = ['source', 'provider', 'file_artifact', 'field', 'ticker', 'session', 'raw_or_derived',
               'adjustment_basis', 'source_hash', 'existing_retrieved_at', 'historical_vintage_known', 'role']
    writer = csv.DictWriter(stream, fieldnames=columns, lineterminator='\n')
    writer.writeheader()
    for group in sorted(groups, key=lambda g: (g['file_artifact'], g['ticker'])):
        for session in sorted(group['sessions']):
            for field in sorted(group['fields']):
                row = {k: group[k] for k in columns if k not in ('field', 'session')}
                writer.writerow(dict(row, field=field, session=session))
    return stream.getvalue().encode('utf-8')


def summarize(dataset):
    rows = dataset['eligible_rows'] + dataset['excluded_rows']
    counts = Counter(r['admission_status'] for r in rows)
    audit = dataset['pit_audit']
    audit.update(admitted_dates=len({r['target_session'] for r in rows if r['admission_status'] == 'ADMITTED_PIT'}),
        unknown_rows=counts['UNKNOWN_PIT'], rejected_rows=counts['REJECTED_PIT'],
        admission_counts={s: counts[s] for s in ('ADMITTED_PIT', 'UNKNOWN_PIT', 'REJECTED_PIT')},
        per_ticker_coverage={t: {s: sum(r['ticker'] == t and r['admission_status'] == s for r in rows)
            for s in ('ADMITTED_PIT', 'UNKNOWN_PIT', 'REJECTED_PIT')} for t in core.CORE},
        known_gate_failures=dict(sorted(Counter(k for r in rows for k, v in r['admission_checks'].items() if v == 'FAIL').items())),
        pit_eligible_date_range=([min(r['target_session'] for r in dataset['eligible_rows']), max(r['target_session'] for r in dataset['eligible_rows'])] if dataset['eligible_rows'] else None))
    if counts['ADMITTED_PIT'] != len(dataset['eligible_rows']):
        raise ValueError('ADMISSION_ELIGIBLE_COUNT_MISMATCH')

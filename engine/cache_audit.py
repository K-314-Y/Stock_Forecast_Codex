"""Print compact research-only threshold audit; never fetch or freeze Trial state."""
import json
from engine import minimal_trial as engine


def main():
    result = {}
    for ticker in engine.CORE:
        try:
            rows, provenance = engine.load_history(ticker)
            limit = engine.threshold(rows)
            result[ticker] = {'status': 'RESEARCH_ONLY_NOT_TRIAL_FROZEN',
                              'training_end_date': limit['training_end_date'],
                              'training_rows': limit['training_rows'],
                              'T_base': limit['T_base'], 'T_final': limit['T_final'],
                              'volatility_adjustment_used': limit['volatility_adjustment_used'],
                              'ATR_component': limit['ATR_component'],
                              'source_hash': provenance['csv_sha256']}
        except (ValueError, KeyError, OSError) as error:
            result[ticker] = {'status': 'REVIEW_REQUIRED', 'reason': str(error),
                              'T_base': None, 'T_final': None}
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()

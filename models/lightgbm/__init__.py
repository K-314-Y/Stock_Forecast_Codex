"""Small frozen multiclass grid; native LightGBM with explicit class order."""
from itertools import product

import lightgbm as lgb

from engine.parallel_data import rules


def search_space():
    config = rules()['lightgbm']
    space = config['search_space']
    keys = tuple(space)
    result = [dict(zip(keys, values)) for values in product(*(space[k] for k in keys))]
    result = [p for p in result if p['num_leaves'] <= 2 ** p['max_depth']]
    if len(result) > config['maximum_candidates']:
        raise ValueError('LIGHTGBM_SEARCH_TOO_LARGE')
    return result


def fit(matrix, labels, parameters):
    config = rules()
    frozen = config['lightgbm']
    if parameters not in search_space():
        raise ValueError('LIGHTGBM_PARAMETERS_NOT_FROZEN')
    params = dict(parameters, objective='multiclass', num_class=3, seed=config['seed'],
                  deterministic=True, force_col_wise=True, num_threads=1, verbosity=-1,
                  feature_fraction_seed=config['seed'], data_random_seed=config['seed'])
    return lgb.train(params, lgb.Dataset(matrix, label=labels), num_boost_round=frozen['n_estimators'])

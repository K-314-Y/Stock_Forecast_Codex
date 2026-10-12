"""Core6 pooled train-only scaler and L2 multinomial logistic, price9 only."""
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.exceptions import ConvergenceWarning
import warnings

from engine.parallel_data import rules


def fit(matrix, labels):
    config = rules()
    parameters = dict(config['logistic'])
    parameters.pop('penalty')
    model = Pipeline([('scaler', StandardScaler()),
                      ('classifier', LogisticRegression(**parameters, l1_ratio=0.0, random_state=config['seed']))])
    with warnings.catch_warnings():
        warnings.simplefilter('error', ConvergenceWarning)
        model.fit(matrix, labels)
    return model

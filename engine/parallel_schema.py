"""Local Draft 2020-12 Freeze schema; semantic validators supply cross-field checks."""
import json

from jsonschema import Draft202012Validator, FormatChecker
from engine import minimal_trial as core


def validate(value, definition):
    schema = json.loads((core.ROOT / 'config/parallel_forecast_freeze_schema.json').read_text())
    schema['$ref'] = '#/$defs/' + definition
    schema.pop('anyOf')
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)

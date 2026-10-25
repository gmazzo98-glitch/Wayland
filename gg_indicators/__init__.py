"""
GG indicator schema + two-axis scoring over Indicators.xlsx (Sheet1 + Scoring Schema).

Extends the existing catalog (indicators.INDICATOR_SEED / models.IndicatorDefinition) and does not
touch the live scoring path, the database, or Sheet1. Entry points:

    from gg_indicators import IndicatorRegistry, Observation, CompanyContext, score_company, format_report
    reg = IndicatorRegistry.load()
    result = score_company(reg, [Observation.trigger("ebit_trend")], CompanyContext(ateco_code="28.99.9"))
    print(format_report(result))

See docs/GG_INDICATOR_INTEGRATION_REPORT.md for conflicts, assumptions and open items.
"""

from .engine import CompanyContext, Observation, ObservationError, compare_weight_sets, score_company
from .registry import ConfigError, IndicatorRegistry
from .report import format_report
from .schema import SchemaError, parse_tag
from .weights import PLACEHOLDER_LABEL, WeightSet, WeightSetError, default_weight_set, load_weight_sets, validate_weight_set

__all__ = [
    "CompanyContext", "ConfigError", "IndicatorRegistry", "Observation", "ObservationError", "PLACEHOLDER_LABEL",
    "SchemaError", "WeightSet", "WeightSetError", "compare_weight_sets", "default_weight_set", "format_report",
    "load_weight_sets", "parse_tag", "score_company", "validate_weight_set",
]

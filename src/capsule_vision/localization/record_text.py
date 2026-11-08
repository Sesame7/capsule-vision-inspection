"""Qt-independent rendering of stored record descriptions."""

import re

from .catalog import text
from .languages import Language

_TECHNICAL_TOKEN = re.compile(
    r"[a-z][a-z0-9_.+-]*://|[0-9a-f]{32,}|"
    r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|"
    r"(?=[a-z0-9_+/=-]{32,})(?=[a-z0-9_+/=-]*[0-9])[a-z0-9_+/=-]{32,}",
    re.IGNORECASE | re.ASCII,
)


def readable_value(value: str | None) -> str:
    """Keep recorded business text, without presenting opaque identifiers as names."""
    return value if value and not _TECHNICAL_TOKEN.search(value) else ""


def record_reason(value: str, language: Language) -> str:
    """Translate recognized exception names and lifecycle stages, keeping free text intact."""
    stage, separator, exception = value.rpartition(" · ")
    reasons = {
        "ConfigurationError": "records_config_error",
        "ConfigurationIOError": "records_config_io",
        "TimeoutError": "short_timeout",
    }
    stages = {
        "startup": "startup_error",
        "runtime": "runtime_error",
        "shutdown": "shutdown_error",
    }
    if exception not in reasons or (separator and stage not in stages):
        return value
    reason = text(reasons[exception], language)
    return f"{text(stages[stage], language)} · {reason}" if separator else reason

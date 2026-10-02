"""Configuration failures, distinct from inference and application lifecycle."""


class ConfigurationError(ValueError):
    """Invalid configuration data or references; no resolved result is produced."""


class ConfigurationIOError(ConfigurationError):
    """Configuration could not be read or atomically saved."""


class ModelArtifactError(ConfigurationError):
    """An independently verified model artifact is unavailable or mismatched."""

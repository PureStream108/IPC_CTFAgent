"""Validation errors for data received from APIs, providers, and checkpoints."""


class InputValidationError(ValueError):
    """Input violates its schema; preserve the ValueError API/recovery contract."""

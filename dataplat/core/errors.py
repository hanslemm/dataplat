"""Typed domain exceptions for dataplat."""

from __future__ import annotations


class DataplatError(Exception):
    """Base class for predictable operational errors."""


class ConfigError(DataplatError):
    """Raised for missing/invalid local configuration."""


class AuthError(DataplatError):
    """Raised for authentication and authorization failures."""


class ServiceError(DataplatError):
    """Raised for provider/service API failures."""


class ValidationError(DataplatError):
    """Raised for user input validation failures."""

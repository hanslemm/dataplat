"""Shared enums for airbyte CLI options."""

from __future__ import annotations

from enum import Enum


class ScheduleType(str, Enum):
    """Schedule type options."""

    cron = "cron"
    manual = "manual"


class DataResidency(str, Enum):
    """Data residency location options."""

    auto = "auto"
    us = "us"
    eu = "eu"


class NamespaceDefinition(str, Enum):
    """Namespace definition options."""

    source = "source"
    destination = "destination"
    custom_format = "custom_format"


class ConnectionStatus(str, Enum):
    """Connection status options."""

    active = "active"
    inactive = "inactive"
    deprecated = "deprecated"


class SchemaUpdatesBehavior(str, Enum):
    """Non-breaking schema updates behavior options."""

    ignore = "ignore"
    disable_connection = "disable_connection"
    propagate_columns = "propagate_columns"
    propagate_fully = "propagate_fully"

"""Semantic firewall schemas for LLM-generated playbooks."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class SeverityLevel(str, Enum):
    """Severity levels for incidents."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ActionType(str, Enum):
    """Allowed remediation action types."""

    RESTART_SERVICE = "RESTART_SERVICE"
    MIGRATE_VM = "MIGRATE_VM"
    ISOLATE_NODE = "ISOLATE_NODE"
    NOTIFY_ADMIN = "NOTIFY_ADMIN"
    POWER_CYCLE = "POWER_CYCLE"


class ActionDSL(BaseModel):
    """Strict schema for safe, machine-readable remediation playbooks."""

    summary: str = Field(description="Human-readable summary of the incident.")
    severity: SeverityLevel = Field(description="Incident severity level.")
    action_type: ActionType = Field(description="Whitelisted action type.")
    target_entity: str = Field(description="Target host, VM, or job identifier.")
    cli_command: str = Field(description="CLI command to execute or communicate.")
    reasoning: str = Field(description="Short rationale for the chosen action.")

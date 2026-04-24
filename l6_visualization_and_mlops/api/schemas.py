"""Pydantic schemas for L6 REST API."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class IncidentResponse(BaseModel):
    """Serialized incident payload for API responses."""

    id: int
    timestamp: datetime
    root_cause_node_type: str
    root_cause_node_id: int
    anomaly_score: float
    playbook_summary: str
    playbook_cli_command: str
    attention_weights_json: str
    is_confirmed_by_engineer: bool | None

    model_config = ConfigDict(from_attributes=True)


class FeedbackRequest(BaseModel):
    """Request payload used to provide engineer feedback."""

    is_confirmed: bool = Field(
        ...,
        description="True if incident is confirmed by engineer, otherwise False.",
    )

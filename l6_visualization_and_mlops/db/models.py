"""ORM models for experience replay storage."""

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from l6_visualization_and_mlops.db.database import Base


class IncidentRecord(Base):
    """Database model for one incident received from L5."""

    __tablename__ = "incident_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    timestamp: Mapped[DateTime] = mapped_column(DateTime, nullable=False, index=True)
    root_cause_node_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    root_cause_node_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    anomaly_score: Mapped[float] = mapped_column(Float, nullable=False)
    playbook_summary: Mapped[str] = mapped_column(String(1024), nullable=False)
    playbook_cli_command: Mapped[str] = mapped_column(String(2048), nullable=False)
    attention_weights_json: Mapped[str] = mapped_column(Text, nullable=False)
    is_confirmed_by_engineer: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)

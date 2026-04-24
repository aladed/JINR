"""FastAPI routes for incident listing and feedback updates."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from l6_visualization_and_mlops.api.schemas import FeedbackRequest, IncidentResponse
from l6_visualization_and_mlops.db.database import get_db
from l6_visualization_and_mlops.db.models import IncidentRecord

router = APIRouter(prefix="/api", tags=["incidents"])


@router.get("/incidents", response_model=list[IncidentResponse])
def get_recent_incidents(db: Session = Depends(get_db)) -> list[IncidentResponse]:
    """Return latest 50 incidents for dashboard visualization.

    Args:
        db: Database session dependency.

    Returns:
        list[IncidentResponse]: Recent incidents sorted by newest first.
    """

    records = (
        db.query(IncidentRecord)
        .order_by(IncidentRecord.timestamp.desc(), IncidentRecord.id.desc())
        .limit(50)
        .all()
    )
    return [IncidentResponse.model_validate(record) for record in records]


@router.post("/incidents/{incident_id}/feedback", response_model=IncidentResponse)
def provide_feedback(
    incident_id: int,
    request: FeedbackRequest,
    db: Session = Depends(get_db),
) -> IncidentResponse:
    """Update engineer confirmation feedback for an incident.

    Args:
        incident_id: Target incident primary key.
        request: Feedback payload with confirmation flag.
        db: Database session dependency.

    Raises:
        HTTPException: If incident does not exist.

    Returns:
        IncidentResponse: Updated incident record.
    """

    record = db.query(IncidentRecord).filter(IncidentRecord.id == incident_id).first()
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Incident {incident_id} not found.",
        )

    record.is_confirmed_by_engineer = request.is_confirmed
    db.commit()
    db.refresh(record)
    return IncidentResponse.model_validate(record)

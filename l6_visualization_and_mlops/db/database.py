"""Database session and engine setup for L6 backend."""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from l6_visualization_and_mlops.core.config import get_settings

settings = get_settings()

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db() -> Generator[Session, None, None]:
    """Yield a transactional SQLAlchemy session for request scope.

    Yields:
        Session: SQLAlchemy session bound to the configured engine.
    """

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

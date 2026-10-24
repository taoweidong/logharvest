from pathlib import Path

from sqlmodel import Session, select

from src.api.v1.schemas import DataSourceCreate
from src.domain.models.entities import DataSource


def create_data_source(session: Session, payload: DataSourceCreate) -> DataSource:
    source = DataSource(**payload.model_dump())
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def list_data_sources(session: Session) -> list[DataSource]:
    return list(session.exec(select(DataSource).order_by(DataSource.id)).all())


def test_data_source_path(source: DataSource) -> dict[str, bool]:
    path = Path(source.root_path)
    return {
        "exists": path.exists(),
        "is_dir": path.is_dir(),
        "readable": path.is_dir() and path.stat().st_mode & 0o444 != 0,
    }

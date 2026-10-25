from pathlib import Path

from sqlmodel import Session, select

from src.api.v1.schemas import DataSourceCreate
from src.domain.models.entities import DataSource
from src.domain.source_guard import READ_ONLY, detect_writability


def create_data_source(session: Session, payload: DataSourceCreate) -> DataSource:
    source = DataSource(**payload.model_dump())
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def list_data_sources(session: Session, limit: int = 50, offset: int = 0) -> list[DataSource]:
    statement = select(DataSource).order_by(DataSource.id).offset(offset).limit(limit)
    return list(session.exec(statement).all())


def test_data_source_path(source: DataSource) -> dict[str, object]:
    path = Path(source.root_path)
    writability = detect_writability(path)
    return {
        "exists": path.exists(),
        "is_dir": path.is_dir(),
        "readable": path.is_dir() and path.stat().st_mode & 0o444 != 0,
        # 三态判定；read_only_confirmed 只有在真的拿到只读证据时才为真。
        "writability": writability,
        "read_only_confirmed": writability == READ_ONLY,
    }

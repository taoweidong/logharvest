from sqlalchemy import func
from sqlmodel import Session, select

from src.domain.models.entities import RuleSet
from src.domain.rules.models import RuleSetConfig


def create_rule_set_version(
    session: Session,
    name: str,
    config: RuleSetConfig,
    created_by: str,
) -> RuleSet:
    max_version = session.exec(select(func.max(RuleSet.version)).where(RuleSet.name == name)).one()
    version = (max_version or 0) + 1
    rule_set = RuleSet(
        name=name,
        version=version,
        config=config.model_dump(mode="json"),
        created_by=created_by,
    )
    session.add(rule_set)
    session.commit()
    session.refresh(rule_set)
    return rule_set


def list_rule_sets(session: Session) -> list[RuleSet]:
    return list(session.exec(select(RuleSet).order_by(RuleSet.name, RuleSet.version)).all())

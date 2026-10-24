import codecs

from pathspec import PathSpec
from sqlalchemy import func
from sqlmodel import Session, select

from src.domain.models.entities import RuleSet
from src.domain.rules.evaluator import compile_ruleset
from src.domain.rules.models import RuleSetConfig


def validate_rule_set_config(config: RuleSetConfig) -> None:
    """Validate a rule set against the runtime compiler and worker inputs.

    Raises ``ValueError`` for invalid regex/flags/glob/threshold field and
    ``LookupError`` for unknown encodings. ``pathspec`` pattern errors are
    ``ValueError`` subclasses and are re-raised as ``ValueError``.
    """
    compile_ruleset(config)
    try:
        PathSpec.from_lines("gitwildmatch", [config.file_glob])
    except ValueError as exc:
        raise ValueError(f"invalid file_glob {config.file_glob!r}: {exc}") from exc
    for field_name in ("encoding", "fallback_encoding"):
        try:
            codecs.lookup(getattr(config, field_name))
        except LookupError as exc:
            raise LookupError(
                f"unknown {field_name} {getattr(config, field_name)!r}"
            ) from exc


def create_rule_set_version(
    session: Session,
    name: str,
    config: RuleSetConfig,
    created_by: str,
) -> RuleSet:
    validate_rule_set_config(config)
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


def list_rule_sets(session: Session, limit: int = 50, offset: int = 0) -> list[RuleSet]:
    statement = (
        select(RuleSet)
        .order_by(RuleSet.name, RuleSet.version)
        .offset(offset)
        .limit(limit)
    )
    return list(session.exec(statement).all())

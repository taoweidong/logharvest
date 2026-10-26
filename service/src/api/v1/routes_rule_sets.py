from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError

from src.api.dependencies import SessionDep, require_permission
from src.api.v1.schemas import RuleSetCreate, RuleSetDetailRead, RuleSetRead
from src.application.services.rule_sets import create_rule_set_version, list_rule_sets
from src.domain.models.auth import User
from src.domain.models.entities import RuleSet
from src.domain.permissions import Permissions

router = APIRouter(prefix="/rule-sets", tags=["rule-sets"])


@router.get("", response_model=list[RuleSetRead])
def get_rule_sets(
    session: SessionDep,
    _: Annotated[User, Depends(require_permission(Permissions.RULESETS_READ))],
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    return list_rule_sets(session, limit=limit, offset=offset)


@router.get("/{rule_set_id}", response_model=RuleSetDetailRead)
def get_rule_set(
    rule_set_id: int,
    session: SessionDep,
    _: Annotated[User, Depends(require_permission(Permissions.RULESETS_READ))],
):
    rule_set = session.get(RuleSet, rule_set_id)
    if rule_set is None:
        raise HTTPException(status_code=404, detail="rule set not found")
    return rule_set


@router.post("", response_model=RuleSetRead, status_code=201)
def post_rule_set(
    payload: RuleSetCreate,
    session: SessionDep,
    user: Annotated[User, Depends(require_permission(Permissions.RULESETS_WRITE))],
):
    try:
        return create_rule_set_version(session, payload.name, payload.config, user.username)
    except (ValueError, LookupError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="rule set version conflict") from exc

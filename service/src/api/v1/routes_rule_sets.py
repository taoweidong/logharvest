from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import IntegrityError

from src.api.dependencies import AdminDep, SessionDep
from src.api.v1.schemas import RuleSetCreate, RuleSetRead
from src.application.services.rule_sets import create_rule_set_version, list_rule_sets

router = APIRouter(prefix="/rule-sets", tags=["rule-sets"])


@router.get("", response_model=list[RuleSetRead])
def get_rule_sets(session: SessionDep, _: AdminDep):
    return list_rule_sets(session)


@router.post("", response_model=RuleSetRead, status_code=201)
def post_rule_set(payload: RuleSetCreate, session: SessionDep, admin: AdminDep):
    try:
        return create_rule_set_version(session, payload.name, payload.config, admin)
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="rule set version conflict") from exc

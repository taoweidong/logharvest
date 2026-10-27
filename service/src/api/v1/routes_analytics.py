"""只读分析接口：任务统计（§8）与跨版本规则索引（§9）。

两者都是"读聚合"，不写库、不占请求级会话之外的资源，所以沿用业务接口的纯 REST
形状：统计返回单个对象，规则索引返回裸数组无 total（窗口翻页）。
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from src.api.dependencies import SessionDep, require_permission
from src.application.schemas import JobStatsRead, RuleIndexRead
from src.application.services.analytics import job_stats, rule_index
from src.domain.models.auth import User
from src.domain.permissions import Permissions

router = APIRouter(tags=["analytics"])

StatsReadDep = Annotated[User, Depends(require_permission(Permissions.JOBS_READ))]
RuleIndexReadDep = Annotated[User, Depends(require_permission(Permissions.RULESETS_READ))]


@router.get("/stats/jobs", response_model=JobStatsRead)
def get_job_stats(
    session: SessionDep,
    _: StatsReadDep,
    days: int = Query(default=14, ge=1, le=90),
    data_source_id: int | None = Query(default=None, ge=1),
):
    """窗口内作业/文件/命中的聚合，日界为 UTC。

    窗口上限 90 天不是拍脑袋：`daily` 每天一行、`top_rules` 走全窗口命中，
    再宽就得靠物化视图或分区裁剪，那是另一件事（设计 §15）。
    """
    return job_stats(session, days=days, data_source_id=data_source_id)


@router.get("/rules", response_model=list[RuleIndexRead])
def get_rule_index(
    session: SessionDep,
    _: RuleIndexReadDep,
    keyword: str | None = Query(default=None),
    match_type: Literal["keyword", "regex"] | None = Query(default=None, alias="type"),
    rule_set_name: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    # 查询键叫 type（前端与权限码语义一致），函数形参避开内置 type。
    return rule_index(
        session,
        keyword=keyword,
        type_=match_type,
        rule_set_name=rule_set_name,
        limit=limit,
        offset=offset,
    )

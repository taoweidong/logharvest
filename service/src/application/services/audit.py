"""操作日志服务：写入与查询。

这里只负责"留痕"和"按窗口读回来"，不参与任何业务判断。两条口径来自设计 §6：

* :func:`record_operation` 只做 ``session.add``，**不提交**——审计行必须和触发它的
  业务写落在同一个事务里，否则业务回滚了日志却留下一条假案；
* 写审计的任何失败都吞掉（转 structlog 告警）：留痕不完整可以补，业务因为审计挂掉
  是不可接受的。
"""

from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import update
from sqlmodel import Session, select

from src.domain.models.audit import OperationLog
from src.domain.models.auth import User
from src.domain.text import escape_like

_audit_logger = structlog.get_logger(__name__)


def record_operation(
    session: Session,
    *,
    actor: User | None,
    action: str,
    resource_type: str = "",
    resource_id: str | int | None = None,
    summary: str,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
    role_hint: str = "",
    actor_name: str | None = None,
) -> None:
    """追加一行审计记录；调用方负责提交（登录类端点用 :func:`commit_operation`）。

    ``actor_name`` 是给"没有可用 User 对象"的事件留的后门：登录失败时库里可能压根没有
    这个账号，但审计必须记下被尝试的用户名，否则失败记录只是一串匿名 401。
    ``detail`` 只放业务字段摘要——口令一类敏感值永不调用方写入。
    """
    try:
        row = OperationLog(
            actor_id=actor.id if actor is not None else None,
            actor_name=(
                actor_name
                if actor_name is not None
                else (actor.username if actor is not None else "")
            ),
            role_hint=role_hint,
            action=action,
            resource_type=resource_type,
            resource_id="" if resource_id is None else str(resource_id),
            summary=summary,
            detail=dict(detail or {}),
            origin_ip=ip or "",
        )
        session.add(row)
    except Exception as exc:  # noqa: BLE001  — 审计失败不得影响业务，只告警
        # 只记录动作名与异常类型：detail 里可能含业务数据，不写进应用日志。
        _audit_logger.warning("operation_log_record_failed", action=action, error=str(exc))


def commit_operation(session: Session) -> None:
    """提交刚追加的审计行，用于业务已经自行提交的端点。

    创建类端点的新资源 id 只有在业务 ``commit`` 之后才拿得到，审计只能自己收尾；
    此时提交失败绝不能把已经成功的业务变成 5xx。
    """
    try:
        session.commit()
    except Exception as exc:  # noqa: BLE001  — 同上：留痕失败不改变业务结果
        session.rollback()
        _audit_logger.warning("operation_log_commit_failed", error=str(exc))


def detach_actor(session: Session, *, actor_id: int) -> None:
    """把某账号留下的审计行 ``actor_id`` 置空（用户名快照保留）。

    ``users.id`` 的外键是 ``ON DELETE SET NULL``，但 SQLite 默认不启用外键约束
    （``PRAGMA foreign_keys`` 未开，全仓库也没有开过），删号后那些 ``actor_id`` 会
    变成悬空引用，按人过滤时把死账号当成还在职。删除路径因此显式置空一次；
    PostgreSQL 已由外键处理，这里的 UPDATE 匹配不到任何行，重复执行无害。
    """
    try:
        # 单条 UPDATE 代替"查出全部行再逐行置空"：账号的审计行可能成千上万，
        # 逐行 ORM 写会把删除一次账号变成一次全表读 + N 条 UPDATE。
        session.execute(
            update(OperationLog).where(OperationLog.actor_id == actor_id).values(actor_id=None)
        )
    except Exception as exc:  # noqa: BLE001  — 审计整理失败不得影响业务删除结果
        _audit_logger.warning("operation_log_detach_failed", error=str(exc))


def list_operation_logs(
    session: Session,
    *,
    limit: int,
    offset: int,
    actor_id: int | None,
    action: str | None,
    resource_type: str | None,
    resource_id: str | None,
    created_from: datetime | None,
    created_to: datetime | None,
) -> list[OperationLog]:
    """按窗口读回审计行；``id`` 作次级排序，保证同一秒内的顺序稳定可翻页。

    ``action`` 是前缀匹配：``user.`` 一次拿到整族用户侧动作，这是页面筛选的主要用法。
    时间过滤成对生效——半个区间没有意义，参数校验（422）在路由层。
    """
    statement = select(OperationLog).order_by(
        OperationLog.created_at.desc(), OperationLog.id.desc()
    )
    if actor_id is not None:
        statement = statement.where(OperationLog.actor_id == actor_id)
    term = (action or "").strip()
    if term:
        statement = statement.where(OperationLog.action.ilike(f"{escape_like(term)}%", escape="\\"))
    if resource_type:
        statement = statement.where(OperationLog.resource_type == resource_type)
    if resource_id:
        statement = statement.where(OperationLog.resource_id == str(resource_id))
    if created_from is not None and created_to is not None:
        statement = statement.where(
            OperationLog.created_at >= created_from,
            OperationLog.created_at <= created_to,
        )
    return list(session.exec(statement.offset(offset).limit(limit)).all())

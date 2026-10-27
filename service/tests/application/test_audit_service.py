"""audit 服务的异常分支与查询矩阵。

留痕失败"吞掉并告警"的三条分支（record/commit/detach）是设计意图：
审计不完整可以补，业务因审计挂掉不可接受。这里用 structlog 的 capture_logs
验证"确实告警了、确实没抛"，防止将来有人把 except 改成 raise 而无测试发现。
"""

import structlog
from sqlmodel import select

from src.application.services import audit as audit_service
from src.application.services.audit import (
    commit_operation,
    detach_actor,
    list_operation_logs,
    record_operation,
)
from src.application.services.auth import ensure_admin
from src.domain.models.audit import OperationLog


def _record(session, **overrides) -> None:
    kwargs = {
        "actor": None,
        "action": "user.create",
        "summary": "创建用户",
    }
    kwargs.update(overrides)
    record_operation(session, **kwargs)


def test_record_operation_swallows_construction_failure(session, monkeypatch) -> None:
    class BrokenOperationLog:
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("db schema drift")

    monkeypatch.setattr(audit_service, "OperationLog", BrokenOperationLog)
    with structlog.testing.capture_logs() as logs:
        _record(session)  # 不抛
    warnings = [log for log in logs if log["event"] == "operation_log_record_failed"]
    assert len(warnings) == 1
    assert warnings[0]["action"] == "user.create"
    assert "db schema drift" in warnings[0]["error"]

    # 告警之外，业务会话保持可用（没有残留半提交状态）。
    assert session.exec(select(OperationLog)).all() == []


def test_commit_operation_swallows_and_rolls_back(session, monkeypatch) -> None:
    _record(session)
    calls = {"rollback": 0}

    def broken_commit() -> None:
        raise RuntimeError("connection reset")

    def counting_rollback() -> None:
        calls["rollback"] += 1

    monkeypatch.setattr(session, "commit", broken_commit)
    monkeypatch.setattr(session, "rollback", counting_rollback)

    with structlog.testing.capture_logs() as logs:
        commit_operation(session)  # 不抛

    assert calls["rollback"] == 1
    assert [log["event"] for log in logs if "operation_log_commit_failed" in log["event"]]


def test_detach_actor_nulls_actor_id_but_keeps_name_snapshot(session) -> None:
    admin = ensure_admin(session, "admin", "Test-Admin-Pass")
    _record(session, actor=admin, action="user.create")
    rows = session.exec(select(OperationLog)).all()
    assert len(rows) == 1 and rows[0].actor_id == admin.id

    detach_actor(session, actor_id=admin.id)
    session.commit()
    session.expire_all()
    row = session.exec(select(OperationLog)).one()
    assert row.actor_id is None
    assert row.actor_name == "admin"


def test_detach_actor_swallows_failure(session, monkeypatch) -> None:
    def broken_execute(*args, **kwargs):
        raise RuntimeError("lock timeout")

    monkeypatch.setattr(session, "execute", broken_execute)
    with structlog.testing.capture_logs() as logs:
        detach_actor(session, actor_id=123)  # 不抛
    assert any(
        log["event"] == "operation_log_detach_failed" and "lock timeout" in log["error"]
        for log in logs
    )


def test_list_operation_logs_filter_matrix(session) -> None:
    admin = ensure_admin(session, "admin", "Test-Admin-Pass")
    _record(session, actor=admin, action="user.create", resource_type="user", resource_id="1")
    _record(session, actor=admin, action="role.create", resource_type="role", resource_id="2")
    _record(session, actor=admin, action="role.assign_menus", resource_type="role")
    session.commit()

    # action 前缀族
    roles = list_operation_logs(
        session,
        limit=50,
        offset=0,
        actor_id=None,
        action="role.",
        resource_type=None,
        resource_id=None,
        created_from=None,
        created_to=None,
    )
    assert {row.action for row in roles} == {"role.create", "role.assign_menus"}

    # LIKE 通配符按字面量处理：查询 "role.%" 不该命中任何行
    wildcard = list_operation_logs(
        session,
        limit=50,
        offset=0,
        actor_id=None,
        action="role.%p%",
        resource_type=None,
        resource_id=None,
        created_from=None,
        created_to=None,
    )
    assert wildcard == []

    # resource_type / resource_id 过滤
    by_type = list_operation_logs(
        session,
        limit=50,
        offset=0,
        actor_id=None,
        action=None,
        resource_type="role",
        resource_id=None,
        created_from=None,
        created_to=None,
    )
    assert len(by_type) == 2
    by_resource = list_operation_logs(
        session,
        limit=50,
        offset=0,
        actor_id=None,
        action=None,
        resource_type=None,
        resource_id="2",
        created_from=None,
        created_to=None,
    )
    assert [row.action for row in by_resource] == ["role.create"]

    # created_from/created_to 成对过滤
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    windowed = list_operation_logs(
        session,
        limit=50,
        offset=0,
        actor_id=None,
        action=None,
        resource_type=None,
        resource_id=None,
        created_from=now - timedelta(seconds=5),
        created_to=now + timedelta(seconds=5),
    )
    assert len(windowed) == 3

    # actor_id 过滤
    by_actor = list_operation_logs(
        session,
        limit=50,
        offset=0,
        actor_id=admin.id,
        action=None,
        resource_type=None,
        resource_id=None,
        created_from=None,
        created_to=None,
    )
    assert len(by_actor) == 3

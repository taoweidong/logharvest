"""只读聚合服务：任务统计（设计 §8）与跨版本规则索引（设计 §9）。

与 ``queries.py`` 的分工：那边是单表列表/游标查询，这里只做"从作业视角看批量结果"
的多表聚合，以及把规则从 ``rule_sets.config`` 里展平成可检索的索引行。全部走同步
SQLAlchemy/SQLModel，项目内没有任何 async 会话。
"""

from datetime import date, datetime, timedelta
from enum import StrEnum

from pydantic import ValidationError
from sqlalchemy import case, distinct, func
from sqlmodel import Session, select

from src.application.schemas import (
    JobStatsRead,
    RuleIndexRead,
    StatsDailyRead,
    StatsTotalsRead,
    TopRuleRead,
)
from src.domain.models.entities import FileTask, Job, Match, RuleSet, utcnow
from src.domain.rules.models import Rule, RuleSetConfig
from src.domain.text import escape_like

TOP_RULES_LIMIT = 10
REGEX_SUMMARY_MAX_CHARS = 80


class JobStatus(StrEnum):
    """作业状态全集：既是统计的分类口径，也是 ``GET /jobs?status=`` 的枚举门。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _date_key(value: object) -> str:
    """把 ``func.date`` 的返回值归一成 ``YYYY-MM-DD`` 字符串。

    同一表达式在两种方言下宿主类型不同：SQLite 直接回文本串，PostgreSQL 的 ``date``
    经 psycopg 适配成 ``datetime.date``。归日后要能跨方言比对，必须收敛到同一表示。
    """
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _as_int(value: object) -> int:
    """聚合列在空结果集上是 ``None``（SQLite/PG 一致），统一落成 0。"""
    return int(value or 0)


def _job_base_filters(cutoff: datetime, data_source_id: int | None) -> list[object]:
    # 窗口只按 jobs.created_at 归集；data_source_id 作为可选收窄。
    filters: list[object] = [Job.created_at >= cutoff]
    if data_source_id is not None:
        filters.append(Job.data_source_id == data_source_id)
    return filters


def job_stats(session: Session, *, days: int, data_source_id: int | None) -> JobStatsRead:
    """按 UTC 日界聚合窗口内作业、文件任务与命中数（设计 §8 口径）。

    窗口口径严格对齐 §8：以 ``jobs.created_at >= now-utc days`` 界定作业集合，
    ``file_tasks``/``matches`` 一律经其所属作业计入（不是文件/命中自己的时间），
    这样 daily 的作业数与文件/命中数落在同一天、彼此自洽。不做缓存、不建物化视图。
    """
    cutoff = utcnow() - timedelta(days=days)
    base = _job_base_filters(cutoff, data_source_id)

    jobs_total = _as_int(session.exec(select(func.count()).select_from(Job).where(*base)).one())
    status_counts = {
        str(status): int(count)
        for status, count in session.exec(
            select(Job.status, func.count()).where(*base).group_by(Job.status)
        ).all()
    }
    files_total, bytes_total, file_succeeded, file_failed = session.exec(
        select(
            func.count(FileTask.id),
            func.sum(FileTask.size_bytes),
            func.sum(case((FileTask.status == JobStatus.SUCCEEDED.value, 1), else_=0)),
            func.sum(case((FileTask.status == JobStatus.FAILED.value, 1), else_=0)),
        )
        .select_from(FileTask)
        .join(Job, FileTask.job_id == Job.id)
        .where(*base)
    ).one()
    matched_rows_total = _as_int(
        session.exec(
            select(func.count()).select_from(Match).join(Job, Match.job_id == Job.id).where(*base)
        ).one()
    )

    totals = StatsTotalsRead(
        jobs=jobs_total,
        succeeded=status_counts.get(JobStatus.SUCCEEDED.value, 0),
        partial=status_counts.get(JobStatus.PARTIAL.value, 0),
        failed=status_counts.get(JobStatus.FAILED.value, 0),
        cancelled=status_counts.get(JobStatus.CANCELLED.value, 0),
        running=status_counts.get(JobStatus.RUNNING.value, 0),
        files_total=_as_int(files_total),
        bytes_total=_as_int(bytes_total),
        matched_rows_total=matched_rows_total,
        file_succeeded=_as_int(file_succeeded),
        file_failed=_as_int(file_failed),
    )

    daily = _daily_breakdown(session, base)
    top_rules = _top_rules(session, base)
    return JobStatsRead(
        days=days,
        data_source_id=data_source_id,
        totals=totals,
        daily=daily,
        top_rules=top_rules,
    )


def _daily_breakdown(session: Session, base: list[object]) -> list[StatsDailyRead]:
    """按 ``jobs.created_at`` 的 UTC 日归桶，把文件/命中指标回卷到作业创建日。

    分三段聚合再在 Python 侧合并，而不是把 jobs/file_tasks/matches 三者 JOIN 到一起：
    三表连接会按"文件×命中"笛卡尔放大，把文件数与字节数重复计入。分段查询天然无扇出。
    """
    day = func.date(Job.created_at)
    jobs_per_day = {
        _date_key(bucket): int(count)
        for bucket, count in session.exec(
            select(day, func.count()).where(*base).group_by(day)
        ).all()
    }
    files_per_day: dict[str, tuple[int, int, int]] = {}
    for bucket, succeeded, failed, size_sum in session.exec(
        select(
            day,
            func.sum(case((FileTask.status == JobStatus.SUCCEEDED.value, 1), else_=0)),
            func.sum(case((FileTask.status == JobStatus.FAILED.value, 1), else_=0)),
            func.sum(FileTask.size_bytes),
        )
        .select_from(FileTask)
        .join(Job, FileTask.job_id == Job.id)
        .where(*base)
        .group_by(day)
    ).all():
        files_per_day[_date_key(bucket)] = (
            _as_int(succeeded),
            _as_int(failed),
            _as_int(size_sum),
        )
    matched_per_day = {
        _date_key(bucket): int(count)
        for bucket, count in session.exec(
            select(day, func.count())
            .select_from(Match)
            .join(Job, Match.job_id == Job.id)
            .where(*base)
            .group_by(day)
        ).all()
    }

    # 某日只要有作业就一定进 jobs_per_day；文件/命中经作业连接，其日界必是作业日界的
    # 子集，所以以作业日为键集合并即可，缺的文件/命中桶按 0 计。
    daily: list[StatsDailyRead] = []
    for key in sorted(jobs_per_day):
        files_succeeded, files_failed, bytes_total = files_per_day.get(key, (0, 0, 0))
        daily.append(
            StatsDailyRead(
                date=key,
                jobs_created=jobs_per_day[key],
                files_succeeded=files_succeeded,
                files_failed=files_failed,
                matched_rows=matched_per_day.get(key, 0),
                bytes_total=bytes_total,
            )
        )
    return daily


def _top_rules(session: Session, base: list[object]) -> list[TopRuleRead]:
    """窗口内按 ``(rule_id, rule_version)`` 聚合命中数与涉及作业数，取前 10。

    排序命中数优先，命中数相等再按 rule_id/version 稳定，避免翻页抖动。
    """
    hits = func.count()
    rows = session.exec(
        select(
            Match.rule_id,
            Match.rule_version,
            hits,
            func.count(distinct(Match.job_id)),
        )
        .join(Job, Match.job_id == Job.id)
        .where(*base)
        .group_by(Match.rule_id, Match.rule_version)
        .order_by(hits.desc(), Match.rule_id, Match.rule_version)
        .limit(TOP_RULES_LIMIT)
    ).all()
    return [
        TopRuleRead(rule_id=rule_id, rule_version=version, hits=int(hit), jobs=int(jobs))
        for rule_id, version, hit, jobs in rows
    ]


def rule_index(
    session: Session,
    *,
    keyword: str | None,
    type_: str | None,
    rule_set_name: str | None,
    limit: int,
    offset: int,
) -> list[RuleIndexRead]:
    """把各版本 ``rule_sets.config`` 展平成只读规则索引（设计 §9）。

    规则不落新表，跨版本检索是查询期解析 JSON；当前版本量级下 Python 侧展平可接受，
    代价记在 §15。``type``/``keyword`` 命中规则内容而非列，故在展平后精确匹配（子串
    匹配天然把 ``%``/``_`` 当字面量）；``rule_set_name`` 是真实列，走转义后的 SQL LIKE。
    """
    statement = select(RuleSet.id, RuleSet.name, RuleSet.version, RuleSet.config)
    if rule_set_name:
        # 列过滤用 LIKE：先转义，用户输入的 %/_ 才是字面量而非通配。
        statement = statement.where(
            RuleSet.name.like(f"%{escape_like(rule_set_name)}%", escape="\\")
        )

    term = (keyword or "").strip().lower()
    items: list[RuleIndexRead] = []
    for rule_set_id, name, version, config in session.exec(statement).all():
        try:
            parsed = RuleSetConfig.model_validate(config)
        except ValidationError:
            # 单个历史脏配置不该拖垮整张跨版本索引：跳过无法解析的规则集。
            continue
        for rule in parsed.rules:
            if type_ is not None and rule.match.type != type_:
                continue
            if term and term not in _rule_searchable(rule):
                continue
            items.append(
                RuleIndexRead(
                    rule_set_id=rule_set_id,
                    rule_set_name=name,
                    version=version,
                    rule_id=rule.id,
                    type=rule.match.type,
                    severity=rule.severity,
                    summary=_rule_summary(rule),
                )
            )

    # 稳定排序：名称升序、版本降序（新版本在前）、rule_id 升序，再在展平结果上分页。
    items.sort(key=lambda item: (item.rule_set_name, -item.version, item.rule_id))
    return items[offset : offset + limit]


def _rule_summary(rule: Rule) -> str:
    """一行人类可读摘要：keyword 取关键字串（前 3 + 总数），regex 取 pattern（截断）。"""
    match = rule.match
    if match.type == "keyword":
        words = list(match.any) + list(match.all)
        summary = f"关键字: {', '.join(words[:3])}"
        if len(words) > 3:
            summary += f" 等 {len(words)} 个"
    else:
        pattern = match.pattern
        if len(pattern) > REGEX_SUMMARY_MAX_CHARS:
            pattern = pattern[:REGEX_SUMMARY_MAX_CHARS] + "…"
        summary = f"正则: {pattern}"
    if rule.threshold is not None:
        summary += f" · 阈值 {rule.threshold.field} {rule.threshold.op} {rule.threshold.value}"
    return summary


def _rule_searchable(rule: Rule) -> str:
    """展平后用于 keyword 子串匹配的可搜索文本：id/名称/标签 + 关键字或 pattern 原文。

    pattern 用未截断的原文（摘要里会被裁到 80 字符），否则长 pattern 的后半段搜不到。
    """
    parts = [rule.id, rule.name, *rule.tags]
    if rule.match.type == "keyword":
        parts.extend(rule.match.any)
        parts.extend(rule.match.all)
    else:
        parts.append(rule.match.pattern)
    return " ".join(part.lower() for part in parts if part)

"""analytics 服务的边界分支：日期归一、脏配置跳过、摘要截断。"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlmodel import Session

from src.application.services.analytics import (
    _date_key,
    _rule_summary,
    job_stats,
    rule_index,
)
from src.domain.models.entities import RuleSet
from src.domain.rules.models import (
    KeywordMatch,
    RegexMatch,
    Rule,
    RuleSetConfig,
    Threshold,
)


def test_date_key_normalizes_dialect_host_types() -> None:
    # datetime（psycopg 适配 date 的形态）、date（SQLite 文本经解析）、裸字符串三分支
    assert _date_key(datetime(2026, 10, 28, 12, 30, tzinfo=UTC)) == "2026-10-28"
    assert _date_key(datetime(2026, 10, 28).date()) == "2026-10-28"
    assert _date_key("2026-10-28") == "2026-10-28"


def test_job_stats_empty_window_is_all_zero(session: Session) -> None:
    stats = job_stats(session, days=7, data_source_id=None)
    assert stats.totals.jobs == 0
    assert stats.totals.files_total == 0
    assert stats.totals.matched_rows_total == 0
    assert stats.daily == []
    assert stats.top_rules == []


def test_rule_index_skips_unparseable_legacy_config(session: Session) -> None:
    good = RuleSet(
        name="good",
        version=1,
        config=RuleSetConfig(
            name="good",
            file_glob="**/*.log",
            rules=[Rule(id="e", match=KeywordMatch(type="keyword", any=["ERROR"]))],
        ).model_dump(mode="json"),
    )
    # 历史脏数据：缺 rules 字段，解析失败但不该拖垮整张索引
    bad = RuleSet(name="bad", version=1, config={"name": "bad", "file_glob": "**/*.log"})
    session.add(good)
    session.add(bad)
    session.commit()

    items = rule_index(session, keyword=None, type_=None, rule_set_name=None, limit=50, offset=0)
    assert [item.rule_set_name for item in items] == ["good"]


def _keyword_rule(rule_id: str, words: list[str]) -> Rule:
    return Rule(id=rule_id, match=KeywordMatch(type="keyword", any=words))


def test_rule_summary_keyword_condenses_over_three_words() -> None:
    rule = _keyword_rule("k", ["alpha", "beta", "gamma", "delta", "epsilon"])
    summary = _rule_summary(rule)
    assert summary == "关键字: alpha, beta, gamma 等 5 个"


def test_rule_summary_regex_truncates_long_pattern() -> None:
    pattern = "x" * 200
    rule = Rule(id="r", match=RegexMatch(type="regex", pattern=pattern))
    summary = _rule_summary(rule)
    assert summary.startswith("正则: ")
    assert summary.endswith("…")
    assert len(summary) < len(pattern)

    rule_with_threshold = Rule(
        id="rt",
        match=RegexMatch(
            type="regex",
            pattern=r"cpu=(?P<cpu>\d+)",
            flags="",
            timeout_ms=50,
        ),
        threshold=Threshold(field="cpu", op="gt", value=Decimal("90")),
    )
    summary = _rule_summary(rule_with_threshold)
    assert "阈值 cpu gt 90" in summary

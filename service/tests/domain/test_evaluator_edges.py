"""规则编译/求值的边界分支：标志位、阈值退化、防御性异常路径。"""

from decimal import Decimal

import pytest

from src.domain.rules import evaluator
from src.domain.rules.evaluator import (
    CompiledRule,
    CompiledRuleSet,
    _regex_flags,
    _threshold_matches,
    compile_ruleset,
    evaluate_line,
)
from src.domain.rules.models import (
    KeywordMatch,
    RegexMatch,
    Rule,
    RuleSetConfig,
    Threshold,
)


def test_regex_flags_map_m_and_s() -> None:
    assert _regex_flags("m", "r") & evaluator.regex.MULTILINE
    assert _regex_flags("s", "r") & evaluator.regex.DOTALL
    # 组合标志逐个置位，未知标志在 compile_ruleset 前就被拒。
    combined = _regex_flags("ims", "r")
    assert combined & evaluator.regex.IGNORECASE
    assert combined & evaluator.regex.MULTILINE
    assert combined & evaluator.regex.DOTALL


def _config(rule: Rule) -> RuleSetConfig:
    return RuleSetConfig(name="t", file_glob="**/*.log", rules=[rule])


def test_threshold_with_non_finite_value_never_matches() -> None:
    # pydantic 会拒绝 NaN，但运行时防御分支仍要兜住手工构造的退化输入。
    threshold = Threshold.model_construct(field="cpu", op="gt", value=Decimal("NaN"))
    matched, value = _threshold_matches(threshold, {"cpu": "99"})
    assert matched is False and value is None


def test_threshold_with_non_finite_field_value_never_matches() -> None:
    threshold = Threshold(field="cpu", op="gt", value=Decimal("90"))
    matched, value = _threshold_matches(threshold, {"cpu": "sNaN"})
    assert matched is False and value is None


def test_threshold_comparison_arithmetic_error_is_swallowed(monkeypatch) -> None:
    """防御分支：比较操作抛 ArithmeticError（如 sNaN 比较）时按"未命中"处理。"""

    def broken_op(_value: Decimal, _threshold: Decimal) -> bool:
        raise ArithmeticError("invalid comparison")

    monkeypatch.setitem(evaluator._OPS, "gt", broken_op)
    threshold = Threshold(field="cpu", op="gt", value=Decimal("90"))
    matched, value = _threshold_matches(threshold, {"cpu": "99"})
    assert matched is False and value is None


def test_evaluate_line_detects_uncompiled_regex() -> None:
    rule = Rule(
        id="r",
        match=RegexMatch(type="regex", pattern="ERROR"),
    )
    # 绕过 compile_ruleset 手工构造"正则未编译"的条目，锁防御断言。
    compiled = CompiledRuleSet(config=_config(rule), rules=(CompiledRule(rule=rule),))
    with pytest.raises(RuntimeError, match="regex match was not compiled"):
        evaluate_line(compiled, "ERROR boom", 1)


def test_evaluate_line_skips_unknown_match_kind() -> None:
    rule = Rule(id="r", match=KeywordMatch(type="keyword", any=["ERROR"]))

    # 手工构造一个 match 类型既非 keyword 分支产物、也非 regex 的条目：
    # 直接塞入 keyword 规则但用 monkeypatch 模拟"未知类型"不可行，
    # 这里用未启用规则之外的真实第三态——不存在的 match 种类由
    # CompiledRuleSet 的构造方（compile_ruleset）保证不产生，因此
    # 该防御分支只能通过伪造 CompiledRule 的 rule.match 触发。
    class FakeMatch:
        type = "future"

    fake = CompiledRule(rule=rule)
    object.__setattr__(fake.rule, "match", FakeMatch())  # Rule 是 pydantic 模型
    compiled = CompiledRuleSet(config=_config(rule), rules=(fake,))
    assert evaluate_line(compiled, "ERROR boom", 1) == []


def test_multiline_flag_changes_anchor_semantics() -> None:
    # 行为级验证：^/$ 在 MULTILINE 下逐行生效（flags='m' 的实际价值）。
    rule = Rule(
        id="m",
        match=RegexMatch(type="regex", pattern=r"^ERROR.*$", flags="m"),
    )
    compiled = compile_ruleset(_config(rule))
    assert evaluate_line(compiled, "INFO ok\nERROR boom", 1) != []


def test_dotall_flag_lets_dot_cross_newlines() -> None:
    rule = Rule(
        id="s",
        match=RegexMatch(type="regex", pattern=r"START.END", flags="s"),
    )
    compiled = compile_ruleset(_config(rule))
    assert evaluate_line(compiled, "START\nEND", 1) != []

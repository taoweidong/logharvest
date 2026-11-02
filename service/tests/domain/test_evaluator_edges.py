"""规则编译/求值的边界分支：标志位、阈值退化、防御性异常路径。"""

from decimal import Decimal

import pytest

from src.domain.rules import evaluator
from src.domain.rules.evaluator import (
    CompiledRule,
    CompiledRuleSet,
    _mandatory_literal,
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


# ------------------------------------------------- 必选字面量提取（hint 预筛输入）


def test_mandatory_run_treats_unterminated_quote_literal_as_text() -> None:
    # `\Q` 之后找不到 `\E`：剩余模式按字面量并入段（regex 宽容语法）。
    assert _mandatory_literal(r"\Qabc") == "abc"
    assert _mandatory_literal(r"\Qab\ncd") == "ab\\ncd"


def test_mandatory_run_skips_character_classes_and_keeps_both_sides() -> None:
    # 字符类中断当前段：两侧各自成段，取最长；未闭合的类扫到结尾同样中断。
    assert _mandatory_literal(r"x[0-9]yyy") == "yyy"
    assert _mandatory_literal(r"x[^0-9]yyy") == "yyy"
    assert _mandatory_literal(r"x[0-9") == "x"


def test_mandatory_run_treats_unterminated_brace_as_literal() -> None:
    # 无 closing `}` 的花括号不是量词（regex 宽容语法）：按字面量并入段。
    assert _mandatory_literal(r"abc{") == "abc{"


# ------------------------------------------------------- 空词表（绕过校验）路径


def test_empty_vocabulary_keyword_keeps_compile_and_eval_legacy_paths() -> None:
    """词表全空（any/all 都空，只能绕过 Pydantic 校验构造）的三处兼容路径。

    compile_ruleset 不打预编译针（keyword_needles 为 None）；索引把它归入 always；
    求值走手工路径，大小写不敏感时现算 line.lower()，最终保持既有的越界行为。
    """
    match = KeywordMatch.model_construct(type="keyword", any=[], all=[], case_sensitive=False)
    rule = Rule.model_construct(
        id="empty", name="", enabled=True, match=match, threshold=None, severity="info", tags=[]
    )
    compiled = compile_ruleset(_config(rule))

    assert compiled.rules[0].keyword_needles is None
    with pytest.raises(IndexError):
        evaluate_line(compiled, "anything", 1)


def test_empty_vocabulary_keyword_case_sensitive_skips_lowering() -> None:
    match = KeywordMatch.model_construct(type="keyword", any=[], all=[], case_sensitive=True)
    rule = Rule.model_construct(
        id="empty", name="", enabled=True, match=match, threshold=None, severity="info", tags=[]
    )
    compiled = compile_ruleset(_config(rule))
    with pytest.raises(IndexError):
        evaluate_line(compiled, "anything", 1)


def test_compile_ruleset_keeps_unknown_match_kind_uncompiled_and_always() -> None:
    """未知匹配类型（绕过校验）在编译期不打针、索引归 always、求值恒不命中。"""
    rule = Rule.model_construct(
        id="u", name="", enabled=True, match=object(), threshold=None, severity="info", tags=[]
    )
    compiled = compile_ruleset(_config(rule))

    assert compiled.rules[0].regex is None
    assert compiled.rules[0].keyword_needles is None
    assert evaluate_line(compiled, "text", 1) == []


def test_match_rule_lowers_line_when_called_directly_without_cached_lower() -> None:
    """_match_rule 的防御契约：line_lower=None 且规则不敏感时现算 line.lower()。

    evaluate_line 的两条路径（索引/无索引）都会预先算好并传入 lower 行，
    该分支只在直接调用 _match_rule 时可达——锁定其自包含语义。
    """
    rule = Rule(id="ci", match=KeywordMatch(type="keyword", any=["ERROR"]))
    match = evaluator._match_rule(CompiledRule(rule=rule), "an ERROR", 1, None)

    assert match is not None
    assert match.matched_text == "ERROR"

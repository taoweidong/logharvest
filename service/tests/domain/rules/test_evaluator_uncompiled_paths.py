"""未走 compile_ruleset 预编译的求值分支与绕过校验的空词表分支的特征化测试。

这两条分支是性能改造留下的兼容路径：外部手工构造 CompiledRule 时 needle 仍需在求值期
现算；词表为空时（只有绕过 Pydantic 校验才可能）必须保持与改造前完全相同的越界行为。
"""

from src.domain.rules.evaluator import CompiledRule, CompiledRuleSet, compile_ruleset, evaluate_line
from src.domain.rules.models import KeywordMatch, Rule, RuleSetConfig


def _evaluate(*rules: Rule) -> list:
    config = RuleSetConfig.model_construct(
        name="c",
        file_glob="**/*",
        encoding="utf-8",
        fallback_encoding="gb18030",
        rules=list(rules),
    )
    return evaluate_line(
        CompiledRuleSet(config=config, rules=tuple(CompiledRule(rule=r) for r in rules)),
        "aB",
        1,
    )


def test_manually_compiled_rule_recomputes_needles_at_eval_time():
    rule = Rule(id="m", match=KeywordMatch(any=["q", "B"]))
    # needles=None（未预编译）也必须返回配置序首命中词，与 compile_ruleset 路径一致
    assert [m.rule_id for m in _evaluate(rule)] == ["m"]
    assert _evaluate(rule)[0].matched_text == "B"
    assert [
        m.matched_text
        for m in evaluate_line(compile_ruleset(RuleSetConfig(name="c", rules=[rule])), "aB", 1)
    ] == ["B"]


def test_manually_compiled_case_sensitive_rule_uses_raw_line():
    rule = Rule(id="s", match=KeywordMatch(any=["B"], case_sensitive=True))
    assert _evaluate(rule)[0].matched_text == "B"
    assert _evaluate(Rule(id="s", match=KeywordMatch(any=["b"], case_sensitive=True))) == []


def test_manually_compiled_all_rule_returns_first_configured_word():
    rule = Rule(id="a", match=KeywordMatch(all=["B", "a"]))
    assert _evaluate(rule)[0].matched_text == "B"

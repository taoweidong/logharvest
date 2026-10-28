"""关键字规则求值的特征化测试（characterization tests）。

目的：为「编译期预计算关键字 needle + 每行只做一次 `line.lower()`」的纯性能重构
提供安全网。这里锁死的是既有可见语义，期望值全部按人工推导的字面量写出，
不由被测代码计算，因此重构前后都必须成立。

锁定的语义：
- any：返回配置顺序里第一个命中词的「原样配置词」，不是文本中最早出现的词。
- all：每个词都出现才算命中，返回 words[0]（成员判定，不计出现次数）。
- words = match.any or match.all 的回退优先级。
- 空串关键字合法且永远"出现"。
- 大小写不敏感使用 str.lower()（非 casefold），故 ß / Σ-ς / İ 行为保持原样。
- 规则顺序、每规则单条结果、禁用规则、正则与门限路径不变。
"""

from decimal import Decimal

from src.domain.rules.evaluator import compile_ruleset, evaluate_line
from src.domain.rules.models import (
    KeywordMatch,
    RegexMatch,
    Rule,
    RuleSetConfig,
    Threshold,
)


def eval_keyword(match: KeywordMatch, line: str, rule_id: str = "r") -> list:
    """编译单条关键字规则并求值一行，返回 RuleMatch 列表。"""
    config = RuleSetConfig(name="t", rules=[Rule(id=rule_id, match=match)])
    return evaluate_line(compile_ruleset(config), line, 1)


def matched_texts(match: KeywordMatch, line: str) -> list[str | None]:
    return [m.matched_text for m in eval_keyword(match, line)]


# --------------------------------------------------------------------------- any


def test_any_returns_first_configured_word_not_earliest_in_text() -> None:
    # "alpha" 在文本中出现更早，但配置顺序上 "zulu" 在前。
    assert matched_texts(KeywordMatch(any=["zulu", "alpha"]), "alpha zulu") == ["zulu"]


def test_any_returns_the_original_configured_word_when_case_insensitive() -> None:
    assert matched_texts(KeywordMatch(any=["Zulu", "ALPHA"]), "alpha zulu") == ["Zulu"]


def test_any_returns_original_word_when_lowering_changes_length() -> None:
    # İ (U+0130).lower() == "i\u0307"（i + 组合上点，2 个码点）：命中的是展开后的 needle，
    # 返回的必须仍是配置原词。
    assert matched_texts(KeywordMatch(any=["\u0130"]), "\u0130") == ["\u0130"]
    # 配置词比原词更短时也一样。
    assert matched_texts(KeywordMatch(any=["I"]), "\u0130") == ["I"]
    # 反向：小写 i 的配置词匹配不到单独的 i（原行是大写 İ 时才需要长度扩展）。
    assert eval_keyword(KeywordMatch(any=["\u0130"]), "i") == []


def test_any_skips_missing_words_and_returns_the_hit() -> None:
    assert matched_texts(KeywordMatch(any=["aaa", "bbb", "ccc"]), "zzz ccc yyy") == ["ccc"]


def test_any_is_substring_not_word_bounded() -> None:
    assert matched_texts(KeywordMatch(any=["ear"]), "heartless") == ["ear"]


def test_any_produces_at_most_one_match_per_rule() -> None:
    assert matched_texts(KeywordMatch(any=["aa", "bb"]), "aa bb AA") == ["aa"]


def test_any_without_hit_returns_no_match() -> None:
    assert eval_keyword(KeywordMatch(any=["error", "fatal"]), "all good") == []


# --------------------------------------------------------------------------- all


def test_all_returns_first_configured_word_regardless_of_text_order() -> None:
    assert matched_texts(KeywordMatch(all=["zulu", "alpha"]), "alpha zulu") == ["zulu"]


def test_all_returns_first_configured_word_when_case_insensitive() -> None:
    assert matched_texts(KeywordMatch(all=["ERROR", "timeout"]), "timeout error") == ["ERROR"]


def test_all_requires_every_word_to_occur() -> None:
    assert eval_keyword(KeywordMatch(all=["error", "timeout"]), "error only") == []


def test_all_uses_membership_not_occurrence_count() -> None:
    # 重复词只做成员判定：一次 "dup" 就满足两个相同的 needle。
    assert matched_texts(KeywordMatch(all=["dup", "dup"]), "dup") == ["dup"]
    assert matched_texts(KeywordMatch(all=["aa", "aa", "bb"]), "aa bb") == ["aa"]


def test_all_with_repeated_word_any_returns_the_first_hit() -> None:
    assert matched_texts(KeywordMatch(any=["nope", "dup", "dup"]), "dup dup") == ["dup"]


# ------------------------------------------------------------------- 空串关键字


def test_empty_string_keyword_any_always_occurs() -> None:
    assert matched_texts(KeywordMatch(any=[""]), "nothing here") == [""]
    assert matched_texts(KeywordMatch(any=["", "nope"]), "nothing here") == [""]


def test_empty_string_keyword_any_always_occurs_when_case_sensitive() -> None:
    assert matched_texts(KeywordMatch(any=[""], case_sensitive=True), "") == [""]


def test_empty_string_in_all_still_requires_the_other_words() -> None:
    assert matched_texts(KeywordMatch(all=["", "error"]), "an error") == [""]
    assert eval_keyword(KeywordMatch(all=["", "error"]), "nothing here") == []


def test_empty_string_is_checked_before_later_words_in_any() -> None:
    # "" 在配置顺序第一位，因此即使后面的词也命中，返回的仍是 ""。
    assert matched_texts(KeywordMatch(any=["", "error"]), "error") == [""]


# ------------------------------------------------------------- 大小写与 lower 语义


def test_case_sensitive_uses_the_original_line() -> None:
    assert matched_texts(KeywordMatch(any=["ERROR"], case_sensitive=True), "ERROR now") == ["ERROR"]
    assert eval_keyword(KeywordMatch(any=["ERROR"], case_sensitive=True), "error now") == []
    assert eval_keyword(KeywordMatch(any=["error"], case_sensitive=True), "ERROR now") == []


def test_case_insensitive_matches_both_cases_via_lower() -> None:
    assert matched_texts(KeywordMatch(any=["ERROR"]), "an error happened") == ["ERROR"]
    assert matched_texts(KeywordMatch(any=["error"]), "an ERROR happened") == ["error"]


def test_case_insensitive_uses_lower_not_casefold_for_sharp_s() -> None:
    # casefold 会把 ß 变成 ss，lower 不会：所以 "SS" 不匹配 "straße"，"ß" 匹配。
    assert eval_keyword(KeywordMatch(any=["SS"]), "straße") == []
    assert matched_texts(KeywordMatch(any=["ß"]), "straße") == ["ß"]
    assert matched_texts(KeywordMatch(any=["STRASSE"]), "strasse") == ["STRASSE"]
    assert eval_keyword(KeywordMatch(any=["STRASSE"]), "straße") == []


def test_case_insensitive_uses_lower_not_casefold_for_final_sigma() -> None:
    # "ΟΔΟΣ".lower() == "οδος"：词尾 Σ 依上下文降为 ς；casefold 不做该上下文判定。
    assert eval_keyword(KeywordMatch(any=["\u03c3"]), "\u039f\u0394\u039f\u03a3") == []
    assert matched_texts(KeywordMatch(any=["\u03c2"]), "\u039f\u0394\u039f\u03a3") == ["\u03c2"]
    # 孤立的 "Σ".lower() 得到 σ（无词首上下文，不触发词尾规则），因此不匹配 ΟΔΟΣ。
    assert eval_keyword(KeywordMatch(any=["\u03a3"]), "\u039f\u0394\u039f\u03a3") == []
    # 词中 Σ 降为普通 σ，两种配置词都能在 "ΣΤΙΓΜΗ".lower() == "στιγμη" 上命中。
    assert matched_texts(KeywordMatch(any=["\u03c3"]), "\u03a3\u03a4\u0399\u0393\u039c\u0397") == [
        "\u03c3"
    ]
    assert matched_texts(KeywordMatch(any=["\u03a3"]), "\u03a3\u03a4\u0399\u0393\u039c\u0397") == [
        "\u03a3"
    ]


def test_case_insensitive_uses_lower_not_casefold_for_ascii_only_lines() -> None:
    # 纯 ASCII 下 lower 与大小写无关命中一致，锁住"不区分大小写"这一侧的基线。
    assert matched_texts(KeywordMatch(any=["TimEdOut"]), "REQ timedOut 1ms") == ["TimEdOut"]


def test_case_sensitive_rules_do_not_leak_into_case_insensitive_and_vice_versa() -> None:
    # 同一行同时存在敏感与不敏感规则：每行只做一次 lower 也必须两边都正确。
    sensitive = Rule(id="s", match=KeywordMatch(any=["AB"], case_sensitive=True))
    insensitive = Rule(id="i", match=KeywordMatch(any=["ab"]))
    wrong_case = Rule(id="w", match=KeywordMatch(any=["XY"], case_sensitive=True))
    config = RuleSetConfig(name="t", rules=[sensitive, insensitive, wrong_case])
    matches = evaluate_line(compile_ruleset(config), "AB cd", 1)
    assert [m.rule_id for m in matches] == ["s", "i"]
    assert [m.matched_text for m in matches] == ["AB", "ab"]
    # 敏感规则在前，不能阻止后续不敏感规则拿到小写化的 haystack。
    config_flip = RuleSetConfig(name="t", rules=[insensitive, sensitive])
    matches_flip = evaluate_line(compile_ruleset(config_flip), "AB cd", 1)
    assert [m.matched_text for m in matches_flip] == ["ab", "AB"]


# ----------------------------------------------------------- any/all 回退优先级


def build_unvalidated(match: KeywordMatch, rule_id: str = "r"):
    """绕过 pydantic 校验构造 KeywordMatch/Rule/RuleSetConfig，用于锁回退分支。"""
    rule = Rule.model_construct(
        id=rule_id,
        name="",
        enabled=True,
        match=match,
        threshold=None,
        severity="info",
        tags=[],
    )
    return RuleSetConfig.model_construct(
        name="t",
        file_glob="**/*",
        encoding="utf-8",
        fallback_encoding="gb18030",
        rules=[rule],
    )


def test_any_takes_precedence_over_all_when_both_are_present() -> None:
    # words = match.any or match.all：any 非空时 all 被完全忽略。
    both = KeywordMatch.model_construct(
        type="keyword", any=["missing"], all=["error"], case_sensitive=False
    )
    compiled = compile_ruleset(build_unvalidated(both))
    assert evaluate_line(compiled, "an error", 1) == []

    both_hit = KeywordMatch.model_construct(
        type="keyword", any=["error"], all=["missing"], case_sensitive=False
    )
    compiled_hit = compile_ruleset(build_unvalidated(both_hit))
    matches = evaluate_line(compiled_hit, "an error", 1)
    assert [m.matched_text for m in matches] == ["error"]


def test_empty_any_falls_back_to_all() -> None:
    fallback = KeywordMatch.model_construct(
        type="keyword", any=[], all=["error", "timeout"], case_sensitive=False
    )
    compiled = compile_ruleset(build_unvalidated(fallback))
    matches = evaluate_line(compiled, "timeout and error", 1)
    assert [m.matched_text for m in matches] == ["error"]
    assert evaluate_line(compiled, "timeout only", 2) == []


# ------------------------------------------------------------------ 顺序与其他


def test_rule_order_and_single_result_per_rule_are_preserved() -> None:
    rules = [
        Rule(id="r1", match=KeywordMatch(any=["bb"])),
        Rule(id="r2", enabled=False, match=KeywordMatch(any=["aa"])),
        Rule(id="r3", match=KeywordMatch(all=["aa", "bb"])),
        Rule(id="r4", match=KeywordMatch(any=["zz"])),
        Rule(
            id="r5",
            match=RegexMatch(pattern=r"cost=(?P<cost>\d+)"),
            threshold=Threshold(field="cost", op="gt", value=Decimal(10)),
        ),
        Rule(id="r6", match=RegexMatch(pattern=r"cost=(?P<cost>\d+)")),
    ]
    compiled = compile_ruleset(RuleSetConfig(name="t", rules=rules))
    matches = evaluate_line(compiled, "aa bb cost=12", 8)
    assert [m.rule_id for m in matches] == ["r1", "r3", "r5", "r6"]
    assert [m.matched_text for m in matches] == ["bb", "aa", "cost=12", "cost=12"]
    assert [m.numeric_value for m in matches] == [None, None, Decimal(12), None]
    assert matches[2].fields == {"cost": "12"}


def test_keyword_rules_still_match_after_a_regex_rule_hit_or_miss() -> None:
    rules = [
        Rule(id="re", match=RegexMatch(pattern=r"^nope\d+$")),
        Rule(id="kw", match=KeywordMatch(any=["WARN"])),
        Rule(id="re2", match=RegexMatch(pattern=r"elapsed=(?P<ms>\d+)")),
    ]
    compiled = compile_ruleset(RuleSetConfig(name="t", rules=rules))
    matches = evaluate_line(compiled, "WARN elapsed=5", 1)
    assert [m.rule_id for m in matches] == ["kw", "re2"]
    assert matches[0].matched_text == "WARN"
    assert matches[1].fields == {"ms": "5"}


def test_empty_line_does_not_match_non_empty_keywords() -> None:
    assert eval_keyword(KeywordMatch(any=["error"]), "") == []
    assert eval_keyword(KeywordMatch(any=["e", "r"]), "") == []
    assert eval_keyword(KeywordMatch(all=["error"]), "") == []
    assert eval_keyword(KeywordMatch(any=["ERROR"], case_sensitive=True), "") == []

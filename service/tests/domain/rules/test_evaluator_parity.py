"""预筛求值与朴素求值的等价性测试：随机规则集 + 边界构造，逐行比对输出。"""

import random

from src.domain.rules.aho import AhoCorasick
from src.domain.rules.evaluator import (
    CompiledRule,
    CompiledRuleSet,
    _mandatory_literal,
    compile_ruleset,
    evaluate_line,
)
from src.domain.rules.models import (
    KeywordMatch,
    RegexMatch,
    Rule,
    RuleSetConfig,
)


def _naive_evaluate(compiled: CompiledRuleSet, line: str, line_no: int) -> list:
    """朴素路径：绕过预筛索引，逐规则完整求值（与索引路径共享 _match_rule）。"""
    matches = []
    lower_line: str | None = None
    for item in compiled.rules:
        if (
            isinstance(item.rule.match, KeywordMatch)
            and not item.rule.match.case_sensitive
            and lower_line is None
        ):
            lower_line = line.lower()
        match = compiled_index_free_match(item, line, line_no, lower_line)
        if match is not None:
            matches.append(match)
    return matches


def compiled_index_free_match(item: CompiledRule, line: str, line_no: int, lower_line):
    from src.domain.rules.evaluator import _match_rule

    return _match_rule(item, line, line_no, lower_line)


def _config(rules: list[Rule]) -> RuleSetConfig:
    return RuleSetConfig.model_validate(
        {
            "name": "parity",
            "file_glob": "**/*.log",
            "rules": [r.model_dump(mode="json") for r in rules],
        }
    )


def _assert_parity(rules: list[Rule], lines: list[str]) -> None:
    compiled = compile_ruleset(_config(rules))
    for line in lines:
        indexed = evaluate_line(compiled, line, 1)
        naive = _naive_evaluate(compiled, line, 1)
        assert [m.rule_id for m in indexed] == [m.rule_id for m in naive], (
            f"规则顺序/集合不一致: {line!r}\n索引: {[m.rule_id for m in indexed]}\n"
            f"朴素: {[m.rule_id for m in naive]}"
        )
        assert indexed == naive, f"命中字段不一致: {line!r}"


def test_aho_corasick_finds_all_needles_including_overlaps() -> None:
    needles = ["ab", "abc", "b", "bcd", ""]
    ac = AhoCorasick(needles)
    ac.build()
    found = ac.find_all("xabcdb")
    # ab@1-3、abc@1-4、b@2、b@5、bcd@2-5、""（空针恒在）
    assert found == set(range(len(needles)))


def test_aho_corasick_dedupes_and_empty_is_always_present() -> None:
    ac = AhoCorasick(["dup", "dup", ""])
    ac.build()
    assert ac.needle_count == 2
    # "dup" 不在 "anything" 中 → 只有恒真的空针（id 1）被报告
    assert ac.find_all("anything") == {1}
    assert ac.find_all("a dup here") == {0, 1}


def test_mandatory_literal_extraction() -> None:
    cases = {
        "cpu=(?P<cost>\\d{3,6})": "cpu=",
        "\\d{1,3}-benchx00042=(?P<x>\\d+)": "-benchx00042=",
        "a?bc": "bc",
        "(a|b)cpu=": "cpu=",
        "abc?": "ab",
        "abc+": "abc",
        "a{0,1}bc": "bc",
        "a{2,4}bc": "abc",  # 每个匹配的尾部必然是 a+bc，连续段 abc 是必选子串
        "key\\d+": "key",
        "^\\[ERROR\\]": "[ERROR]",
        "(foo|bar)?": None,
        "a|bc": None,
        "ab|a": None,
        "pid=(?P<pid>\\d+)": "pid=",
        "\\Qa.b\\E-c": "a.b-c",
        "(?<=pre)word": "word",
        "foo(?:bar)?baz": "foo",  # foo 与 baz 均必选，取最长者之一即可
        "status=50\\d": "status=50",
    }
    for pattern, expected in cases.items():
        assert _mandatory_literal(pattern) == expected, (
            f"{pattern!r}: got {_mandatory_literal(pattern)!r}, want {expected!r}"
        )


def test_parity_keyword_modes_and_case() -> None:
    rules = [
        Rule(
            id="kw1",
            match=KeywordMatch(
                type="keyword", any=["ERROR", "timeout"], all=[], case_sensitive=True
            ),
        ),
        Rule(
            id="kw2",
            match=KeywordMatch(type="keyword", any=["error"], all=[], case_sensitive=False),
        ),
        Rule(
            id="kw3",
            match=KeywordMatch(
                type="keyword", any=[], all=["WARN", "disk full"], case_sensitive=False
            ),
        ),
        Rule(
            id="kw4",
            match=KeywordMatch(type="keyword", any=[], all=["ERROR", "FATAL"], case_sensitive=True),
        ),
        Rule(
            id="kw5", match=KeywordMatch(type="keyword", any=["Error"], all=[], case_sensitive=True)
        ),
    ]
    lines = [
        "[2026-10-27][ERROR][pid=1] timeout happened",
        "[2026-10-27][error][pid=2] nothing",
        "[2026-10-27][WARN] disk full on /nas",
        "[2026-10-27] ERROR and FATAL both here",
        "[2026-10-27] error lowercase only",
        "plain line with nothing",
        "Error mixed case",
    ]
    _assert_parity(rules, lines)


def test_parity_prefix_chain_needles() -> None:
    # 前缀链是 AC 输出链与 any/all 计数的关键边界
    rules = [
        Rule(
            id="p1",
            match=KeywordMatch(type="keyword", any=["a", "ab", "abc"], all=[], case_sensitive=True),
        ),
        Rule(
            id="p2",
            match=KeywordMatch(type="keyword", any=[], all=["a", "ab"], case_sensitive=True),
        ),
        Rule(id="p3", match=KeywordMatch(type="keyword", any=["abc"], all=[], case_sensitive=True)),
        Rule(id="p4", match=KeywordMatch(type="keyword", any=[], all=["abc"], case_sensitive=True)),
    ]
    lines = ["abc", "ab", "abx", "a", "xabcx", "xab", "abcab", ""]
    _assert_parity(rules, lines)


def test_parity_regex_hints_and_thresholds() -> None:
    rules = [
        Rule(id="re1", match=RegexMatch(type="regex", pattern="pid=(?P<pid>\\d+)", timeout_ms=50)),
        Rule(
            id="re2",
            match=RegexMatch(
                type="regex",
                pattern="cost_ms=(?P<c>\\d{3,6})",
                timeout_ms=50,
                threshold={"field": "cost", "op": "gt", "value": "2000"},
            ),
        ),
        Rule(id="re3", match=RegexMatch(type="regex", pattern="\\d{2,4}-miss=\\d+", timeout_ms=50)),
        Rule(id="re4", match=RegexMatch(type="regex", pattern="(?i)ERROR", timeout_ms=50)),
        Rule(id="re5", match=RegexMatch(type="regex", pattern="a?bc", timeout_ms=50)),
        Rule(id="re6", match=RegexMatch(type="regex", pattern="foo(?:bar)?baz", timeout_ms=50)),
    ]
    lines = [
        "[x][pid=2048] cost_ms=3500",
        "[x][pid=99] cost_ms=150",
        "no numbers here",
        "bc and foorbaz and foobarbaz",
        "ERROR uppercase",
        "error lowercase for (?i)",
        "",
    ]
    _assert_parity(rules, lines)


def test_parity_ci_regex_full_case_folding() -> None:
    """regex 模块 IGNORECASE 是全 Unicode 折叠：hint 预筛不得对 ci 正则生效。

    TITLE/i 命中 tıtle（土耳其点less i）、STATUS=/i 命中 ſtatus=（长 s）——
    这类折叠 str.lower()+子串检查无法复现；ci 正则必须走完整求值。
    """
    rules = [
        Rule(id="ci1", match=RegexMatch(type="regex", pattern="TITLE", flags="i", timeout_ms=50)),
        Rule(id="ci2", match=RegexMatch(type="regex", pattern="STATUS=", flags="i", timeout_ms=50)),
        Rule(id="cs1", match=RegexMatch(type="regex", pattern="title", timeout_ms=50)),
    ]
    lines = ["tıtle", "ſtatus=", "title", "TITLE", "nothing"]
    _assert_parity(rules, lines)


def test_parity_disabled_rules_and_mixed() -> None:
    rules = [
        Rule(
            id="on1", match=KeywordMatch(type="keyword", any=["ERROR"], all=[], case_sensitive=True)
        ),
        Rule(
            id="off",
            enabled=False,
            match=KeywordMatch(type="keyword", any=["ERROR"], all=[], case_sensitive=True),
        ),
        Rule(id="on2", match=RegexMatch(type="regex", pattern="pid=(?P<pid>\\d+)", timeout_ms=50)),
        Rule(
            id="off2",
            enabled=False,
            match=RegexMatch(type="regex", pattern="pid=(?P<pid>\\d+)", timeout_ms=50),
        ),
    ]
    lines = ["[ERROR][pid=1]", "quiet"]
    _assert_parity(rules, lines)


def test_parity_randomized_rule_sets() -> None:
    rng = random.Random(20261029)
    vocab = [
        "ERROR",
        "error",
        "Error",
        "timeout",
        "connection refused",
        "pid=",
        "cost_ms=",
        "a",
        "ab",
        "abc",
        "warn",
        "WARN",
        "disk full",
        "x",
        "",
    ]
    for round_no in range(30):
        rules: list[Rule] = []
        for i in range(rng.randrange(1, 25)):
            if rng.random() < 0.5:
                needles = [rng.choice(vocab) for _ in range(rng.randrange(1, 4))]
                if rng.random() < 0.5:
                    rules.append(
                        Rule(
                            id=f"kw{i}",
                            match=KeywordMatch(
                                type="keyword",
                                any=needles,
                                all=[],
                                case_sensitive=rng.random() < 0.5,
                            ),
                        )
                    )
                else:
                    rules.append(
                        Rule(
                            id=f"kw{i}",
                            match=KeywordMatch(
                                type="keyword",
                                any=[],
                                all=needles,
                                case_sensitive=rng.random() < 0.5,
                            ),
                        )
                    )
            else:
                frags = [
                    "pid=",
                    "cost",
                    "\\d{1,3}",
                    "-x=",
                    "ERROR",
                    "a",
                    "[ab]",
                    "(?:c)?",
                    "\\.",
                    "e",
                ]
                pattern = "".join(rng.choice(frags) for _ in range(rng.randrange(1, 5)))
                rules.append(
                    Rule(
                        id=f"re{i}",
                        match=RegexMatch(type="regex", pattern=pattern, timeout_ms=50),
                    )
                )
        lines = []
        for _ in range(40):
            parts = [rng.choice(vocab) for _ in range(rng.randrange(0, 8))]
            lines.append(" ".join(parts))
        try:
            _assert_parity(rules, lines)
        except Exception as exc:
            # 两路径必须抛同型异常才算等价：这里只放行"朴素与索引一致"的异常
            raise AssertionError(f"round {round_no}: {exc}") from exc

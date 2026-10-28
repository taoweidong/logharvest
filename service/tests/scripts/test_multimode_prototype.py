from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.domain.rules.evaluator import compile_ruleset, evaluate_line
from src.domain.rules.models import (
    KeywordMatch,
    RegexMatch,
    Rule,
    RuleMatch,
    RuleSetConfig,
    Threshold,
)


def config_for(*matches):
    return RuleSetConfig(
        name="prototype-test",
        rules=[Rule(id=f"r{i}", match=match) for i, match in enumerate(matches)],
    )


def make_prototype(config):
    from scripts.multimode_prototype import compile_prototype

    return compile_prototype(config)


@pytest.fixture
def native():
    return pytest.importorskip("hyperscan", reason="install uv group prototype for native tests")


def test_missing_native_dependency_preserves_complete_reference_results(monkeypatch):
    import scripts.multimode_prototype as module

    config = config_for(KeywordMatch(any=["ERROR"]), RegexMatch(pattern="ERROR"))
    monkeypatch.setattr(module, "hs", None)
    prototype = make_prototype(config)
    assert not prototype.native_available
    assert prototype.evaluate_line("ERROR", 1) == evaluate_line(compile_ruleset(config), "ERROR", 1)
    assert prototype.stats.fallback_lines == 1


@pytest.mark.parametrize(
    ("match", "line", "text"),
    [
        (KeywordMatch(any=["late", "early"]), "early late", "late"),
        (KeywordMatch(all=["AA", "a", "AA"]), "aa", "AA"),
        (KeywordMatch(any=["TITLE"]), "tıtle", None),
        (KeywordMatch(any=["STATUS="]), "ſtatus=", None),
        (KeywordMatch(any=["İ"]), "i\u0307", "İ"),
        (KeywordMatch(any=["STRASSE"]), "Straße", None),
        (KeywordMatch(any=["Σ"]), "ΟΣ", None),
        (KeywordMatch(any=["Σ"]), "σ", "Σ"),
        (KeywordMatch(any=["", "missing"]), "", ""),
        (KeywordMatch(all=["", "a"]), "", None),
        (KeywordMatch(all=["", "a"]), "a", ""),
        (KeywordMatch(any=["foo\0bar"]), "foo", None),
        (KeywordMatch(any=["foo\0bar"]), "foo\0bar", "foo\0bar"),
        (KeywordMatch(any=["\ud800"]), "\ud800", "\ud800"),
        (KeywordMatch(any=["中文", "文"]), "中文", "中文"),
        (KeywordMatch(any=["a.*(b)"]), "a.*(b)", "a.*(b)"),
        (KeywordMatch(any=["ERROR"], case_sensitive=True), "error", None),
    ],
)
def test_keyword_semantics_preserve_config_order_and_python_lower(native, match, line, text):
    config = config_for(match)
    prototype = make_prototype(config)
    actual = prototype.evaluate_line(line, 1)
    assert actual == evaluate_line(compile_ruleset(config), line, 1)
    assert [item.matched_text for item in actual] == ([] if text is None else [text])


@pytest.mark.parametrize(
    ("pattern", "flags", "line"),
    [
        ("TITLE", "i", "tıtle"),
        ("STATUS=", "i", "ſtatus="),
        ("ERROR{e<=1}", "", "ERROR"),
        ("(?ai:ERROR)", "", "error"),
        ("foo|bar", "", "bar"),
        (r"(?P<x>a)(?P=x)", "", "aa"),
        (r"(?<=id=)\d+", "", "id=123"),
        (r"(?P<x>a)?b", "", "b"),
        (r"^ERROR$", "m", "ok\nERROR\nok"),
        (r"a.*b", "s", "a\nb"),
        (r"foo(*SKIP)(*FAIL)|bar", "", "foo bar"),
    ],
)
def test_uncertified_regexes_keep_exact_semantics(native, pattern, flags, line):
    config = config_for(RegexMatch(pattern=pattern, flags=flags))
    prototype = make_prototype(config)
    actual = prototype.evaluate_line(line, 1)
    assert len(actual) == 1
    assert actual == evaluate_line(compile_ruleset(config), line, 1)
    assert prototype.route_counts["screened_regex"] == 0
    assert prototype.stats.regex_calls == 1


@pytest.mark.parametrize(
    "pattern",
    ["ERROR", r"cost_ms=(?P<cost>\d+)", r"\d{1,3}-benchx001=(?P<x>\d{1,5})"],
)
def test_certified_whole_patterns_skip_negative_exact_calls(native, pattern):
    prototype = make_prototype(config_for(RegexMatch(pattern=pattern)))
    assert prototype.native_available
    assert prototype.route_counts["screened_regex"] == 1
    assert prototype.evaluate_line("unrelated 中文", 1) == []
    assert prototype.stats.regex_calls == 0
    assert prototype.stats.native_scans == 1
    assert prototype.stats.fallback_lines == 0


def test_full_results_thresholds_duplicate_rules_and_order(native):
    config = RuleSetConfig(
        name="identity",
        rules=[
            Rule(id="kw", match=KeywordMatch(any=["cost", "ERROR"]), severity="warn", tags=["a"]),
            Rule(
                id="re1",
                match=RegexMatch(pattern=r"cost=(?P<n>\d+)"),
                threshold={"field": "n", "op": "gte", "value": "12"},
            ),
            Rule(id="re2", match=RegexMatch(pattern=r"cost=(?P<n>\d+)")),
            Rule(
                id="absent",
                match=RegexMatch(pattern=r"cost=(?P<n>\d+)"),
                threshold={"field": "n", "op": "gt", "value": "12"},
            ),
            Rule(id="optional", match=RegexMatch(pattern=r"(?P<x>a)?b")),
            Rule(id="off", enabled=False, match=KeywordMatch(any=[""])),
        ],
    )
    prototype = make_prototype(config)
    actual = prototype.evaluate_line("ERROR cost=12 cost=99 b", 2)
    assert actual == [
        RuleMatch(rule_id="kw", severity="warn", tags=["a"], fields={}, matched_text="cost"),
        RuleMatch(
            rule_id="re1",
            severity="info",
            tags=[],
            fields={"n": "12"},
            numeric_value=Decimal("12"),
            matched_text="cost=12",
        ),
        RuleMatch(
            rule_id="re2", severity="info", tags=[], fields={"n": "12"}, matched_text="cost=12"
        ),
        RuleMatch(
            rule_id="optional", severity="info", tags=[], fields={"x": None}, matched_text="b"
        ),
    ]
    assert actual == evaluate_line(compile_ruleset(config), "ERROR cost=12 cost=99 b", 2)


def test_singlematch_is_per_literal_per_line_not_per_occurrence_or_batch(native):
    config = config_for(*(KeywordMatch(any=["a" * n], case_sensitive=True) for n in range(1, 65)))
    prototype = make_prototype(config)
    for line_no in (1, 2):
        assert len(prototype.evaluate_line("a" * 4096, line_no)) == 64
    assert prototype.stats.literal_events == 128
    assert prototype.stats.native_scans == 2
    assert prototype.stats.fallback_lines == 0
    assert prototype.evaluate_line("", 3) == []


def test_native_scan_failure_discards_partial_candidates(native):
    config = config_for(
        KeywordMatch(any=["ERROR"], case_sensitive=True), RegexMatch(pattern="WARN")
    )
    prototype = make_prototype(config)

    class BrokenDatabase:
        def scan(self, data, match_event_handler):
            match_event_handler(0, 0, 1, 0, None)
            raise native.ScanTerminated("injected partial scan")

    prototype.databases[True] = BrokenDatabase()
    line = "ERROR WARN"
    assert prototype.evaluate_line(line, 1) == evaluate_line(compile_ruleset(config), line, 1)
    assert prototype.stats.fallback_lines == 1


def test_native_compile_failure_falls_back_instead_of_dropping_rules(native, monkeypatch):
    import scripts.multimode_prototype as module

    def broken_database(**kwargs):
        raise native.error("injected compiler failure")

    config = config_for(KeywordMatch(any=["ERROR"]))
    monkeypatch.setattr(module.hs, "Database", broken_database)
    prototype = make_prototype(config)
    assert not prototype.native_available
    assert prototype.compile_error
    assert len(prototype.evaluate_line("ERROR", 1)) == 1


def test_surrogate_input_uses_full_reference_without_stale_hits(native):
    config = config_for(KeywordMatch(any=["ERROR"]), RegexMatch(pattern="WARN"))
    prototype = make_prototype(config)
    assert len(prototype.evaluate_line("ERROR", 1)) == 1
    line = "\ud800 WARN"
    assert prototype.evaluate_line(line, 2) == evaluate_line(compile_ruleset(config), line, 2)
    assert prototype.stats.fallback_lines == 1
    assert prototype.evaluate_line("none", 3) == []


def test_empty_and_disabled_only_configs(native):
    for config in (
        config_for(),
        RuleSetConfig(
            name="disabled",
            rules=[Rule(id="off", enabled=False, match=KeywordMatch(any=["ERROR"]))],
        ),
    ):
        prototype = make_prototype(config)
        assert prototype.evaluate_line("ERROR", 1) == []
        assert prototype.stats.native_scans == 0


def test_keyword_rule_with_threshold_never_uses_fast_path(native):
    # 模型禁止 keyword+threshold；绕过校验构造时参考路径不产出结果，快路径必须交回参考。
    keyword = KeywordMatch.model_construct(any=["ERROR"], all=[], case_sensitive=True)
    rule = Rule.model_construct(
        id="kw",
        name="",
        enabled=True,
        match=keyword,
        threshold=Threshold(field="n", op="gt", value="1"),
        severity="info",
        tags=[],
    )
    config = RuleSetConfig.model_construct(
        name="construct",
        file_glob="**/*",
        encoding="utf-8",
        fallback_encoding="gb18030",
        rules=[rule],
    )
    prototype = make_prototype(config)
    line = "ERROR"
    assert prototype.evaluate_line(line, 1) == evaluate_line(compile_ruleset(config), line, 1) == []
    assert prototype.route_counts["compatible_rules"] == 1
    assert prototype.route_counts["native_keywords"] == 0


@given(
    words=st.lists(
        st.sampled_from(["a", "aa", "A", "İ", "Σ", "", "foo\0bar", "中文"]), min_size=1, max_size=6
    ),
    line=st.text(alphabet="aAbf or\0İiΣσς中文", max_size=80),
    use_all=st.booleans(),
    sensitive=st.booleans(),
)
@settings(max_examples=120, deadline=None)
def test_generated_keywords_equal_independent_reference(words, line, use_all, sensitive):
    config = config_for(
        KeywordMatch(**{"all" if use_all else "any": words}, case_sensitive=sensitive),
        RegexMatch(pattern=r"a(?P<n>\d+)?"),
    )
    prototype = make_prototype(config)
    assert prototype.evaluate_line(line, 1) == evaluate_line(compile_ruleset(config), line, 1)

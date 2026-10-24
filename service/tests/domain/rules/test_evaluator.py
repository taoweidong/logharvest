import logging
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.domain.rules.evaluator import compile_ruleset, evaluate_line
from src.domain.rules.models import (
    KeywordMatch,
    RegexMatch,
    Rule,
    RuleSetConfig,
    Threshold,
)


def compile_one(rule: Rule):
    return compile_ruleset(RuleSetConfig(name="t", rules=[rule]))


def test_keyword_any_is_case_insensitive_by_default() -> None:
    compiled = compile_one(Rule(id="e", match=KeywordMatch(any=["error", "exception"])))
    matches = evaluate_line(compiled, "an ERROR happened", 1)
    assert [m.rule_id for m in matches] == ["e"]


def test_keyword_all_requires_every_keyword() -> None:
    compiled = compile_one(Rule(id="e", match=KeywordMatch(all=["error", "timeout"])))
    assert evaluate_line(compiled, "error timeout", 1)
    assert evaluate_line(compiled, "error only", 2) == []


def test_keyword_matched_text_preserves_configured_keyword() -> None:
    compiled = compile_one(Rule(id="e", match=KeywordMatch(any=["ERROR"])))
    matches = evaluate_line(compiled, "an error happened", 1)
    assert len(matches) == 1
    assert matches[0].matched_text == "ERROR"


def test_regex_extracts_named_fields_and_threshold() -> None:
    compiled = compile_one(
        Rule(
            id="slow",
            match=RegexMatch(pattern=r"elapsed=(?P<elapsed_ms>\d+)ms"),
            threshold=Threshold(field="elapsed_ms", op="gt", value=Decimal(5000)),
            severity="warning",
        )
    )
    matches = evaluate_line(compiled, "request elapsed=6000ms", 3)
    assert len(matches) == 1
    assert matches[0].fields["elapsed_ms"] == "6000"
    assert matches[0].numeric_value == Decimal(6000)
    assert matches[0].severity == "warning"
    assert matches[0].matched_text == "elapsed=6000ms"
    assert evaluate_line(compiled, "request elapsed=100ms", 4) == []


def test_threshold_missing_optional_group_does_not_match() -> None:
    compiled = compile_one(
        Rule(
            id="slow",
            match=RegexMatch(
                pattern=r"elapsed=(?P<elapsed_ms>\d+)ms(?: cached=(?P<cached_ms>\d+)ms)?"
            ),
            threshold=Threshold(field="cached_ms", op="gt", value=Decimal(1)),
        )
    )
    assert evaluate_line(compiled, "elapsed=6000ms", 1) == []
    matches = evaluate_line(compiled, "elapsed=6000ms cached=9ms", 2)
    assert len(matches) == 1
    assert matches[0].numeric_value == Decimal(9)


def test_threshold_field_must_be_named_group() -> None:
    with pytest.raises(ValueError, match="slow"):
        compile_one(
            Rule(
                id="slow",
                match=RegexMatch(pattern=r"elapsed=(?P<elapsed_ms>\d+)ms"),
                threshold=Threshold(field="missing", op="gt", value=Decimal(1)),
            )
        )


def test_threshold_with_regex_flags_and_eq() -> None:
    compiled = compile_one(
        Rule(
            id="notfound",
            match=RegexMatch(pattern=r"STATUS=(?P<code>\d+)", flags="i"),
            threshold=Threshold(field="code", op="eq", value=Decimal(404)),
        )
    )
    matches = evaluate_line(compiled, "status=404", 1)
    assert len(matches) == 1
    assert matches[0].numeric_value == Decimal(404)
    assert evaluate_line(compiled, "status=500", 2) == []


def test_non_finite_and_non_numeric_threshold_values_do_not_match() -> None:
    compiled = compile_one(
        Rule(
            id="v",
            match=RegexMatch(pattern=r"value=(?P<v>\S+)"),
            threshold=Threshold(field="v", op="gt", value=Decimal(0)),
        )
    )
    for line_no, line in enumerate(["value=NaN", "value=Infinity", "value=abc"], start=1):
        assert evaluate_line(compiled, line, line_no) == []


def test_keyword_rule_rejects_threshold() -> None:
    with pytest.raises(ValidationError):
        Rule(
            id="e",
            match=KeywordMatch(any=["error"]),
            threshold=Threshold(field="elapsed_ms", op="gt", value=Decimal(1)),
        )


@pytest.mark.parametrize("timeout_ms", [0, -1])
def test_timeout_ms_must_be_positive(timeout_ms: int) -> None:
    with pytest.raises(ValidationError):
        RegexMatch(pattern=r"x", timeout_ms=timeout_ms)


def test_regex_timeout_does_not_abort_evaluation(caplog: pytest.LogCaptureFixture) -> None:
    compiled = compile_ruleset(
        RuleSetConfig(
            name="t",
            rules=[
                Rule(id="evil", match=RegexMatch(pattern=r"(a+)+$", timeout_ms=1)),
                Rule(id="ok", match=KeywordMatch(any=["error"])),
            ],
        )
    )
    line = "a" * 50_000 + "! error"
    with caplog.at_level(logging.WARNING, logger="src.domain.rules.evaluator"):
        matches = evaluate_line(compiled, line, 7)
    assert [m.rule_id for m in matches] == ["ok"]
    assert "evil" in caplog.text
    assert "line 7" in caplog.text


def test_invalid_regex_pattern_names_rule_in_error() -> None:
    with pytest.raises(ValueError, match="broken"):
        compile_one(Rule(id="broken", match=RegexMatch(pattern=r"(")))


def test_unknown_regex_flags_are_rejected() -> None:
    with pytest.raises(ValueError, match="bad"):
        compile_one(Rule(id="bad", match=RegexMatch(pattern=r"x", flags="x")))


def test_disabled_rule_is_skipped() -> None:
    compiled = compile_one(Rule(id="e", enabled=False, match=KeywordMatch(any=["error"])))
    assert evaluate_line(compiled, "error", 1) == []


def test_keyword_any_and_all_are_mutually_exclusive() -> None:
    with pytest.raises(ValidationError):
        KeywordMatch(any=["a"], all=["b"])


def test_keyword_case_sensitive_requires_exact_case() -> None:
    compiled = compile_one(Rule(id="e", match=KeywordMatch(any=["ERROR"], case_sensitive=True)))
    assert evaluate_line(compiled, "an ERROR happened", 1)
    assert evaluate_line(compiled, "an error happened", 2) == []


def test_rule_ids_must_be_unique() -> None:
    with pytest.raises(ValidationError):
        RuleSetConfig(
            name="t",
            rules=[
                Rule(id="dup", match=KeywordMatch(any=["error"])),
                Rule(id="dup", match=KeywordMatch(any=["timeout"])),
            ],
        )

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


def test_regex_extracts_named_fields_and_threshold() -> None:
    compiled = compile_one(
        Rule(
            id="slow",
            match=RegexMatch(pattern=r"elapsed=(?P<elapsed_ms>\d+)ms"),
            threshold=Threshold(field="elapsed_ms", op="gt", value=Decimal("5000")),
            severity="warning",
        )
    )
    matches = evaluate_line(compiled, "request elapsed=6000ms", 3)
    assert len(matches) == 1
    assert matches[0].fields["elapsed_ms"] == "6000"
    assert matches[0].numeric_value == Decimal("6000")
    assert matches[0].severity == "warning"
    assert evaluate_line(compiled, "request elapsed=100ms", 4) == []


def test_threshold_missing_field_does_not_match() -> None:
    compiled = compile_one(
        Rule(
            id="slow",
            match=RegexMatch(pattern=r"elapsed=(?P<elapsed_ms>\d+)ms"),
            threshold=Threshold(field="missing", op="gt", value=Decimal("1")),
        )
    )
    assert evaluate_line(compiled, "elapsed=6000ms", 1) == []


def test_disabled_rule_is_skipped() -> None:
    compiled = compile_one(Rule(id="e", enabled=False, match=KeywordMatch(any=["error"])))
    assert evaluate_line(compiled, "error", 1) == []


def test_keyword_any_and_all_are_mutually_exclusive() -> None:
    with pytest.raises(ValidationError):
        KeywordMatch(any=["a"], all=["b"])


def test_keyword_case_sensitive_requires_exact_case() -> None:
    compiled = compile_one(
        Rule(id="e", match=KeywordMatch(any=["ERROR"], case_sensitive=True))
    )
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

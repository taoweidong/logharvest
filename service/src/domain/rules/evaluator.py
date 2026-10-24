from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable

import regex

from src.domain.rules.models import (
    KeywordMatch,
    RegexMatch,
    Rule,
    RuleMatch,
    RuleSetConfig,
    Threshold,
)

_OPS: dict[str, Callable[[Decimal, Decimal], bool]] = {
    "gt": lambda left, right: left > right,
    "gte": lambda left, right: left >= right,
    "lt": lambda left, right: left < right,
    "lte": lambda left, right: left <= right,
    "eq": lambda left, right: left == right,
}


@dataclass(frozen=True)
class CompiledRule:
    rule: Rule
    regex: regex.Pattern | None = None


@dataclass(frozen=True)
class CompiledRuleSet:
    config: RuleSetConfig
    rules: tuple[CompiledRule, ...]


def _regex_flags(flags: str) -> int:
    result = 0
    if "i" in flags:
        result |= regex.IGNORECASE
    if "m" in flags:
        result |= regex.MULTILINE
    if "s" in flags:
        result |= regex.DOTALL
    return result


def compile_ruleset(config: RuleSetConfig) -> CompiledRuleSet:
    compiled: list[CompiledRule] = []
    for rule in config.rules:
        if isinstance(rule.match, RegexMatch):
            pattern = regex.compile(rule.match.pattern, _regex_flags(rule.match.flags))
            compiled.append(CompiledRule(rule=rule, regex=pattern))
        else:
            compiled.append(CompiledRule(rule=rule))
    return CompiledRuleSet(config=config, rules=tuple(compiled))


def _threshold_matches(
    threshold: Threshold, fields: dict[str, str | None]
) -> tuple[bool, Decimal | None]:
    raw = fields.get(threshold.field)
    if raw is None:
        return False, None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return False, None
    return _OPS[threshold.op](value, threshold.value), value


def evaluate_line(compiled: CompiledRuleSet, line: str, line_no: int) -> list[RuleMatch]:
    del line_no  # line number is attached by the worker when persisting
    matches: list[RuleMatch] = []
    for item in compiled.rules:
        rule = item.rule
        if not rule.enabled:
            continue

        fields: dict[str, str | None] = {}
        matched_text: str | None = None

        if isinstance(rule.match, KeywordMatch):
            haystack = line if rule.match.case_sensitive else line.lower()
            words = rule.match.any or rule.match.all
            needles = words if rule.match.case_sensitive else [word.lower() for word in words]
            if rule.match.any:
                hit = next((word for word in needles if word in haystack), None)
                if hit is None:
                    continue
                matched_text = hit
            else:
                if not all(word in haystack for word in needles):
                    continue
                matched_text = words[0]
        elif isinstance(rule.match, RegexMatch):
            assert item.regex is not None
            found = item.regex.search(line, timeout=rule.match.timeout_ms / 1000)
            if found is None:
                continue
            fields = found.groupdict()
            matched_text = found.group(0)
        else:
            continue

        numeric_value: Decimal | None = None
        if rule.threshold is not None:
            passed, numeric_value = _threshold_matches(rule.threshold, fields)
            if not passed:
                continue

        matches.append(
            RuleMatch(
                rule_id=rule.id,
                severity=rule.severity,
                tags=list(rule.tags),
                fields=fields,
                numeric_value=numeric_value,
                matched_text=matched_text,
            )
        )
    return matches

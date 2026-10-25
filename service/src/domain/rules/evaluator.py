from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import regex

from src.domain.rules.models import (
    KeywordMatch,
    RegexMatch,
    Rule,
    RuleMatch,
    RuleSetConfig,
    Threshold,
)

logger = logging.getLogger(__name__)

_OPS: dict[str, Callable[[Decimal, Decimal], bool]] = {
    "gt": lambda left, right: left > right,
    "gte": lambda left, right: left >= right,
    "lt": lambda left, right: left < right,
    "lte": lambda left, right: left <= right,
    "eq": lambda left, right: left == right,
}

_ALLOWED_REGEX_FLAGS = frozenset("ims")


@dataclass(frozen=True)
class CompiledRule:
    rule: Rule
    regex: regex.Pattern | None = None


@dataclass(frozen=True)
class CompiledRuleSet:
    config: RuleSetConfig
    rules: tuple[CompiledRule, ...]


def _regex_flags(flags: str, rule_id: str) -> int:
    unknown = sorted(set(flags) - _ALLOWED_REGEX_FLAGS)
    if unknown:
        raise ValueError(
            f"rule {rule_id!r}: unsupported regex flags {unknown}; allowed flags are 'i', 'm', 's'"
        )
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
            try:
                pattern = regex.compile(rule.match.pattern, _regex_flags(rule.match.flags, rule.id))
            except regex.error as exc:
                raise ValueError(f"rule {rule.id!r}: invalid regex pattern: {exc}") from exc
            if rule.threshold is not None and rule.threshold.field not in pattern.groupindex:
                raise ValueError(
                    f"rule {rule.id!r}: threshold field {rule.threshold.field!r} is not a "
                    f"named group in pattern (available: {sorted(pattern.groupindex)})"
                )
            compiled.append(CompiledRule(rule=rule, regex=pattern))
        else:
            compiled.append(CompiledRule(rule=rule))
    return CompiledRuleSet(config=config, rules=tuple(compiled))


def _threshold_matches(
    threshold: Threshold, fields: dict[str, str | None]
) -> tuple[bool, Decimal | None]:
    if not threshold.value.is_finite():
        return False, None
    raw = fields.get(threshold.field)
    if raw is None:
        return False, None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return False, None
    if not value.is_finite():
        return False, None
    try:
        return _OPS[threshold.op](value, threshold.value), value
    except ArithmeticError:
        return False, None


def _keyword_hit(match: KeywordMatch, line: str) -> str | None:
    haystack = line if match.case_sensitive else line.lower()
    words = match.any or match.all
    if match.any:
        for word in words:
            needle = word if match.case_sensitive else word.lower()
            if needle in haystack:
                return word
        return None
    if all((word if match.case_sensitive else word.lower()) in haystack for word in words):
        return words[0]
    return None


def evaluate_line(compiled: CompiledRuleSet, line: str, line_no: int) -> list[RuleMatch]:
    matches: list[RuleMatch] = []
    for item in compiled.rules:
        rule = item.rule
        if not rule.enabled:
            continue

        fields: dict[str, str | None] = {}
        matched_text: str | None = None

        if isinstance(rule.match, KeywordMatch):
            matched_text = _keyword_hit(rule.match, line)
            if matched_text is None:
                continue
        elif isinstance(rule.match, RegexMatch):
            if item.regex is None:
                raise RuntimeError(f"rule {rule.id!r}: regex match was not compiled")
            try:
                found = item.regex.search(line, timeout=rule.match.timeout_ms / 1000)
            except TimeoutError:
                logger.warning(
                    "rule %s: regex timed out after %d ms on line %d",
                    rule.id,
                    rule.match.timeout_ms,
                    line_no,
                )
                continue
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

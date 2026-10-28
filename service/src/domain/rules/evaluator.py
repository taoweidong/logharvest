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
    # 编译期预计算的关键字对：(needle, 配置原词)，按配置顺序排列。
    # needle = word（敏感）或 word.lower()（不敏感）；None 表示未经 compile_ruleset
    # 预编译（外部手工构造 CompiledRule 的情形），求值时退回现算 needle 的路径。
    keyword_needles: tuple[tuple[str, str], ...] | None = None
    # any 模式返回第一个命中词的配置原词，all 模式返回首词；由 keyword_needles 一并在
    # 编译期定下，避免每行每规则重复读取 match.any/match.all 并做 lower()。
    keyword_is_any: bool = False


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
        elif isinstance(rule.match, KeywordMatch):
            keyword = rule.match
            words = keyword.any or keyword.all
            if words:
                compiled.append(
                    CompiledRule(
                        rule=rule,
                        keyword_needles=tuple(
                            (word if keyword.case_sensitive else word.lower(), word)
                            for word in words
                        ),
                        keyword_is_any=bool(keyword.any),
                    )
                )
            else:
                # 词表为空时（只有绕过校验才可能）保持求值期的既有行为。
                compiled.append(CompiledRule(rule=rule))
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


def _keyword_hit(match: KeywordMatch, haystack: str, compiled: CompiledRule) -> str | None:
    """在 haystack 上判定关键字命中，返回命中的配置原词。

    haystack 由调用方决定：区分大小写时是原始行，否则是该行的小写副本。
    needle 已在编译期归一化，这里只做子串判定。
    """
    needles = compiled.keyword_needles
    if needles is None:
        # 手工构造（未预编译）的条目：退回逐词现算 needle 的旧路径。
        words = match.any or match.all
        needles = tuple((word if match.case_sensitive else word.lower(), word) for word in words)
        is_any = bool(match.any)
    else:
        is_any = compiled.keyword_is_any
    if is_any:
        for needle, word in needles:
            if needle in haystack:
                return word
        return None
    for needle, _word in needles:
        if needle not in haystack:
            return None
    return needles[0][1]


def evaluate_line(compiled: CompiledRuleSet, line: str, line_no: int) -> list[RuleMatch]:
    matches: list[RuleMatch] = []
    # 每行最多小写化一次，且只在真正需要不敏感 haystack 的关键字规则出现时才计算。
    lower_line: str | None = None
    for item in compiled.rules:
        rule = item.rule
        if not rule.enabled:
            continue

        fields: dict[str, str | None] = {}
        matched_text: str | None = None

        if isinstance(rule.match, KeywordMatch):
            if rule.match.case_sensitive:
                haystack = line
            else:
                if lower_line is None:
                    lower_line = line.lower()
                haystack = lower_line
            matched_text = _keyword_hit(rule.match, haystack, item)
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

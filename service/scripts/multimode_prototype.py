from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from src.domain.rules.evaluator import CompiledRuleSet, compile_ruleset, evaluate_line
from src.domain.rules.models import KeywordMatch, RegexMatch, RuleMatch, RuleSetConfig

try:
    import hyperscan as hs
except (ImportError, OSError):
    hs = None


_PLAIN_LITERAL = re.compile(r"[A-Za-z0-9 _=:/,@%!-]+")
_NUMERIC_TEMPLATE = re.compile(
    r"(?:\\d\{\d{1,3},\d{1,3}\})?([A-Za-z0-9 _=:/,@%!-]+)"
    r"\(\?P<[A-Za-z_][A-Za-z0-9_]*>\\d(?:\+|\{\d{1,3},\d{1,3}\})\)"
)


@dataclass
class ScanStats:
    lines: int = 0
    native_scans: int = 0
    literal_events: int = 0
    regex_calls: int = 0
    fallback_lines: int = 0
    compatible_rule_calls: int = 0
    keyword_fast_matches: int = 0


@dataclass(frozen=True)
class KeywordPlan:
    ordered_ids: tuple[int, ...]
    required_ids: frozenset[int]
    use_any: bool


def certified_literal(match: RegexMatch) -> str | None:
    # Full-pattern recognition prevents extensions or flags from weakening the literal condition.
    if "i" in match.flags:
        return None
    if _PLAIN_LITERAL.fullmatch(match.pattern):
        return match.pattern
    found = _NUMERIC_TEMPLATE.fullmatch(match.pattern)
    return found.group(1) if found else None


def _encodable_literal(word: str) -> bool:
    # The 0.9.1 binding uses strlen for literal lengths, so NUL cannot enter its database.
    if "\0" in word:
        return False
    try:
        word.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


class ScreeningPrototype:
    """Opt-in, sequential-only prototype; each instance owns its native scratch."""

    def __init__(self, config: RuleSetConfig):
        self.reference = compile_ruleset(config.model_copy(deep=True))
        self.stats = ScanStats()
        self.databases: dict[bool, Any] = {}
        self.native_available = False
        self.compile_error: str | None = None
        self.keywords: dict[int, KeywordPlan] = {}
        self.keyword_owners: dict[int, set[int]] = defaultdict(set)
        self.regex_owners: dict[int, set[int]] = defaultdict(set)
        self.fallback_positions: set[int] = set()
        self.always_hits: set[int] = set()
        literal_ids: dict[tuple[bool, str], int] = {}
        self.positions = {item.rule.id: i for i, item in enumerate(self.reference.rules)}
        self.active_positions = {
            i for i, item in enumerate(self.reference.rules) if item.rule.enabled
        }
        self.regex_positions = {
            i
            for i in self.active_positions
            if isinstance(self.reference.rules[i].rule.match, RegexMatch)
        }

        def register(word: str, sensitive: bool) -> int:
            key = (sensitive, word)
            if key not in literal_ids:
                literal_ids[key] = len(literal_ids)
            literal_id = literal_ids[key]
            if not word:
                self.always_hits.add(literal_id)
            return literal_id

        screened_regex = 0
        self.screened_positions: set[int] = set()
        for position in sorted(self.active_positions):
            match = self.reference.rules[position].rule.match
            if isinstance(match, KeywordMatch):
                if self.reference.rules[position].rule.threshold is not None:
                    # 模型禁止 keyword+threshold，但绕过校验构造时参考路径会因字段为空而不产出；
                    # 快路径无法复现该判定，交回参考求值器保证等价。
                    self.fallback_positions.add(position)
                    continue
                words = match.any or match.all
                normalized = [word if match.case_sensitive else word.lower() for word in words]
                if not all(_encodable_literal(word) for word in normalized):
                    self.fallback_positions.add(position)
                    continue
                ids = tuple(register(word, match.case_sensitive) for word in normalized)
                self.keywords[position] = KeywordPlan(ids, frozenset(ids), bool(match.any))
                for literal_id in set(ids):
                    self.keyword_owners[literal_id].add(position)
            else:
                literal = certified_literal(match)
                if literal is None:
                    self.fallback_positions.add(position)
                    continue
                self.regex_owners[register(literal, True)].add(position)
                self.screened_positions.add(position)
                screened_regex += 1

        self.route_counts = {
            "native_keywords": len(self.keywords),
            "screened_regex": screened_regex,
            "compatible_rules": len(self.fallback_positions),
            "unique_literals": len(literal_ids),
        }
        # Static rule subsets are compiled once: rebuilding a sorted tuple per line would
        # cost more than the rules it skips whenever most rules are incompatible.
        self.fallback_compiled = self._subset(self.fallback_positions)
        self.screened_compiled = self._subset(self.screened_positions)
        self._fallback_rule_count = len(self.fallback_positions)
        self._fallback_regex_count = len(self.fallback_positions & self.regex_positions)
        self._screened_count = len(self.screened_positions)
        self._active_regex_count = len(self.regex_positions)
        if hs is None:
            self.compile_error = "hyperscan unavailable; install the prototype dependency group"
            return
        try:
            for sensitive in (True, False):
                entries = [
                    (literal_id, word.encode("utf-8"))
                    for (channel, word), literal_id in literal_ids.items()
                    if channel == sensitive and word
                ]
                if not entries:
                    continue
                database = hs.Database(mode=hs.HS_MODE_BLOCK)
                database.compile(
                    expressions=[word for _, word in entries],
                    ids=[literal_id for literal_id, _ in entries],
                    elements=len(entries),
                    flags=hs.HS_FLAG_SINGLEMATCH,
                    literal=True,
                )
                self.databases[sensitive] = database
        except hs.error as exc:
            self.databases.clear()
            self.compile_error = str(exc)
            return
        self.native_available = True

    def _subset(self, positions: set[int]) -> CompiledRuleSet | None:
        if not positions:
            return None
        return CompiledRuleSet(
            config=self.reference.config,
            rules=tuple(self.reference.rules[i] for i in sorted(positions)),
        )

    def _fallback(self, line: str, line_no: int) -> list[RuleMatch]:
        self.stats.fallback_lines += 1
        self.stats.regex_calls += self._active_regex_count
        self.stats.compatible_rule_calls += len(self.active_positions)
        # Disabled rules are skipped by the reference evaluator, so the full set is exact.
        return evaluate_line(self.reference, line, line_no)

    def evaluate_line(self, line: str, line_no: int) -> list[RuleMatch]:
        self.stats.lines += 1
        if not self.native_available:
            return self._fallback(line, line_no)
        hits = set(self.always_hits)
        seeded = len(hits)

        def on_match(literal_id, start, end, flags, context):
            hits.add(literal_id)
            return None

        try:
            for sensitive, database in self.databases.items():
                data = (line if sensitive else line.lower()).encode("utf-8")
                self.stats.native_scans += 1
                database.scan(data, match_event_handler=on_match)
        except (UnicodeEncodeError, hs.error):
            return self._fallback(line, line_no)
        self.stats.literal_events += len(hits) - seeded

        keyword_positions: set[int] = set()
        screened: set[int] = set()
        for literal_id in hits:
            keyword_positions.update(self.keyword_owners.get(literal_id, ()))
            screened.update(self.regex_owners.get(literal_id, ()))

        results: list[RuleMatch] = []
        if self.fallback_compiled is not None:
            self.stats.compatible_rule_calls += self._fallback_rule_count
            self.stats.regex_calls += self._fallback_regex_count
            results.extend(evaluate_line(self.fallback_compiled, line, line_no))
        if screened:
            self.stats.regex_calls += len(screened)
            compiled = self.screened_compiled if len(screened) == self._screened_count else None
            if compiled is None:
                compiled = self._subset(screened)
            assert compiled is not None
            results.extend(evaluate_line(compiled, line, line_no))
        for position in sorted(keyword_positions):
            plan = self.keywords[position]
            if not plan.use_any and not plan.required_ids.issubset(hits):
                continue
            rule = self.reference.rules[position].rule
            match = rule.match
            assert isinstance(match, KeywordMatch)
            words = match.any or match.all
            word_index = (
                next(i for i, literal_id in enumerate(plan.ordered_ids) if literal_id in hits)
                if plan.use_any
                else 0
            )
            results.append(
                RuleMatch(
                    rule_id=rule.id,
                    severity=rule.severity,
                    tags=list(rule.tags),
                    fields={},
                    matched_text=words[word_index],
                )
            )
            self.stats.keyword_fast_matches += 1
        results.sort(key=lambda item: self.positions[item.rule_id])
        return results


def compile_prototype(config: RuleSetConfig) -> ScreeningPrototype:
    return ScreeningPrototype(config)

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import regex

from src.domain.rules.aho import AhoCorasick
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

# 字符类/锚点类转义：中断字面量段（不能作为必选字面量的一部分）
_REGEX_CLASS_ESCAPES = frozenset("dswDSWbBAZz")


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


class RuleIndex:
    """候选预筛索引：把"每行遍历全部规则"降为"每行一次 AC 扫描 + 字面量 hint 检查"。

    关键字规则：同组针（大小写敏感 / 不敏感）合并进一棵 Aho–Corasick 自动机
    （:mod:`src.domain.rules.aho`），每行各扫一次得到"出现过的针"，再按规则需要的
    不同针数（any 恒为 1、all 为针全集）筛出候选。针是字面量，AC 报告所有出现
    （含前后缀重叠），候选集合**零假阴性、零假阳性**。

    正则规则：提取模式中的**必选字面量段**（:func:`_mandatory_literal`，保守提取
    ——完整正则的任何匹配必然包含该段），每行对去重后的 hint 逐一 ``in`` 检查；
    提取不到 hint 的规则进入 always 集合。预筛零假阴性、允许假阳性（hint 命中但
    完整正则不命中），命中判定仍由完整正则执行。

    索引路径与朴素路径（``index is None``，手工构造 CompiledRuleSet 的测试用）
    共用同一个逐规则函数 :func:`_match_rule`，输出由等价性测试钉住
    （tests/domain/rules/test_evaluator_parity.py）。
    """

    __slots__ = (
        "always",
        "hint_map",
        "kw_automata",
        "kw_needle_total",
        "kw_refs",
        "needs_lower",
    )

    def __init__(
        self,
        kw_automata: list[tuple[AhoCorasick, bool]],
        kw_refs: list[list[list[int]]],
        kw_needle_total: dict[int, int],
        hint_map: dict[str, list[int]],
        always: list[int],
    ) -> None:
        # kw_groups: 每个 (自动机, 是否大小写敏感) 组；refs[组][针编号] → 规则位置
        self.kw_automata = kw_automata
        self.kw_refs = kw_refs
        # 规则位置 → 需要命中的不同针数（any 模式 = 1；all 模式 = 针全集大小）
        self.kw_needle_total = kw_needle_total
        # hint_map: 必选字面量段 → 规则位置。只收大小写敏感的正则——
        # 不敏感正则的全折叠语义无法用 lower()+子串复现（见 _build_index）。
        self.hint_map = hint_map
        self.always = sorted(always)
        # 没有任何不敏感消费方（ci 自动机 / ci hint）时跳过整行 lower()
        self.needs_lower = any(not case_sensitive for _automaton, case_sensitive in kw_automata)

    def candidates(self, line: str, line_lower: str) -> tuple[int, ...]:
        candidates: set[int] = set(self.always)
        # 关键字候选：命中针的去重数量达到该规则需要的针数（any=1，all=针全集）。
        counts: dict[int, int] = {}
        for (automaton, case_sensitive), refs in zip(self.kw_automata, self.kw_refs, strict=True):
            hay = line if case_sensitive else line_lower
            for needle_id in automaton.find_all(hay):
                for pos in refs[needle_id]:
                    counts[pos] = counts.get(pos, 0) + 1
        for pos, count in counts.items():
            if count >= self.kw_needle_total[pos]:
                candidates.add(pos)
        # 正则候选：必选字面量段出现即入选（完整判定交给正则本身）。
        for hint, positions in self.hint_map.items():
            if hint in line:
                candidates.update(positions)
        return tuple(sorted(candidates))


@dataclass(frozen=True)
class CompiledRuleSet:
    config: RuleSetConfig
    rules: tuple[CompiledRule, ...]
    # 手工构造（测试）不传 index 时走朴素遍历路径，行为与索引路径等价。
    index: RuleIndex | None = None


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


def _mandatory_literal(pattern: str) -> str | None:
    """从正则模式中保守提取"任一匹配必然包含"的最长字面量段。

    提取不到（分支、可选组、量词紧邻等任何存疑情形）返回 ``None``，调用方把该
    规则放进 always 集合。提取错误会造成漏匹配，因此所有规则都往"宁缺毋滥"方向
    保守处理：

    * ``\\d``/``[...]`` 等类与 ``^``/``$``/``\\b`` 锚点中断当前段；
    * 转义字面量（``\\.``、``\\+``、``\\Q…\\E``）并入当前段；
    * 量词：``?``/``*``/``{0,…}`` 使紧邻的前一个字面量字符变为可选（从段中移除），
      ``+``/``{n,…}``（n≥1）不改变必选性；
    * 分支 ``|`` 与任何分组 ``(...)`` 会让已收集的段全部作废（不进组内提取）。
    """
    runs: list[str] = []
    run: list[str] = []
    top_level_alternation = False
    i = 0
    n = len(pattern)

    def flush() -> None:
        if run:
            runs.append("".join(run))
            run.clear()

    while i < n:
        ch = pattern[i]
        if ch == "\\":
            nxt = pattern[i + 1] if i + 1 < n else ""
            if nxt == "Q":
                end = pattern.find("\\E", i + 2)
                if end == -1:
                    run.append(pattern[i + 2 :])
                    i = n
                else:
                    run.append(pattern[i + 2 : end])
                    i = end + 2
            elif nxt in _REGEX_CLASS_ESCAPES or nxt == "":
                flush()
                i += 2
            else:
                run.append(nxt if nxt else "\\")
                i += 2
            continue
        if ch == "[":
            flush()
            j = i + 1
            if j < n and pattern[j] == "^":
                j += 1
            while j < n and (pattern[j] != "]" or pattern[j - 1] == "\\"):
                j += 1
            i = j + 1
            continue
        if ch == "(":
            if pattern.startswith("(?i", i) and (i + 3 >= n or pattern[i + 3] in "):"):
                # 内联大小写不敏感标志：其后所有字面量的大小写语义都不可靠，
                # 无法安全提取——放弃整个模式（宁缺毋滥）。
                return None
            flush()
            # 跳过整组（含嵌套与各种 (?...) 形式）；组内容不参与提取。
            # 组之前的段不受组影响（组只在自身被量词修饰时才影响自己）。
            depth = 0
            while i < n:
                if pattern[i] == "\\":
                    i += 2
                    continue
                if pattern[i] == "(":
                    depth += 1
                elif pattern[i] == ")":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
            continue
        if ch == "|":
            # 顶层分支：两侧的字面量段都可能不在被选中的分支上 → 全部作废。
            run.clear()
            runs.clear()
            top_level_alternation = True
            i += 1
            continue
        if ch in "*?":
            # 零次或多次：紧邻的前一个字面量字符变为可选
            if run:
                run.pop()
            i += 1
            continue
        if ch == "+":
            # 一次或多次：前一个 token 仍至少出现一次，`+` 本身不是字面量
            i += 1
            continue
        if ch == "{":
            end = pattern.find("}", i)
            if end == -1:
                # 非量词的花括号字面量（regex 宽容语法）：按字面量并入段
                run.append(ch)
                i += 1
                continue
            body = pattern[i + 1 : end]
            low = body.split(",", 1)[0]
            if low.isdigit() and int(low) == 0 and run:
                run.pop()
            i = end + 1
            continue
        if ch in ".$^)":
            flush()
            i += 1
            continue
        run.append(ch)
        i += 1
    flush()
    if top_level_alternation:
        return None
    return max(runs, key=len) if runs else None


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
    return CompiledRuleSet(
        config=config, rules=tuple(compiled), index=_build_index(tuple(compiled))
    )


def _build_index(rules: tuple[CompiledRule, ...]) -> RuleIndex:
    # 关键字按大小写敏感性分两组；正则 hint 按是否带 i 标志分两组。
    kw_cs_needles: dict[str, int] = {}
    kw_ci_needles: dict[str, int] = {}
    kw_cs_refs: list[list[int]] = []
    kw_ci_refs: list[list[int]] = []
    needle_total: dict[int, int] = {}
    hint_cs: dict[str, list[int]] = {}
    always: list[int] = []

    for pos, item in enumerate(rules):
        rule = item.rule
        if not rule.enabled:
            continue
        match = rule.match
        if isinstance(match, KeywordMatch) and item.keyword_needles is not None:
            unique = {needle for needle, _word in item.keyword_needles}
            case_sensitive = bool(match.case_sensitive)
            needle_ids = kw_cs_needles if case_sensitive else kw_ci_needles
            refs = kw_cs_refs if case_sensitive else kw_ci_refs
            needle_total[pos] = 1 if item.keyword_is_any else len(unique)
            for needle in sorted(unique):
                nid = needle_ids.get(needle)
                if nid is None:
                    nid = len(needle_ids)
                    needle_ids[needle] = nid
                    refs.append([])
                refs[nid].append(pos)
        elif isinstance(match, RegexMatch):
            if "i" in match.flags:
                # regex 模块的 IGNORECASE 是全 Unicode 折叠（TITLE/i 命中 tıtle、
                # STATUS=/i 命中 ſtatus=），str.lower()+子串检查无法复现该语义，
                # hint 预筛会漏匹配 → 不敏感正则不预筛，交回完整求值。
                always.append(pos)
                continue
            hint = _mandatory_literal(match.pattern)
            if hint is None:
                always.append(pos)
                continue
            hint_cs.setdefault(hint, []).append(pos)
        else:
            # 词表为空的关键字规则与未知匹配类型：无法用针表达，进 always 保留原行为。
            always.append(pos)

    kw_automata: list[tuple[AhoCorasick, bool]] = []
    kw_ref_groups: list[list[list[int]]] = []
    for needles_map, refs, case_sensitive in (
        (kw_cs_needles, kw_cs_refs, True),
        (kw_ci_needles, kw_ci_refs, False),
    ):
        if not needles_map:
            continue
        needle_list = list(needles_map)  # dict 插入序即编号序
        automaton = AhoCorasick(needle_list)
        automaton.build()
        kw_automata.append((automaton, case_sensitive))
        kw_ref_groups.append(refs)

    return RuleIndex(
        kw_automata=kw_automata,
        kw_refs=kw_ref_groups,
        kw_needle_total=needle_total,
        hint_map=hint_cs,
        always=always,
    )


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


def _match_rule(
    item: CompiledRule, line: str, line_no: int, line_lower: str | None
) -> RuleMatch | None:
    """单条规则的完整求值（预筛后调用）；返回 None 表示该行未命中。"""
    rule = item.rule
    if not rule.enabled:
        return None

    fields: dict[str, str | None] = {}
    matched_text: str | None = None

    if isinstance(rule.match, KeywordMatch):
        if rule.match.case_sensitive:
            haystack = line
        else:
            if line_lower is None:
                line_lower = line.lower()
            haystack = line_lower
        matched_text = _keyword_hit(rule.match, haystack, item)
        if matched_text is None:
            return None
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
            return None
        if found is None:
            return None
        fields = found.groupdict()
        matched_text = found.group(0)
    else:
        return None

    numeric_value: Decimal | None = None
    if rule.threshold is not None:
        passed, numeric_value = _threshold_matches(rule.threshold, fields)
        if not passed:
            return None

    return RuleMatch(
        rule_id=rule.id,
        severity=rule.severity,
        tags=list(rule.tags),
        fields=fields,
        numeric_value=numeric_value,
        matched_text=matched_text,
    )


def evaluate_line(compiled: CompiledRuleSet, line: str, line_no: int) -> list[RuleMatch]:
    index = compiled.index
    if index is not None:
        # 预筛路径：关键字 needle 与正则 hint 共用一次 line.lower()（仅存在消费方时）。
        line_lower = line.lower() if index.needs_lower else line
        positions = index.candidates(line, line_lower)
        return [
            match
            for pos in positions
            if (match := _match_rule(compiled.rules[pos], line, line_no, line_lower)) is not None
        ]

    matches: list[RuleMatch] = []
    # 每行最多小写化一次，且只在真正需要不敏感 haystack 的关键字规则出现时才计算。
    lower_line: str | None = None
    for item in compiled.rules:
        if (
            isinstance(item.rule.match, KeywordMatch)
            and not item.rule.match.case_sensitive
            and lower_line is None
        ):
            lower_line = line.lower()
        match = _match_rule(item, line, line_no, lower_line)
        if match is not None:
            matches.append(match)
    return matches
